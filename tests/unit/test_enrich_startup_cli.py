from __future__ import annotations

import importlib
from concurrent.futures import ThreadPoolExecutor
from contextvars import Context, copy_context
from enum import Enum
from pathlib import Path
from threading import Barrier, local
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import click
from click.testing import CliRunner
import dotenv
import pytest
from typer.main import get_command

import src.ai.tiered_router as tiered_router
from src.core.measurement_store import read_measurements, tier_decision_store_path
from src.core.policy_loader import AIFeaturePolicy


@pytest.fixture
def enrich_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[click.Group, Mock, list[Mock]]:
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    cli = importlib.import_module("cli")
    enrich = importlib.import_module("src.commands.enrich")
    root = get_command(cli.app)
    assert isinstance(root, click.Group)

    hooks = [Mock() for _ in range(3)]
    for name, hook in zip(
        ("_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance", "_maybe_run_session_catchup"),
        hooks, strict=True,
    ):
        monkeypatch.setattr(cli, name, hook)
    resolved = SimpleNamespace(
        paths=SimpleNamespace(program_id="synthetic"),
        program=SimpleNamespace(ai=None, m365=None),
    )
    monkeypatch.setattr(enrich, "_PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(enrich, "_EDITIONS_ROOT", tmp_path)
    monkeypatch.setattr(enrich, "resolve_edition", lambda *_args, **_kwargs: resolved)
    monkeypatch.setattr(enrich, "load_program_context", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(enrich, "_build_workiq_cost_guard", lambda **_kwargs: None)
    monkeypatch.setattr(enrich, "_build_workiq_breaker", lambda **_kwargs: None)
    monkeypatch.setattr(enrich, "_load_registry_lanes", lambda *_args: [])
    bridge = Mock()
    bridge.probe.return_value = SimpleNamespace(available=True, has_workiq=True)
    constructor = Mock(return_value=bridge)
    monkeypatch.setattr("src.m365.agency_bridge.AgencyBridge", constructor)
    return root, constructor, hooks


@pytest.mark.parametrize("arguments", [
    ["--edition", "synthetic", "--dry-run"],
    ["--dry-run", "--edition", "synthetic"],
    ["--edition=synthetic", "--dry-run", "--format", "json", "--no-batch"],
    ["--edition", "synthetic", "--since", "--dry-run", "--dry-run"],
])
def test_enrich_preview_skips_unrelated_startup(
    enrich_startup: tuple[click.Group, Mock, list[Mock]], arguments: list[str],
) -> None:
    root, constructor, hooks = enrich_startup
    result = CliRunner().invoke(root, ["enrich", *arguments])
    assert result.exit_code == 0, result.output + repr(result.exception)
    assert "WorkIQ budget and circuit-breaker state may be written" in result.stderr
    hooks[0].assert_called_once()
    hooks[1].assert_not_called()
    hooks[2].assert_not_called()
    constructor.assert_called_once()


@pytest.mark.parametrize("arguments", [
    ["--edition", "synthetic"],
    ["--edition", "--dry-run"],
    ["--edition=--dry-run"],
    ["--edition", "--dry-run=false"],
    ["--edition=--dry-run=false"],
    ["--edition", "synthetic", "--since", "--dry-run"],
])
def test_enrich_normal_invocation_preserves_startup(
    enrich_startup: tuple[click.Group, Mock, list[Mock]], arguments: list[str],
) -> None:
    root, constructor, hooks = enrich_startup
    result = CliRunner().invoke(root, ["enrich", *arguments])
    assert result.exit_code == 0, result.output + repr(result.exception)
    for hook in hooks:
        hook.assert_called_once()
    constructor.assert_called_once()
    assert "dry-run:" not in result.stderr


@pytest.mark.parametrize("arguments", [
    ["--dry-run"],
    ["--dry-run", "--edition", "synthetic", "--invalid-option"],
    ["--edition", "synthetic", "--dry-run=false"],
])
def test_invalid_enrich_preview_does_not_run_unrelated_startup(
    enrich_startup: tuple[click.Group, Mock, list[Mock]], arguments: list[str],
) -> None:
    root, constructor, hooks = enrich_startup
    result = CliRunner().invoke(root, ["enrich", *arguments])
    assert result.exit_code == 2, result.output + repr(result.exception)
    hooks[0].assert_called_once()
    hooks[1].assert_not_called()
    hooks[2].assert_not_called()
    constructor.assert_not_called()


@pytest.mark.parametrize("token", ["--dry-run", "--dry-run=false"])
def test_enrich_option_after_terminator_does_not_skip_startup(
    enrich_startup: tuple[click.Group, Mock, list[Mock]],
    token: str,
) -> None:
    root, constructor, hooks = enrich_startup
    result = CliRunner().invoke(root, ["enrich", "--edition", "synthetic", "--", token])
    assert result.exit_code == 2, result.output + repr(result.exception)
    for hook in hooks:
        hook.assert_called_once()
    constructor.assert_not_called()


@pytest.mark.parametrize("arguments", [
    ["--dry-run", "--help"],
    ["--help", "--dry-run"],
])
def test_enrich_preview_help_skips_unrelated_startup(
    enrich_startup: tuple[click.Group, Mock, list[Mock]], arguments: list[str],
) -> None:
    root, constructor, hooks = enrich_startup
    result = CliRunner().invoke(root, ["enrich", *arguments])
    assert result.exit_code == 0, result.output + repr(result.exception)
    assert "--dry-run" in result.output
    hooks[0].assert_called_once()
    hooks[1].assert_not_called()
    hooks[2].assert_not_called()
    constructor.assert_not_called()


@pytest.mark.parametrize("preview", [False, True])
def test_enrich_respects_root_no_catchup_option(
    enrich_startup: tuple[click.Group, Mock, list[Mock]], preview: bool,
) -> None:
    root, constructor, hooks = enrich_startup
    arguments = ["--no-catchup", "enrich", "--edition", "synthetic"]
    if preview:
        arguments.append("--dry-run")
    result = CliRunner().invoke(root, arguments)
    assert result.exit_code == 0, result.output + repr(result.exception)
    hooks[0].assert_called_once()
    if preview:
        hooks[1].assert_not_called()
        hooks[2].assert_not_called()
    else:
        hooks[1].assert_called_once()
        hooks[2].assert_called_once()
        assert hooks[2].call_args.kwargs["no_catchup"] is True
    constructor.assert_called_once()


def test_enrich_preview_keeps_safety_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    enrich_startup: tuple[click.Group, Mock, list[Mock]],
) -> None:
    root, constructor, hooks = enrich_startup
    enrich = importlib.import_module("src.commands.enrich")
    cost_guard = Mock()
    breaker = Mock()
    build_guard = Mock(return_value=cost_guard)
    build_breaker = Mock(return_value=breaker)
    monkeypatch.setattr(enrich, "_build_workiq_cost_guard", build_guard)
    monkeypatch.setattr(enrich, "_build_workiq_breaker", build_breaker)

    result = CliRunner().invoke(root, ["enrich", "--edition", "synthetic", "--dry-run"])

    assert result.exit_code == 0, result.output + repr(result.exception)
    build_guard.assert_called_once()
    assert build_guard.call_args.kwargs["edition"] == "synthetic"
    assert build_guard.call_args.kwargs["programs_root"] == tmp_path
    build_breaker.assert_called_once_with(edition="synthetic", programs_root=tmp_path)
    constructor.assert_called_once_with(workiq_breaker=breaker)
    hooks[0].assert_called_once()
    hooks[1].assert_not_called()
    hooks[2].assert_not_called()


def test_enrich_preview_registers_real_tier_sink_without_unrelated_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    cli = importlib.import_module("cli")
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    handler = Mock(side_effect=lambda edition, dry_run: _record_synthetic_tier_decision(edition))
    root.commands["enrich"] = click.Command(
        "enrich", callback=handler, params=[
            click.Option(["--edition"]),
            click.Option(["--dry-run"], is_flag=True),
        ],
    )
    sink = Mock()
    make_sink = Mock(return_value=sink)
    maintenance = Mock()
    catchup = Mock()
    monkeypatch.setattr(cli, "_program_for_startup", lambda _ctx: "synthetic")
    monkeypatch.setattr(tiered_router, "_DECISION_SINKS", [])
    monkeypatch.setattr(
        cli, "load_arch_data_fix", lambda *_args, **_kwargs: SimpleNamespace(mode=SimpleNamespace(value="off")),
    )
    monkeypatch.setattr(cli, "make_tier_decision_sink", make_sink)
    monkeypatch.setattr(cli, "_maybe_run_scheduled_db_maintenance", maintenance)
    monkeypatch.setattr(cli, "_maybe_run_session_catchup", catchup)
    monkeypatch.setattr("sys.argv", ["vertex", "enrich", "--edition", "synthetic", "--dry-run"])

    result = CliRunner().invoke(root, ["enrich", "--edition", "synthetic", "--dry-run"])

    assert result.exit_code == 0, result.output + repr(result.exception)
    handler.assert_called_once_with(edition="synthetic", dry_run=True)
    make_sink.assert_called_once()
    config = make_sink.call_args.args[0]
    assert (config.program_id, config.edition_id) == ("synthetic", "synthetic")
    sink.assert_called_once()
    sink.reset_mock()
    _record_synthetic_tier_decision("after")
    sink.assert_not_called()
    maintenance.assert_not_called()
    catchup.assert_not_called()


@pytest.fixture
def isolated_tier_sink_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[ModuleType, local, list[tiered_router.TierDecision]]:
    cli = importlib.import_module("cli")
    current = local()
    observed: list[tiered_router.TierDecision] = []
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(cli, "_program_for_startup", lambda _ctx: current.program)
    monkeypatch.setattr(
        cli, "load_arch_data_fix", lambda *_args, **_kwargs: SimpleNamespace(mode=SimpleNamespace(value="off")),
    )
    monkeypatch.setattr(tiered_router, "_DECISION_SINKS", [])
    tiered_router.register_decision_sink(observed.append)
    return cli, current, observed


def _record_synthetic_tier_decision(feature: str) -> None:
    policy = AIFeaturePolicy(
        max_tokens=800, temperature=0.2, model_tier="frontier",
        tier0_confidence_threshold=0.5, deterministic_first=True, frontier_eligible=True,
    )
    tiered_router.route_through_tiers(feature, cache_lookup_fn=lambda: "synthetic", policy=policy)


@pytest.mark.parametrize("fail_first", [False, True])
def test_tier_sink_scopes_sequential_invocations_and_cleanup(
    isolated_tier_sink_startup: tuple[ModuleType, local, list[tiered_router.TierDecision]],
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_first: bool,
) -> None:
    cli, current, observed = isolated_tier_sink_startup
    root = get_command(cli.app)
    assert isinstance(root, click.Group)

    def handler(edition: str, dry_run: bool) -> None:
        _record_synthetic_tier_decision(edition)
        if edition == "alpha-first" and fail_first:
            raise RuntimeError("synthetic command failure")

    root.commands["enrich"] = click.Command(
        "enrich", callback=handler, params=[
            click.Option(["--edition"]), click.Option(["--dry-run"], is_flag=True),
        ],
    )
    runner = CliRunner()
    for program, edition in (
        ("alpha", "alpha-first"), ("beta", "beta-only"),
        (None, "unscoped"), ("alpha", "alpha-second"),
    ):
        current.program = program
        args = ["enrich", "--edition", edition, "--dry-run"]
        monkeypatch.setattr("sys.argv", ["vertex", *args])
        result = runner.invoke(root, args)
        assert result.exit_code == (1 if fail_first and edition == "alpha-first" else 0)

    alpha = read_measurements(tier_decision_store_path("alpha", programs_root=tmp_path))
    beta = read_measurements(tier_decision_store_path("beta", programs_root=tmp_path))
    assert [(row["program_id"], row["edition_id"], row["feature"]) for row in alpha] == [
        ("alpha", "alpha-first", "alpha-first"), ("alpha", "alpha-second", "alpha-second"),
    ]
    assert [(row["program_id"], row["edition_id"], row["feature"]) for row in beta] == [
        ("beta", "beta-only", "beta-only"),
    ]
    assert alpha[0]["run_id"] != alpha[1]["run_id"]
    assert [decision.feature for decision in observed] == ["alpha-first", "beta-only", "unscoped", "alpha-second"]


@pytest.mark.parametrize("nested_program", [True, False])
def test_tier_sink_restores_parent_after_nested_invocation(
    isolated_tier_sink_startup: tuple[ModuleType, local, list[tiered_router.TierDecision]],
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nested_program: bool,
) -> None:
    cli, current, observed = isolated_tier_sink_startup
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    runner = CliRunner()

    def handler(edition: str, dry_run: bool) -> None:
        if edition == "alpha":
            _record_synthetic_tier_decision("alpha-before")
            current.program = "beta" if nested_program else None
            args = ["enrich", "--edition", "beta", "--dry-run"] if nested_program else ["admin"]
            with monkeypatch.context() as nested_patch:
                nested_patch.setattr("sys.argv", ["vertex", *args])
                nested = runner.invoke(root, args)
            assert nested.exit_code == 0, repr(nested.exception)
            current.program = "alpha"
            _record_synthetic_tier_decision("alpha-after")
        else:
            _record_synthetic_tier_decision("beta")

    root.commands["enrich"] = click.Command(
        "enrich", callback=handler, params=[
            click.Option(["--edition"]), click.Option(["--dry-run"], is_flag=True),
        ],
    )
    root.commands["admin"] = click.Command("admin", callback=lambda: _record_synthetic_tier_decision("unscoped"))
    current.program = "alpha"
    args = ["enrich", "--edition", "alpha", "--dry-run"]
    monkeypatch.setattr("sys.argv", ["vertex", *args])
    result = runner.invoke(root, args)
    assert result.exit_code == 0, repr(result.exception)
    alpha = read_measurements(tier_decision_store_path("alpha", programs_root=tmp_path))
    beta = read_measurements(tier_decision_store_path("beta", programs_root=tmp_path))
    assert [row["feature"] for row in alpha] == ["alpha-before", "alpha-after"]
    assert [row["feature"] for row in beta] == (["beta"] if nested_program else [])
    assert [decision.feature for decision in observed] == [
        "alpha-before", "beta" if nested_program else "unscoped", "alpha-after",
    ]


def test_tier_sink_isolates_overlapping_contexts(
    isolated_tier_sink_startup: tuple[ModuleType, local, list[tiered_router.TierDecision]],
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli, current, observed = isolated_tier_sink_startup
    barrier = Barrier(2, timeout=10)
    monkeypatch.setattr("sys.argv", ["vertex", "enrich"])

    def run(program: str) -> None:
        current.program = program
        with click.Context(click.Command("enrich")) as ctx:
            ctx.invoked_subcommand = "enrich"
            cli._maybe_register_tier_decision_sink(ctx)
            cli._maybe_register_tier_decision_sink(ctx)
            barrier.wait()
            _record_synthetic_tier_decision(program)
            barrier.wait()

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(run, ("alpha", "beta")))

    for program in ("alpha", "beta"):
        rows = read_measurements(tier_decision_store_path(program, programs_root=tmp_path))
        assert [(row["program_id"], row["feature"]) for row in rows] == [(program, program)]
    assert {decision.feature for decision in observed} == {"alpha", "beta"}


def test_tier_sink_does_not_outlive_copied_invocation_context(
    isolated_tier_sink_startup: tuple[ModuleType, local, list[tiered_router.TierDecision]],
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli, current, observed = isolated_tier_sink_startup
    contexts: list[Context] = []
    root = get_command(cli.app)
    assert isinstance(root, click.Group)

    def handler(edition: str, dry_run: bool) -> None:
        contexts.append(copy_context())
        _record_synthetic_tier_decision("during")

    root.commands["enrich"] = click.Command(
        "enrich", callback=handler, params=[
            click.Option(["--edition"]), click.Option(["--dry-run"], is_flag=True),
        ],
    )
    current.program = "alpha"
    args = ["enrich", "--edition", "alpha", "--dry-run"]
    monkeypatch.setattr("sys.argv", ["vertex", *args])
    result = CliRunner().invoke(root, args)
    assert result.exit_code == 0, repr(result.exception)
    contexts[0].run(_record_synthetic_tier_decision, "after")
    rows = read_measurements(tier_decision_store_path("alpha", programs_root=tmp_path))
    assert [row["feature"] for row in rows] == ["during"]
    assert [decision.feature for decision in observed] == ["during", "after"]


def test_tier_sink_uses_embedded_invocation_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = importlib.import_module("cli")
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(
        cli, "resolve_edition_paths",
        lambda edition, **_kwargs: SimpleNamespace(program_id=edition),
    )
    monkeypatch.setattr(
        cli, "load_arch_data_fix", lambda *_args, **_kwargs: SimpleNamespace(mode=SimpleNamespace(value="off")),
    )
    monkeypatch.setattr(tiered_router, "_DECISION_SINKS", [])
    monkeypatch.setattr("sys.argv", ["embedded-host", "--edition", "alpha"])
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    root.commands["enrich"] = click.Command(
        "enrich", callback=lambda edition, dry_run: _record_synthetic_tier_decision(edition),
        params=[click.Option(["--edition"]), click.Option(["--dry-run"], is_flag=True)],
    )
    for edition in ("alpha", "beta"):
        result = CliRunner().invoke(root, ["enrich", "--edition", edition, "--dry-run"])
        assert result.exit_code == 0, repr(result.exception)
    for edition in ("alpha", "beta"):
        rows = read_measurements(tier_decision_store_path(edition, programs_root=tmp_path))
        assert [(row["program_id"], row["edition_id"], row["feature"]) for row in rows] == [
            (edition, edition, edition),
        ]


@pytest.mark.parametrize(
    ("arguments", "program", "edition"),
    [
        (["status", "--edition", "alpha", "--edition", "beta"], "beta", "beta"),
        (["--edition", "alpha", "status", "--edition", "beta"], "beta", "beta"),
        (["status", "--edition", "beta", "--since", "--edition=alpha"], "beta", "beta"),
        (["status", "--edition", "beta", "--since", "--program=alpha"], "beta", "beta"),
        (["--edition", "alpha", "status", "--program", "beta"], "beta", None),
        (["status", "--program", "alpha", "--program", "beta"], "beta", None),
        (["status", "-p", "beta"], "beta", None),
        (["status", "--program", "alpha", "--edition", "beta"], None, None),
        (["status", "--since", "--program=alpha"], None, None),
        (["--edition", "alpha", "status"], None, None),
    ],
)
def test_tier_sink_and_startup_use_parsed_command_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    program: str | None,
    edition: str | None,
) -> None:
    cli = importlib.import_module("cli")
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(
        cli, "resolve_edition_paths",
        lambda value, **_kwargs: SimpleNamespace(program_id=value),
    )
    monkeypatch.setattr(
        cli, "load_arch_data_fix",
        lambda *_args, **_kwargs: SimpleNamespace(mode=SimpleNamespace(value="off")),
    )
    monkeypatch.setattr(tiered_router, "_DECISION_SINKS", [])
    monkeypatch.setattr("sys.argv", ["embedded-host", "--edition", "alpha"])
    maintenance = Mock()
    catchup = Mock()
    monkeypatch.setattr(cli, "maybe_run_scheduled_compaction", maintenance)
    monkeypatch.setattr(cli, "_stdout_supports_interactive_catchup", lambda: True)
    monkeypatch.setattr(cli, "build_catchup_event_builder", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "maybe_catchup", catchup)
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    root.commands["status"] = click.Command(
        "status",
        callback=lambda program, edition, since: _record_synthetic_tier_decision("decision"),
        params=[
            click.Option(["--program", "-p"]),
            click.Option(["--edition"]),
            click.Option(["--since"]),
        ],
    )

    result = CliRunner().invoke(root, arguments)

    assert result.exit_code == 0, repr(result.exception)
    for candidate in ("alpha", "beta"):
        rows = read_measurements(tier_decision_store_path(candidate, programs_root=tmp_path))
        expected = [(program, edition, "decision")] if candidate == program else []
        assert [(row["program_id"], row["edition_id"], row["feature"]) for row in rows] == expected
    if program is None:
        maintenance.assert_not_called()
        catchup.assert_not_called()
    else:
        maintenance.assert_called_once_with(program)
        assert catchup.call_args.args == (program,)


def test_startup_scope_probe_does_not_run_parameter_callbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = importlib.import_module("cli")
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(
        cli, "resolve_edition_paths",
        lambda value, **_kwargs: SimpleNamespace(program_id=value),
    )
    monkeypatch.setattr("sys.argv", ["embedded-host"])
    maintenance = Mock()
    sink_factory = Mock()
    monkeypatch.setattr(cli, "maybe_run_scheduled_compaction", maintenance)
    monkeypatch.setattr(cli, "make_tier_decision_sink", sink_factory)
    callback = Mock(side_effect=lambda _ctx, _param, value: value.upper())
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    root.commands["status"] = click.Command(
        "status",
        callback=lambda edition: _record_synthetic_tier_decision(edition),
        params=[click.Option(["--edition"], callback=callback)],
    )

    result = CliRunner().invoke(root, ["status", "--edition", "beta"])

    assert result.exit_code == 0, repr(result.exception)
    assert callback.call_count == 1
    sink_factory.assert_not_called()
    maintenance.assert_not_called()


@pytest.mark.parametrize(("child_program", "scoped"), [("alpha", True), ("beta", False)])
def test_nested_command_scope_refuses_conflicting_programs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, child_program: str, scoped: bool,
) -> None:
    cli = importlib.import_module("cli")
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(
        cli, "load_arch_data_fix",
        lambda *_args, **_kwargs: SimpleNamespace(mode=SimpleNamespace(value="off")),
    )
    monkeypatch.setattr(tiered_router, "_DECISION_SINKS", [])
    monkeypatch.setattr("sys.argv", ["embedded-host"])
    maintenance = Mock()
    monkeypatch.setattr(cli, "maybe_run_scheduled_compaction", maintenance)
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    group = click.Group(
        "signals",
        callback=lambda program: _record_synthetic_tier_decision("parent"),
        params=[click.Option(["--program"])],
    )
    group.add_command(click.Command(
        "show",
        callback=lambda program: _record_synthetic_tier_decision("child"),
        params=[click.Option(["--program"])],
    ))
    root.commands["signals"] = group

    result = CliRunner().invoke(
        root, ["signals", "--program", "alpha", "show", "--program", child_program],
    )

    assert result.exit_code == 0, repr(result.exception)
    rows = read_measurements(tier_decision_store_path("alpha", programs_root=tmp_path))
    assert [row["feature"] for row in rows] == (["parent", "child"] if scoped else [])
    if scoped:
        maintenance.assert_called_once_with("alpha")
    else:
        maintenance.assert_not_called()


def test_embedded_admin_db_command_skips_scheduled_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = importlib.import_module("cli")
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["embedded-host"])
    maintenance = Mock()
    monkeypatch.setattr(cli, "maybe_run_scheduled_compaction", maintenance)
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    admin = click.Group("admin")
    db = click.Group("db")
    db.add_command(click.Command(
        "verify", callback=lambda program: None, params=[click.Option(["--program"])],
    ))
    admin.add_command(db)
    root.commands["admin"] = admin

    result = CliRunner().invoke(root, ["admin", "db", "verify", "--program", "beta"])

    assert result.exit_code == 0, repr(result.exception)
    maintenance.assert_not_called()


@pytest.mark.parametrize(
    ("arguments", "group_name", "command_name"),
    [
        (["status", "--edition", "alpha", "--edition", "beta"], None, "status"),
        (["actuate", "review", "--program", "alpha", "-p", "beta"], "actuate", "review"),
    ],
)
def test_tier_sink_uses_real_typer_command_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    group_name: str | None,
    command_name: str,
) -> None:
    cli = importlib.import_module("cli")
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(
        cli, "resolve_edition_paths",
        lambda value, **_kwargs: SimpleNamespace(program_id=value),
    )
    monkeypatch.setattr(
        cli, "load_arch_data_fix",
        lambda *_args, **_kwargs: SimpleNamespace(mode=SimpleNamespace(value="off")),
    )
    monkeypatch.setattr(tiered_router, "_DECISION_SINKS", [])
    monkeypatch.setattr(cli, "_maybe_run_scheduled_db_maintenance", Mock())
    monkeypatch.setattr(cli, "_maybe_run_session_catchup", Mock())
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    parent = root if group_name is None else root.commands[group_name]
    assert isinstance(parent, click.Group)
    command = parent.commands[command_name]
    handler = Mock(side_effect=lambda **kwargs: _record_synthetic_tier_decision(
        kwargs["edition"] if group_name is None else kwargs["program"]
    ))
    monkeypatch.setattr(command, "callback", handler)

    result = CliRunner().invoke(root, arguments)

    assert result.exit_code == 0, result.output + repr(result.exception)
    handler.assert_called_once()
    rows = read_measurements(tier_decision_store_path("beta", programs_root=tmp_path))
    assert [(row["program_id"], row["edition_id"], row["feature"]) for row in rows] == [
        ("beta", "beta" if group_name is None else None, "beta"),
    ]
    assert read_measurements(tier_decision_store_path("alpha", programs_root=tmp_path)) == ()


def test_tier_sink_uses_real_typer_watch_with_default_cadence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = importlib.import_module("cli")
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(
        cli, "load_arch_data_fix",
        lambda *_args, **_kwargs: SimpleNamespace(mode=SimpleNamespace(value="off")),
    )
    monkeypatch.setattr(tiered_router, "_DECISION_SINKS", [])
    monkeypatch.setattr(cli, "_maybe_run_scheduled_db_maintenance", Mock())
    sink = Mock()
    sink_factory = Mock(return_value=sink)
    monkeypatch.setattr(cli, "make_tier_decision_sink", sink_factory)
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    handler = Mock(side_effect=lambda **_kwargs: _record_synthetic_tier_decision("watch"))
    monkeypatch.setattr(root.commands["watch"], "callback", handler)

    result = CliRunner().invoke(root, ["watch", "--program", "alpha"])

    assert result.exit_code == 0, result.output + repr(result.exception)
    handler.assert_called_once()
    sink_factory.assert_called_once()
    assert sink_factory.call_args.args[0].program_id == "alpha"
    sink.assert_called_once()


def test_startup_scope_probe_refuses_transformed_parameter_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UppercaseType(click.ParamType):
        name = "uppercase"

        def convert(
            self, value: object, param: click.Parameter | None, ctx: click.Context | None,
        ) -> str:
            return str(value).upper()

    cli = importlib.import_module("cli")
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(
        cli, "resolve_edition_paths",
        lambda value, **_kwargs: SimpleNamespace(program_id=value),
    )
    monkeypatch.setattr(
        cli, "load_arch_data_fix",
        lambda *_args, **_kwargs: SimpleNamespace(mode=SimpleNamespace(value="off")),
    )
    sink_factory = Mock()
    maintenance = Mock()
    monkeypatch.setattr(cli, "make_tier_decision_sink", sink_factory)
    monkeypatch.setattr(cli, "maybe_run_scheduled_compaction", maintenance)
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    handler = Mock()
    root.commands["status"] = click.Command(
        "status", callback=handler, params=[click.Option(["--edition"], type=UppercaseType())],
    )

    result = CliRunner().invoke(root, ["status", "--edition", "beta"])

    assert result.exit_code == 0, result.output + repr(result.exception)
    handler.assert_called_once_with(edition="BETA")
    sink_factory.assert_not_called()
    maintenance.assert_not_called()


def test_startup_scope_probe_does_not_inspect_custom_type_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    contexts: list[str | None] = []

    class MutatingType(click.ParamType):
        @property
        def __class__(self) -> type[MutatingType]:
            ctx = click.get_current_context()
            contexts.append(ctx.info_name)
            ctx.params["edition"] = "alpha"
            return MutatingType

        def convert(
            self, value: object, param: click.Parameter | None, ctx: click.Context | None,
        ) -> str:
            assert ctx is not None
            ctx.params["edition"] = "alpha"
            return str(value)

    cli = importlib.import_module("cli")
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(
        cli, "resolve_edition_paths",
        lambda value, **_kwargs: SimpleNamespace(program_id=value),
    )
    sink_factory = Mock()
    maintenance = Mock()
    monkeypatch.setattr(cli, "make_tier_decision_sink", sink_factory)
    monkeypatch.setattr(cli, "maybe_run_scheduled_compaction", maintenance)
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    handler = Mock()
    option = click.Option(["--other"], type=str)
    option.type = MutatingType()
    root.commands["status"] = click.Command(
        "status", callback=handler, params=[click.Option(["--edition"]), option],
    )

    result = CliRunner().invoke(root, ["status", "--edition", "beta", "--other", "value"])

    assert result.exit_code == 0, result.output + repr(result.exception)
    handler.assert_called_once_with(edition="alpha", other="value")
    assert "root" not in contexts
    sink_factory.assert_not_called()
    maintenance.assert_not_called()


def test_startup_scope_probe_refuses_custom_option_processing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UppercaseOption(click.Option):
        def process_value(self, ctx: click.Context, value: object) -> object:
            return str(super().process_value(ctx, value)).upper()

    cli = importlib.import_module("cli")
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(
        cli, "resolve_edition_paths",
        lambda value, **_kwargs: SimpleNamespace(program_id=value),
    )
    sink_factory = Mock()
    maintenance = Mock()
    monkeypatch.setattr(cli, "make_tier_decision_sink", sink_factory)
    monkeypatch.setattr(cli, "maybe_run_scheduled_compaction", maintenance)
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    handler = Mock()
    root.commands["status"] = click.Command(
        "status", callback=handler, params=[UppercaseOption(["--edition"])],
    )

    result = CliRunner().invoke(root, ["status", "--edition", "beta"])

    assert result.exit_code == 0, result.output + repr(result.exception)
    handler.assert_called_once_with(edition="BETA")
    sink_factory.assert_not_called()
    maintenance.assert_not_called()


@pytest.mark.parametrize(
    "source", ["callback", "type", "default", "object", "enum", "func", "tuple", "path", "choice"],
)
def test_startup_scope_probe_refuses_other_parameter_scope_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str,
) -> None:
    class MutatingType(click.ParamType):
        def convert(
            self, value: object, param: click.Parameter | None, ctx: click.Context | None,
        ) -> object:
            assert ctx is not None
            ctx.params["edition"] = "alpha"
            return value

    def mutate(_ctx: click.Context, _param: click.Parameter, value: object) -> object:
        _ctx.params["edition"] = "alpha"
        return value

    def default() -> str:
        click.get_current_context().params["edition"] = "alpha"
        return "value"

    def convert(value: str) -> str:
        click.get_current_context().params["edition"] = "alpha"
        return value

    class MutatingDefault:
        def __str__(self) -> str:
            click.get_current_context().params["edition"] = "alpha"
            return "value"

    class MutatingEnum(Enum):
        VALUE = "value"

        def __str__(self) -> str:
            click.get_current_context().params["edition"] = "alpha"
            return self.value

    class MutatingChoice:
        def __str__(self) -> str:
            click.get_current_context().params["edition"] = "alpha"
            return "value"

    class MutatingPath(str):
        def __new__(cls, value: str) -> MutatingPath:
            click.get_current_context().params["edition"] = "alpha"
            return super().__new__(cls, value)

    cli = importlib.import_module("cli")
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(
        cli, "resolve_edition_paths",
        lambda value, **_kwargs: SimpleNamespace(program_id=value),
    )
    sink_factory = Mock()
    maintenance = Mock()
    monkeypatch.setattr(cli, "make_tier_decision_sink", sink_factory)
    monkeypatch.setattr(cli, "maybe_run_scheduled_compaction", maintenance)
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    handler = Mock()
    option_args: dict[str, object] = {}
    choice = MutatingChoice()
    if source == "callback":
        option_args["callback"] = mutate
    elif source == "type":
        option_args["type"] = MutatingType()
    elif source == "default":
        option_args["default"] = default
    elif source == "object":
        option_args.update(type=str, default=MutatingDefault())
    elif source == "enum":
        option_args.update(type=str, default=MutatingEnum.VALUE)
    elif source == "func":
        option_args["type"] = convert
    elif source == "path":
        option_args["type"] = click.Path(path_type=MutatingPath)
    elif source == "choice":
        option_args["type"] = click.Choice([choice])
    else:
        option_args["type"] = (str, MutatingType())
    root.commands["status"] = click.Command(
        "status", callback=handler, params=[
            click.Option(["--edition"]), click.Option(["--other"], **option_args),
        ],
    )
    arguments = ["status", "--edition", "beta"]
    if source not in {"default", "object", "enum"}:
        arguments.extend(["--other", *(["first"] if source == "tuple" else []), "value"])

    result = CliRunner().invoke(root, arguments)

    assert result.exit_code == 0, result.output + repr(result.exception)
    handler.assert_called_once_with(
        edition="alpha",
        other=("first", "value") if source == "tuple" else choice if source == "choice" else "value",
    )
    sink_factory.assert_not_called()
    maintenance.assert_not_called()


@pytest.mark.parametrize("attribute", ["value", "_value_", "_name_", "__class__", "object_class"])
def test_startup_scope_probe_refuses_enum_property_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attribute: str,
) -> None:
    calls: list[str | None] = []

    def mark_mutation() -> None:
        ctx = click.get_current_context()
        calls.append(ctx.info_name)
        ctx.params["edition"] = "alpha"

    def mutate(_value: Enum) -> str:
        mark_mutation()
        return "VALUE" if attribute == "_name_" else "value"

    class MutatingClassEnum(Enum):
        VALUE = "value"

        @property
        def __class__(self) -> type[MutatingClassEnum]:
            mark_mutation()
            return MutatingClassEnum

    class MutatingEnum(Enum):
        VALUE = "value"

    class MutatingObject:
        @property
        def __class__(self) -> type[MutatingObject]:
            mark_mutation()
            return MutatingObject

        def __str__(self) -> str:
            return "value"

    if attribute == "__class__":
        default = MutatingClassEnum.VALUE
    elif attribute == "object_class":
        default = MutatingObject()
    else:
        setattr(MutatingEnum, attribute, property(mutate))
        default = MutatingEnum.VALUE

    cli = importlib.import_module("cli")
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(
        cli, "resolve_edition_paths",
        lambda value, **_kwargs: SimpleNamespace(program_id=value),
    )
    sink_factory = Mock()
    maintenance = Mock()
    monkeypatch.setattr(cli, "make_tier_decision_sink", sink_factory)
    monkeypatch.setattr(cli, "maybe_run_scheduled_compaction", maintenance)
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    handler = Mock()
    root.commands["status"] = click.Command(
        "status", callback=handler, params=[
            click.Option(["--edition"]), click.Option(["--other"], type=str, default=default),
        ],
    )

    result = CliRunner().invoke(root, ["status", "--edition", "beta"])

    assert result.exit_code == 0, result.output + repr(result.exception)
    expected_edition = "alpha" if attribute in {"_name_", "__class__", "object_class"} else "beta"
    expected_name = "MutatingClassEnum" if attribute == "__class__" else "MutatingEnum"
    expected_other = "value" if attribute == "object_class" else f"{expected_name}.VALUE"
    handler.assert_called_once_with(edition=expected_edition, other=expected_other)
    if attribute in {"_name_", "__class__", "object_class"}:
        assert calls and set(calls) == {"status"}
    else:
        assert not calls
    sink_factory.assert_not_called()
    maintenance.assert_not_called()


@pytest.mark.parametrize("attribute", ["__str__", "__getattribute__"])
@pytest.mark.parametrize("string_enum", [False, True])
def test_startup_value_check_does_not_invoke_enum_class_descriptors(attribute: str, string_enum: bool) -> None:
    calls: list[str] = []

    class MutatingDescriptor:
        def __get__(self, _instance: object, _owner: type[Enum]) -> object:
            calls.append(attribute)
            return Enum.__str__ if attribute == "__str__" else object.__getattribute__

    class MutatingEnum(Enum):
        VALUE = "value"

    class MutatingStringEnum(str, Enum):
        VALUE = "value"

    enum_type = MutatingStringEnum if string_enum else MutatingEnum
    value = enum_type.VALUE
    setattr(enum_type, attribute, MutatingDescriptor())
    cli = importlib.import_module("cli")

    safe = cli._safe_startup_value(value)
    assert not safe
    assert calls == []


@pytest.mark.parametrize("container", ["scalar", "tuple", "list"])
def test_startup_value_check_does_not_compare_custom_metaclasses(container: str) -> None:
    calls: list[object] = []

    class SpoofingMeta(type):
        def __eq__(cls, other: object) -> bool:
            calls.append(other)
            return True

    class SpoofingValue(metaclass=SpoofingMeta):
        pass

    value = SpoofingValue()
    candidate = {"scalar": value, "tuple": (value,), "list": [value]}[container]
    cli = importlib.import_module("cli")

    assert not cli._safe_startup_value(candidate)
    assert calls == []


@pytest.mark.parametrize("source", ["type", "range", "choice", "choices", "formats"])
def test_startup_type_check_does_not_compare_custom_metaclasses(source: str) -> None:
    calls: list[object] = []

    class SpoofingMeta(type):
        def __eq__(cls, other: object) -> bool:
            calls.append(other)
            return True

    class SpoofingValue(metaclass=SpoofingMeta):
        def __iter__(self):
            return iter(["safe"])

    class SpoofingType(click.ParamType, metaclass=SpoofingMeta):
        pass

    if source == "type":
        param_type = SpoofingType()
    elif source == "range":
        param_type = click.IntRange()
        param_type.min = SpoofingValue()
    elif source in {"choice", "choices"}:
        param_type = click.Choice(["safe"])
        param_type.choices = (SpoofingValue(),) if source == "choice" else SpoofingValue()
    else:
        param_type = click.DateTime()
        param_type.formats = SpoofingValue()
    calls.clear()
    cli = importlib.import_module("cli")

    assert not cli._safe_startup_type(param_type)
    assert calls == []


@pytest.mark.parametrize("subject", ["command", "parameter"])
def test_startup_scope_probe_does_not_compare_custom_metaclasses(subject: str) -> None:
    calls: list[str] = []

    class SpoofingMeta(type):
        def __eq__(cls, _other: object) -> bool:
            calls.append("equality")
            return True

    class SpoofingCommand(click.Command, metaclass=SpoofingMeta):
        def make_parser(self, ctx: click.Context):
            calls.append("parser")
            return super().make_parser(ctx)

    class SpoofingOption(click.Option, metaclass=SpoofingMeta):
        def add_to_parser(self, parser, ctx: click.Context) -> None:
            calls.append("parameter parser")
            super().add_to_parser(parser, ctx)

    command = (
        SpoofingCommand("status")
        if subject == "command"
        else click.Command("status", params=[SpoofingOption(["--other"])])
    )
    parent = click.Context(click.Group("root"), info_name="root")
    calls.clear()
    cli = importlib.import_module("cli")

    assert cli._parsed_startup_scope(parent, "status", command, []) is None
    assert calls == []


def test_startup_scope_probe_refuses_metaclass_type_spoofing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    contexts: list[str | None] = []

    class SpoofingMeta(type):
        def __eq__(cls, other: object) -> bool:
            ctx = click.get_current_context(silent=True)
            contexts.append(ctx.info_name if ctx is not None else None)
            return other is click.types.StringParamType

    class MutatingType(click.ParamType, metaclass=SpoofingMeta):
        def convert(
            self, value: object, param: click.Parameter | None, ctx: click.Context | None,
        ) -> str:
            assert ctx is not None
            ctx.params["edition"] = "alpha"
            return str(value)

    cli = importlib.import_module("cli")
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(
        cli, "resolve_edition_paths",
        lambda value, **_kwargs: SimpleNamespace(program_id=value),
    )
    sink_factory = Mock()
    maintenance = Mock()
    monkeypatch.setattr(cli, "make_tier_decision_sink", sink_factory)
    monkeypatch.setattr(cli, "maybe_run_scheduled_compaction", maintenance)
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    handler = Mock()
    option = click.Option(["--other"], type=str)
    option.type = MutatingType()
    root.commands["status"] = click.Command(
        "status", callback=handler, params=[click.Option(["--edition"]), option],
    )
    contexts.clear()

    result = CliRunner().invoke(root, ["status", "--edition", "beta", "--other", "value"])

    assert result.exit_code == 0, result.output + repr(result.exception)
    handler.assert_called_once_with(edition="alpha", other="value")
    assert "root" not in contexts
    sink_factory.assert_not_called()
    maintenance.assert_not_called()


def test_startup_scope_probe_refuses_token_normalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    contexts: list[str | None] = []

    def normalize(value: str) -> str:
        ctx = click.get_current_context()
        contexts.append(ctx.info_name)
        if ctx.info_name == "status" and value == "value":
            ctx.params["edition"] = "alpha"
        return value

    cli = importlib.import_module("cli")
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(
        cli, "resolve_edition_paths",
        lambda value, **_kwargs: SimpleNamespace(program_id=value),
    )
    sink_factory = Mock()
    maintenance = Mock()
    monkeypatch.setattr(cli, "make_tier_decision_sink", sink_factory)
    monkeypatch.setattr(cli, "maybe_run_scheduled_compaction", maintenance)
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    handler = Mock()
    root.commands["status"] = click.Command(
        "status", callback=handler, params=[
            click.Option(["--edition"]), click.Option(["--other"], type=click.Choice(["value"])),
        ],
        context_settings={"token_normalize_func": normalize},
    )

    result = CliRunner().invoke(root, ["status", "--edition", "beta", "--other", "value"])

    assert result.exit_code == 0, result.output + repr(result.exception)
    handler.assert_called_once_with(edition="alpha", other="value")
    assert contexts and set(contexts) == {"status"}
    sink_factory.assert_not_called()
    maintenance.assert_not_called()


def test_startup_scope_probe_does_not_evaluate_default_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def default() -> str:
        calls.append("called")
        click.get_current_context().params["edition"] = "alpha"
        return "value"

    cli = importlib.import_module("cli")
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(
        cli, "resolve_edition_paths",
        lambda value, **_kwargs: SimpleNamespace(program_id=value),
    )
    sink_factory = Mock()
    maintenance = Mock()
    monkeypatch.setattr(cli, "make_tier_decision_sink", sink_factory)
    monkeypatch.setattr(cli, "maybe_run_scheduled_compaction", maintenance)
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    handler = Mock()
    root.commands["status"] = click.Command(
        "status", callback=handler, params=[
            click.Option(["--edition"]), click.Option(["--other"]),
        ],
    )

    result = CliRunner().invoke(
        root, ["status", "--edition", "beta"],
        default_map={"status": {"other": default}},
    )

    assert result.exit_code == 0, result.output + repr(result.exception)
    handler.assert_called_once_with(edition="alpha", other="value")
    assert calls == ["called"]
    sink_factory.assert_not_called()
    maintenance.assert_not_called()


def test_invalid_startup_scope_warns_once_without_echoing_identifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    cli = importlib.import_module("cli")
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(cli, "_stdout_supports_interactive_catchup", lambda: True)
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    handler = Mock()
    root.commands["status"] = click.Command(
        "status", callback=handler, params=[click.Option(["--program"])],
    )

    result = CliRunner().invoke(root, ["status", "--program", "private:identifier"])

    assert result.exit_code == 0, result.output + repr(result.exception)
    handler.assert_called_once_with(program="private:identifier")
    warnings = [
        record.message for record in caplog.records
        if record.name == "cli" and "startup scope" in record.message
    ]
    assert len(warnings) == 1
    assert "private:identifier" not in warnings[0]


def test_explicit_scope_takes_precedence_over_default_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = importlib.import_module("cli")
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(
        cli, "resolve_edition_paths",
        lambda _value, **_kwargs: SimpleNamespace(program_id="alpha"),
    )
    monkeypatch.setattr(
        cli, "load_arch_data_fix",
        lambda *_args, **_kwargs: SimpleNamespace(mode=SimpleNamespace(value="off")),
    )
    monkeypatch.setattr(tiered_router, "_DECISION_SINKS", [])
    monkeypatch.setenv("VERTEX_SCOPE_TEST_EDITION", "other")
    maintenance = Mock()
    monkeypatch.setattr(cli, "maybe_run_scheduled_compaction", maintenance)
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    root.commands["status"] = click.Command(
        "status",
        callback=lambda program, edition: _record_synthetic_tier_decision(edition),
        params=[
            click.Option(["--program"]),
            click.Option(["--edition"], default="other", envvar="VERTEX_SCOPE_TEST_EDITION"),
        ],
    )

    result = CliRunner().invoke(root, ["status", "--program", "alpha", "--edition", "beta"])

    assert result.exit_code == 0, result.output + repr(result.exception)
    rows = read_measurements(tier_decision_store_path("alpha", programs_root=tmp_path))
    assert [(row["program_id"], row["edition_id"], row["feature"]) for row in rows] == [
        ("alpha", "beta", "beta"),
    ]
    maintenance.assert_called_once_with("alpha")


@pytest.mark.parametrize(
    "source",
    ["default", "envvar", "autoenv", "default_map", "prompt", "callback", "required"],
)
def test_startup_scope_probe_refuses_unobserved_scope_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str,
) -> None:
    cli = importlib.import_module("cli")
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(
        cli, "resolve_edition_paths",
        lambda value, **_kwargs: SimpleNamespace(program_id=value),
    )
    monkeypatch.setattr(
        cli, "load_arch_data_fix",
        lambda *_args, **_kwargs: SimpleNamespace(mode=SimpleNamespace(value="off")),
    )
    sink_factory = Mock()
    maintenance = Mock()
    catchup = Mock()
    monkeypatch.setattr(cli, "make_tier_decision_sink", sink_factory)
    monkeypatch.setattr(cli, "maybe_run_scheduled_compaction", maintenance)
    monkeypatch.setattr(cli, "_stdout_supports_interactive_catchup", lambda: True)
    monkeypatch.setattr(cli, "build_catchup_event_builder", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "maybe_catchup", catchup)
    option_args: dict[str, object] = {}
    settings: dict[str, object] = {}
    if source == "default":
        option_args["default"] = "beta"
    elif source == "envvar":
        option_args["envvar"] = "VERTEX_SCOPE_TEST_EDITION"
        monkeypatch.setenv("VERTEX_SCOPE_TEST_EDITION", "beta")
    elif source == "autoenv":
        settings["auto_envvar_prefix"] = "VERTEX_SCOPE_TEST"
        monkeypatch.setenv("VERTEX_SCOPE_TEST_EDITION", "beta")
    elif source == "prompt":
        option_args["prompt"] = True
    elif source == "callback":
        option_args["callback"] = lambda _ctx, _param, value: value or "beta"
    elif source == "required":
        option_args["required"] = True

    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    handler = Mock()
    root.commands["status"] = click.Command(
        "status",
        callback=handler,
        params=[click.Option(["--program"]), click.Option(["--edition"], **option_args)],
        context_settings=settings,
    )
    invoke_args: dict[str, object] = {}
    if source == "default_map":
        invoke_args["default_map"] = {"status": {"edition": "beta"}}
    elif source == "prompt":
        invoke_args["input"] = "beta\n"

    result = CliRunner().invoke(root, ["status", "--program", "alpha"], **invoke_args)

    if source == "required":
        assert result.exit_code == 2, result.output + repr(result.exception)
        handler.assert_not_called()
    else:
        assert result.exit_code == 0, result.output + repr(result.exception)
        handler.assert_called_once_with(program="alpha", edition="beta")
    sink_factory.assert_not_called()
    maintenance.assert_not_called()
    catchup.assert_not_called()


@pytest.mark.parametrize("program", ["..", "alpha\\other", "alpha:other", "alpha*", "alpha\nother"])
def test_startup_scope_probe_refuses_invalid_program_before_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program: str,
) -> None:
    cli = importlib.import_module("cli")
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(
        cli, "load_arch_data_fix",
        lambda *_args, **_kwargs: SimpleNamespace(mode=SimpleNamespace(value="off")),
    )
    sink_factory = Mock()
    maintenance = Mock()
    monkeypatch.setattr(cli, "make_tier_decision_sink", sink_factory)
    monkeypatch.setattr(cli, "maybe_run_scheduled_compaction", maintenance)
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    handler = Mock()
    root.commands["status"] = click.Command(
        "status", callback=handler, params=[click.Option(["--program"])],
    )

    result = CliRunner().invoke(root, ["status", "--program", program])

    assert result.exit_code == 0, result.output + repr(result.exception)
    handler.assert_called_once_with(program=program)
    sink_factory.assert_not_called()
    maintenance.assert_not_called()


@pytest.mark.parametrize("edition", ["..", "alpha\\other", "alpha:other", "alpha*", "alpha\nother"])
def test_startup_scope_probe_refuses_invalid_edition_before_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edition: str,
) -> None:
    cli = importlib.import_module("cli")
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", tmp_path)
    resolve = Mock()
    sink_factory = Mock()
    maintenance = Mock()
    monkeypatch.setattr(cli, "resolve_edition_paths", resolve)
    monkeypatch.setattr(cli, "make_tier_decision_sink", sink_factory)
    monkeypatch.setattr(cli, "maybe_run_scheduled_compaction", maintenance)
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    handler = Mock()
    root.commands["status"] = click.Command(
        "status", callback=handler, params=[click.Option(["--edition"])],
    )

    result = CliRunner().invoke(root, ["status", "--edition", edition])

    assert result.exit_code == 0, result.output + repr(result.exception)
    handler.assert_called_once_with(edition=edition)
    resolve.assert_not_called()
    sink_factory.assert_not_called()
    maintenance.assert_not_called()


def test_startup_scope_probe_refuses_invalid_resolved_program(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = importlib.import_module("cli")
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(
        cli, "resolve_edition_paths",
        lambda _value, **_kwargs: SimpleNamespace(program_id=".."),
    )
    sink_factory = Mock()
    maintenance = Mock()
    monkeypatch.setattr(cli, "make_tier_decision_sink", sink_factory)
    monkeypatch.setattr(cli, "maybe_run_scheduled_compaction", maintenance)
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    handler = Mock()
    root.commands["status"] = click.Command(
        "status", callback=handler, params=[click.Option(["--edition"])],
    )

    result = CliRunner().invoke(root, ["status", "--edition", "beta"])

    assert result.exit_code == 0, result.output + repr(result.exception)
    handler.assert_called_once_with(edition="beta")
    sink_factory.assert_not_called()
    maintenance.assert_not_called()


def test_scoped_startup_resolves_edition_once_per_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = importlib.import_module("cli")
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", tmp_path)
    resolve = Mock(side_effect=[
        SimpleNamespace(program_id="alpha"),
        SimpleNamespace(program_id="beta"),
        SimpleNamespace(program_id="beta"),
    ])
    sink_factory = Mock()
    maintenance = Mock()
    catchup = Mock()
    monkeypatch.setattr(cli, "resolve_edition_paths", resolve)
    monkeypatch.setattr(cli, "make_tier_decision_sink", sink_factory)
    monkeypatch.setattr(cli, "maybe_run_scheduled_compaction", maintenance)
    monkeypatch.setattr(cli, "_stdout_supports_interactive_catchup", lambda: True)
    monkeypatch.setattr(cli, "build_catchup_event_builder", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "maybe_catchup", catchup)
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    root.commands["status"] = click.Command(
        "status", callback=lambda edition: None, params=[click.Option(["--edition"])],
    )

    result = CliRunner().invoke(root, ["status", "--edition", "weekly"])

    assert result.exit_code == 0, result.output + repr(result.exception)
    resolve.assert_called_once_with("weekly", programs_root=tmp_path)
    assert sink_factory.call_args.args[0].program_id == "alpha"
    maintenance.assert_called_once_with("alpha")
    assert catchup.call_args.args == ("alpha",)


def test_scoped_command_help_does_not_run_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = importlib.import_module("cli")
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", tmp_path)
    sink_factory = Mock()
    maintenance = Mock()
    monkeypatch.setattr(cli, "make_tier_decision_sink", sink_factory)
    monkeypatch.setattr(cli, "maybe_run_scheduled_compaction", maintenance)
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    handler = Mock()
    root.commands["status"] = click.Command(
        "status", callback=handler, params=[click.Option(["--program"])],
    )

    result = CliRunner().invoke(root, ["status", "--program", "alpha", "--help"])

    assert result.exit_code == 0, result.output + repr(result.exception)
    handler.assert_not_called()
    sink_factory.assert_not_called()
    maintenance.assert_not_called()
