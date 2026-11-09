from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import click
from click.testing import CliRunner as ClickRunner
import pytest
import typer
from typer.main import get_command
from typer.testing import CliRunner

import cli
from src.ai.intent_router import IntentRouter, IntentRouterError, RoutedInvocation
from src.commands.ask import ask_command, route_request
from src.core.ledger.event_log import read_events


@pytest.fixture
def startup_spies(monkeypatch: pytest.MonkeyPatch) -> list[Mock]:
    spies = [Mock() for _ in range(4)]
    for name, spy in zip(
        (
            "_set_invocation_ai_mode",
            "_maybe_register_tier_decision_sink",
            "_maybe_run_scheduled_db_maintenance",
            "_maybe_run_session_catchup",
        ),
        spies,
        strict=True,
    ):
        monkeypatch.setattr(cli, name, spy)
    return spies


@pytest.mark.parametrize(
    "arguments",
    [
        ["ask", "show status"],
        ["ask", "--edition", "", "show status"],
        ["ask", "--program", "   ", "show status"],
        ["ask", "--miss-log", "--program=acme", "show status"],
        ["ask", "--edition"],
        ["ask", "--help"],
        ["show status"],
        ["--edition", " ", "show status"],
        ["--edition", "acme_weekly", "ask", "show status"],
    ],
)
def test_unscoped_ask_skips_root_startup(
    arguments: list[str], startup_spies: list[Mock]
) -> None:
    result = CliRunner().invoke(cli.app, arguments)

    assert result.exit_code == (0 if "--help" in arguments else 2), result.output
    for spy in startup_spies:
        spy.assert_not_called()
    if "show status" in arguments:
        assert "--edition" in result.output
        assert "--program" in result.output
        assert "list editions" in result.output


def test_unscoped_ai_fallback_does_not_call_provider_or_write_six_audit_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, startup_spies: list[Mock]
) -> None:
    root = tmp_path / "programs"
    provider = Mock()
    provider.structured.side_effect = lambda _system, _user, *, parser, **_kwargs: parser(
        {"command": "doctor", "args": ["--edition", "acme_weekly"], "warnings": []}
    )
    route = IntentRouter.route
    monkeypatch.setattr(
        IntentRouter,
        "route",
        lambda self, request, *, default_edition="": route(
            self, request, default_edition=default_edition, programs_root=root
        ),
    )
    monkeypatch.setattr("src.commands.ask._build_router", lambda: IntentRouter(client=provider))

    result = CliRunner().invoke(cli.app, ["ask", "do something custom"])

    records = [
        json.loads(line)
        for path in root.rglob("*.events.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert (result.exit_code, provider.structured.call_count, len(records)) == (2, 0, 0)
    for spy in startup_spies:
        spy.assert_not_called()


def test_unscoped_clustering_does_not_read_or_modify_miss_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, startup_spies: list[Mock]
) -> None:
    miss_log = tmp_path / "misses.jsonl"
    miss_log.write_bytes(b'{"question":"synthetic"}\n')
    cluster = Mock(return_value="unexpected")
    monkeypatch.setattr("src.commands.ask.cluster_misses", cluster)

    result = CliRunner().invoke(
        cli.app, ["ask", "--cluster-misses", "--miss-log", str(miss_log), "ignored"]
    )

    assert result.exit_code == 2, result.output
    cluster.assert_not_called()
    assert miss_log.read_bytes() == b'{"question":"synthetic"}\n'
    for spy in startup_spies:
        spy.assert_not_called()


def test_direct_unscoped_ask_refuses_before_clustering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = Mock(return_value="unexpected")
    monkeypatch.setattr("src.commands.ask.cluster_misses", cluster)

    with pytest.raises(typer.BadParameter, match="--edition.*--program"):
        ask_command(
            "ignored", edition="", program="", cluster_misses_flag=True, miss_log=tmp_path / "misses.jsonl"
        )

    cluster.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_ask_help_describes_the_scope_requirement(startup_spies: list[Mock]) -> None:
    result = CliRunner().invoke(cli.app, ["ask", "--help"])

    text = " ".join(result.output.split())
    assert result.exit_code == 0, result.output
    assert "required unless --program is set" in text
    assert "required unless --edition is set" in text
    for spy in startup_spies:
        spy.assert_not_called()


@pytest.mark.parametrize("cluster", [False, True])
def test_split_layout_is_reported_without_startup_or_private_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, startup_spies: list[Mock], cluster: bool,
) -> None:
    root = tmp_path / "programs"
    program = root / "alpha"
    editions = program / "editions"
    editions.mkdir(parents=True)
    (editions / "weekly.yaml").write_text(
        "id: weekly\nprogram_id: alpha\n", encoding="utf-8"
    )
    (program / "output" / "weekly").mkdir(parents=True)
    (program / "publications" / "weekly").mkdir(parents=True)
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", root)
    monkeypatch.setattr("src.ai.intent_router.PROGRAMS_ROOT", root)

    args = ["ask", "--edition", "weekly"]
    if cluster:
        args.append("--cluster-misses")
    result = CliRunner().invoke(cli.app, [*args, "list open risks"])

    assert result.exit_code == (2 if cluster else 1), result.output
    assert "layout" in result.output.lower()
    assert str(tmp_path) not in result.output
    for spy in startup_spies:
        spy.assert_not_called()


@pytest.mark.parametrize("program", ["..", "alpha\\other", "alpha:other", "alpha*"])
def test_invalid_program_scope_is_rejected_before_startup_or_clustering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, startup_spies: list[Mock], program: str
) -> None:
    cluster = Mock(return_value="unexpected")
    monkeypatch.setattr("src.commands.ask.cluster_misses", cluster)

    result = CliRunner().invoke(
        cli.app, ["ask", "--program", program, "--cluster-misses", "--miss-log", str(tmp_path / "misses"), "ignored"]
    )

    assert result.exit_code == 2, result.output
    assert "bare" in result.output
    cluster.assert_not_called()
    assert not list(tmp_path.iterdir())
    for spy in startup_spies:
        spy.assert_not_called()


@pytest.mark.parametrize("program", ["..", "alpha\\other", "alpha:other", "alpha*"])
def test_direct_ask_rejects_invalid_program_before_clustering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program: str
) -> None:
    cluster = Mock(return_value="unexpected")
    monkeypatch.setattr("src.commands.ask.cluster_misses", cluster)

    with pytest.raises(typer.BadParameter, match="bare"):
        ask_command(
            "ignored", edition="", program=program, cluster_misses_flag=True, miss_log=tmp_path / "misses"
        )

    cluster.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_direct_unscoped_route_refuses_before_building_router(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    build_router = Mock()
    monkeypatch.setattr("src.commands.ask._build_router", build_router)

    with pytest.raises(typer.BadParameter, match="--edition.*--program"):
        route_request("do something custom")

    build_router.assert_not_called()


@pytest.mark.parametrize("text", ["do something custom", "show status"])
def test_direct_ai_router_rejects_missing_edition_before_provider(
    tmp_path: Path, text: str
) -> None:
    provider = Mock()
    router = IntentRouter(client=provider)

    with pytest.raises(IntentRouterError, match="configured edition"):
        router.route(text, programs_root=tmp_path)

    provider.structured.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("use_ai", [False, True])
def test_edition_scope_uses_its_configured_program_for_routing_and_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, startup_spies: list[Mock], use_ai: bool
) -> None:
    root = tmp_path / "programs"
    edition_dir = root / "alpha_team" / "editions"
    edition_dir.mkdir(parents=True)
    (edition_dir / "launch_weekly.yaml").write_text(
        "id: launch_weekly\nprogram_id: alpha_team\n", encoding="utf-8"
    )
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", root)
    provider = Mock()
    provider.structured.side_effect = lambda _system, _user, *, parser, **_kwargs: parser(
        {"command": "doctor", "args": ["--edition", "launch_weekly"], "warnings": []}
    )
    route = IntentRouter.route
    monkeypatch.setattr(
        IntentRouter,
        "route",
        lambda self, request, *, default_edition="": route(
            self, request, default_edition=default_edition, programs_root=root
        ),
    )
    monkeypatch.setattr(
        "src.commands.ask._build_router",
        lambda: IntentRouter(client=provider if use_ai else None),
    )

    request = "do something custom" if use_ai else "list open risks"
    result = CliRunner().invoke(cli.app, ["ask", "--edition", "launch_weekly", request])

    assert result.exit_code == 0, result.output
    if use_ai:
        events = read_events("alpha_team", programs_root=root, repair_corruption=False)
        assert len(events) == 6
        assert any(event.event_type == "ai.release_decision.v1" for event in events)
        assert not (root / "launch").exists()
    else:
        assert "vertex risks list --program alpha_team" in result.output
    for spy in startup_spies:
        spy.assert_called_once()


@pytest.mark.parametrize("kind", ["missing", "ambiguous", "invalid"])
def test_unresolvable_edition_stops_before_provider_or_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, startup_spies: list[Mock], kind: str
) -> None:
    root = tmp_path / "programs"
    if kind == "ambiguous":
        for program in ("alpha", "beta"):
            edition_dir = root / program / "editions"
            edition_dir.mkdir(parents=True)
            (edition_dir / "weekly.yaml").write_text(
                f"id: weekly\nprogram_id: {program}\n", encoding="utf-8"
            )
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    provider = Mock()
    provider.structured.side_effect = lambda _system, _user, *, parser, **_kwargs: parser(
        {"command": "doctor", "args": ["--edition", "weekly"], "warnings": []}
    )
    route = IntentRouter.route
    monkeypatch.setattr(
        IntentRouter,
        "route",
        lambda self, request, *, default_edition="": route(
            self, request, default_edition=default_edition, programs_root=root
        ),
    )
    monkeypatch.setattr("src.commands.ask._build_router", lambda: IntentRouter(client=provider))
    edition = "invalid\\path" if kind == "invalid" else "weekly"
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", root)

    result = CliRunner().invoke(cli.app, ["ask", "--edition", edition, "do something custom"])

    assert result.exit_code == 1, result.output
    assert "edition" in result.output.lower()
    assert "list editions" in result.output
    provider.structured.assert_not_called()
    after = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert after == before
    for spy in startup_spies:
        spy.assert_not_called()


@pytest.mark.parametrize("kind", ["missing", "ambiguous", "invalid"])
def test_real_root_reports_unresolvable_edition_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    root = tmp_path / "programs"
    if kind == "ambiguous":
        for program in ("alpha", "beta"):
            edition_dir = root / program / "editions"
            edition_dir.mkdir(parents=True)
            (edition_dir / "weekly.yaml").write_text(
                f"id: weekly\nprogram_id: {program}\n", encoding="utf-8"
            )
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    edition = "invalid\\path" if kind == "invalid" else "weekly"
    provider = Mock()
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", root)
    monkeypatch.setattr("src.ai.intent_router.PROGRAMS_ROOT", root)
    monkeypatch.setattr(cli.sys, "argv", ["vertex", "ask", "--edition", edition, "do something custom"])
    monkeypatch.setattr("src.commands.ask._build_router", lambda: IntentRouter(client=provider))

    result = CliRunner().invoke(cli.app, ["ask", "--edition", edition, "do something custom"])

    assert result.exit_code == 1, result.output
    assert "edition" in result.output.lower()
    assert "list editions" in result.output
    provider.structured.assert_not_called()
    after = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert after == before


@pytest.mark.parametrize(
    "arguments",
    [
        ["ask", "--edition", "acme_weekly", "--help"],
        ["ask", "--edition", "acme_weekly", "question", "--help"],
    ],
)
def test_scoped_help_skips_unrelated_startup(arguments: list[str], startup_spies: list[Mock]) -> None:
    result = CliRunner().invoke(cli.app, arguments)

    assert result.exit_code == 0, result.output
    for spy in startup_spies:
        spy.assert_not_called()


def test_program_scoped_ask_preserves_named_intent_behavior(
    monkeypatch: pytest.MonkeyPatch, startup_spies: list[Mock]
) -> None:
    answer = Mock(return_value="synthetic answer")
    monkeypatch.setattr("src.commands.ask._answer_with_reality", answer)

    result = CliRunner().invoke(cli.app, ["ask", "--program", "acme", "question"])

    assert result.exit_code == 0, result.output
    assert "synthetic answer" in result.output
    answer.assert_called_once()
    assert answer.call_args.kwargs["program"] == "acme"
    for spy in startup_spies:
        spy.assert_called_once()


@pytest.mark.parametrize("kind", ["global_and_child_edition", "repeated_program", "root_fallback"])
def test_real_startup_uses_the_admitted_ask_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    root = tmp_path / "programs"
    for program in ("alpha", "beta"):
        directory = root / program / "editions"
        directory.mkdir(parents=True)
        (directory / f"{program}_weekly.yaml").write_text(
            f"id: {program}_weekly\nprogram_id: {program}\n", encoding="utf-8"
        )
    if kind == "global_and_child_edition":
        args = ["--edition", "alpha_weekly", "ask", "--edition", "beta_weekly", "list open risks"]
    elif kind == "repeated_program":
        args = ["ask", "--program", "alpha", "--program", "beta", "list open risks"]
    else:
        args = ["--edition", "beta_weekly", "list open risks"]

    monkeypatch.setattr(cli, "PROGRAMS_ROOT", root)
    monkeypatch.setattr("src.ai.intent_router.PROGRAMS_ROOT", root)
    monkeypatch.setattr(cli.sys, "argv", ["vertex", *args])
    monkeypatch.setattr(cli, "_REGISTERED_TIER_SINKS", set())
    monkeypatch.setattr(
        cli, "load_arch_data_fix", Mock(return_value=SimpleNamespace(mode=SimpleNamespace(value="off")))
    )
    make_sink = Mock(return_value=lambda _: None)
    compact = Mock()
    catchup = Mock()
    monkeypatch.setattr(cli, "make_tier_decision_sink", make_sink)
    monkeypatch.setattr(cli, "register_decision_sink", Mock())
    monkeypatch.setattr(cli, "maybe_run_scheduled_compaction", compact)
    monkeypatch.setattr(cli, "_stdout_supports_interactive_catchup", lambda: True)
    monkeypatch.setattr(cli, "maybe_catchup", catchup)
    monkeypatch.setattr(cli, "build_catchup_event_builder", Mock(return_value=None))
    monkeypatch.setattr("src.commands.ask._build_router", lambda: IntentRouter())
    answer = Mock(return_value="synthetic answer")
    monkeypatch.setattr("src.commands.ask._answer_with_reality", answer)

    result = CliRunner().invoke(cli.app, args)

    assert result.exit_code == 0, result.output
    if kind == "repeated_program":
        assert answer.call_args.kwargs["program"] == "beta"
        assert make_sink.call_args.args[0].edition_id is None
    else:
        assert "vertex risks list --program beta" in result.output
        assert make_sink.call_args.args[0].edition_id == "beta_weekly"
    assert make_sink.call_args.args[0].program_id == "beta"
    assert compact.call_args.args[0] == "beta"
    assert catchup.call_args.args[0] == "beta"


@pytest.mark.parametrize("mode", ["deterministic", "ai", "cluster", "root_fallback"])
def test_template_edition_is_refused_before_startup_provider_or_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, startup_spies: list[Mock], mode: str
) -> None:
    root = tmp_path / "programs"
    directory = root / "_templates" / "alpha" / "editions"
    directory.mkdir(parents=True)
    (directory / "launch_weekly.yaml").write_text(
        "id: launch_weekly\nprogram_id: template:alpha\n", encoding="utf-8"
    )
    (root / "alpha").mkdir()
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", root)
    monkeypatch.setattr("src.ai.intent_router.PROGRAMS_ROOT", root)
    provider = Mock()
    cluster = Mock()
    monkeypatch.setattr("src.commands.ask._build_router", lambda: IntentRouter(client=provider))
    monkeypatch.setattr("src.commands.ask.cluster_misses", cluster)
    if mode == "cluster":
        args = ["ask", "--edition", "launch_weekly", "--cluster-misses", "ignored"]
    elif mode == "root_fallback":
        args = ["--edition", "launch_weekly", "list open risks"]
    else:
        request = "list open risks" if mode == "deterministic" else "do something custom"
        args = ["ask", "--edition", "launch_weekly", request]

    result = CliRunner().invoke(cli.app, args)

    assert result.exit_code == (2 if mode == "cluster" else 1), result.output
    assert "template" in result.output.lower()
    provider.structured.assert_not_called()
    cluster.assert_not_called()
    assert {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before
    for spy in startup_spies:
        spy.assert_not_called()


@pytest.mark.parametrize("text", ["list open risks", "do something custom"])
def test_direct_router_refuses_template_edition_before_provider(
    tmp_path: Path, text: str
) -> None:
    root = tmp_path / "programs"
    directory = root / "_templates" / "alpha" / "editions"
    directory.mkdir(parents=True)
    (directory / "launch_weekly.yaml").write_text(
        "id: launch_weekly\nprogram_id: template:alpha\n", encoding="utf-8"
    )
    provider = Mock()

    with pytest.raises(IntentRouterError, match="Template"):
        IntentRouter(client=provider).route(text, default_edition="launch_weekly", programs_root=root)

    provider.structured.assert_not_called()
    assert not (root / "alpha").exists()
    assert not (root / "_templates" / "alpha" / "ledger").exists()


@pytest.mark.parametrize("kind", ["missing", "template"])
def test_direct_clustering_rejects_unusable_edition_before_reading_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    root = tmp_path / "programs"
    if kind == "template":
        directory = root / "_templates" / "alpha" / "editions"
        directory.mkdir(parents=True)
        (directory / "weekly.yaml").write_text(
            "id: weekly\nprogram_id: template:alpha\n", encoding="utf-8"
        )
    monkeypatch.setattr("src.ai.intent_router.PROGRAMS_ROOT", root)
    cluster = Mock()
    monkeypatch.setattr("src.commands.ask.cluster_misses", cluster)

    with pytest.raises(typer.BadParameter, match="Template|Unknown edition"):
        ask_command(
            "ignored", edition="weekly", program="", cluster_misses_flag=True, miss_log=tmp_path / "misses"
        )

    cluster.assert_not_called()
    assert not (tmp_path / "misses").exists()


@pytest.mark.parametrize("scope", [["--program", "acme"], ["--edition", "acme_weekly"]])
def test_scoped_clustering_preserves_startup_and_maintenance_behavior(
    scope: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, startup_spies: list[Mock]
) -> None:
    edition_dir = tmp_path / "acme" / "editions"
    edition_dir.mkdir(parents=True)
    (edition_dir / "acme_weekly.yaml").write_text(
        "id: acme_weekly\nprogram_id: acme\n", encoding="utf-8"
    )
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr("src.ai.intent_router.PROGRAMS_ROOT", tmp_path)
    cluster = Mock(return_value="synthetic clusters")
    monkeypatch.setattr("src.commands.ask.cluster_misses", cluster)

    result = CliRunner().invoke(cli.app, ["ask", *scope, "--cluster-misses", "ignored"])

    assert result.exit_code == 0, result.output
    assert "synthetic clusters" in result.output
    cluster.assert_called_once()
    for spy in startup_spies:
        spy.assert_called_once()


def test_scope_probe_does_not_convert_path_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, startup_spies: list[Mock]
) -> None:
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    edition_dir = tmp_path / "acme" / "editions"
    edition_dir.mkdir(parents=True)
    (edition_dir / "acme_weekly.yaml").write_text(
        "id: acme_weekly\nprogram_id: acme\n", encoding="utf-8"
    )
    monkeypatch.setattr(cli, "PROGRAMS_ROOT", tmp_path)
    conversions: list[str] = []
    convert = click.Path.convert

    def checked_convert(
        self: click.Path, value: object, param: click.Parameter | None, ctx: click.Context | None
    ) -> object:
        assert ctx is not None
        assert ctx.help_option_names != [], "Scope preflight must not inspect paths"
        conversions.append(str(value))
        return convert(self, value, param, ctx)

    monkeypatch.setattr(click.Path, "convert", checked_convert)
    router = Mock()
    router.route.return_value = RoutedInvocation(
        command="next", args=("--edition", "acme_weekly"), warnings=(), prompt_version=None
    )
    monkeypatch.setattr("src.commands.ask._build_router", lambda: router)
    result = ClickRunner().invoke(
        root, ["ask", "--edition", "acme_weekly", "--miss-log", str(tmp_path / "misses.jsonl"), "next"]
    )

    assert result.exit_code == 0, result.output
    assert len(conversions) == 1
    for spy in startup_spies:
        spy.assert_called_once()
