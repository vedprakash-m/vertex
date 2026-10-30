from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import Mock

import dotenv
import pytest
import typer
from typer.testing import CliRunner

from src.commands import diff, edit, freshness, override, prep, report, review_full, review_sections, triage


@pytest.fixture(scope="module")
def freshness_prep_external_homes(tmp_path_factory):
    from datetime import datetime, timezone
    import json
    import shutil
    from src.commands.report import generate_report_draft
    from tests.unit.test_commands_report import _sample_items
    from tests.unit.test_prep_brief import _seed_v2_report_layout

    root = tmp_path_factory.mktemp("freshness-prep-external")
    installation = Path(__file__).resolve().parents[2]
    selected, cwd = root / "selected", root / "misleading-cwd"
    for home in (selected, cwd, installation):
        fixture = root / "installation-fixture" if home == installation else home
        fixture.mkdir(parents=True, exist_ok=True)
        reports, archive = _seed_v2_report_layout(
            fixture, fixture, edition_names=("acme_weekly", "example_lt_deck"),
        )
        generate_report_draft(
            edition_name="example_lt_deck", reports_root=reports, archive_root=archive,
            programs_root=fixture / "programs",
            as_of=datetime(2026, 5, 5, 18, tzinfo=timezone.utc),
            work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
            kusto_query_executor=lambda query: [], open_browser=False,
        )
        for edition in ("acme_weekly", "example_lt_deck"):
            index = fixture / "programs" / "acme" / "archive" / edition / "index.json"
            index.parent.mkdir(parents=True, exist_ok=True)
            index.write_text(json.dumps({
                "edition": edition,
                "issues": [] if home != cwd else [{
                    "issue_number": 20, "generated_at": "2026-01-01T00:00:00+00:00",
                    "kind": "confirmed",
                }],
            }), encoding="utf-8")
        if home == installation:
            shutil.copytree(fixture / "programs", installation / "programs", dirs_exist_ok=True)
    return selected, cwd, installation


@pytest.mark.parametrize("command", ("freshness", "prep"))
@pytest.mark.parametrize("entrypoint", ("preflight", "direct", "cli", "legacy", "captured"))
def test_freshness_prep_actual_external_and_nested_legacy(
    freshness_prep_external_homes, command: str, entrypoint: str,
) -> None:
    import hashlib
    import json
    import os
    import subprocess
    import sys

    selected, cwd, installation = freshness_prep_external_homes
    home = installation if entrypoint == "legacy" else selected

    def protected():
        roots = [cwd, selected / "programs" / "acme" / "archive",
                 installation / "programs" / "acme" / "archive"]
        if home != installation:
            roots.append(installation / "programs")
        return {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for root in roots for path in root.rglob("*") if path.is_file()
        }

    before = protected()
    env = dict(os.environ)
    env.pop("VERTEX_PROGRAMS_ROOT", None)
    env["PYTHONPATH"] = os.pathsep.join((str(installation), env.get("PYTHONPATH", "")))
    if entrypoint != "legacy":
        env["VERTEX_PROGRAMS_ROOT"] = str(selected / "programs")
    code = f"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from src.commands import {command} as command
from src.ai.ai_mode import AIMode, set_ai_mode
set_ai_mode(AIMode.DISABLED)
from tests.unit.test_commands_freshness import _sample_items
if {entrypoint!r} == "captured":
    os.environ["VERTEX_PROGRAMS_ROOT"] = {str(cwd / "programs")!r}
if {command!r} == "freshness":
    command._load_live_freshness_items = lambda bundle, timestamp, since: (_sample_items(timestamp), 0)
    def forbidden_notification(*args, **kwargs):
        raise AssertionError("Dry-run must not record a notification")
    command.append_confirmed_notify_run = forbidden_notification
else:
    import src.core.engms_content
    src.core.engms_content.fetch_engms_page_summary = lambda *args, **kwargs: None
if {entrypoint!r} == "preflight":
    command._preflight_{command}_workspace()
elif {entrypoint!r} == "cli":
    from cli import app
    app()
else:
    if {command!r} == "freshness":
        result = command.generate_freshness_report(
            edition_name="acme_weekly", notify=True, dry_run=True,
            as_of=datetime(2026, 5, 5, 18, tzinfo=timezone.utc),
        )
        assert result.notify_previews
        path = result.md_path
    else:
        result = command.generate_prep_brief(
            edition_name="example_lt_deck",
            as_of=datetime(2026, 5, 5, 18, tzinfo=timezone.utc),
        )
        path = result.markdown_path
    assert result.issue_number == 1
    assert path.is_relative_to(Path({str(home / "programs")!r})), path
    assert path.exists()
    print(json.dumps({{"issue_number": result.issue_number, "path": str(path)}}))
"""
    arguments = []
    if entrypoint == "cli":
        arguments = [
            "--no-catchup", command, "--edition",
            "acme_weekly" if command == "freshness" else "example_lt_deck",
            "--format", "json",
        ]
        if command == "freshness":
            arguments.extend(["--notify", "--dry-run"])
    result = subprocess.run(
        [sys.executable, "-c", code, *arguments], cwd=cwd, env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert protected() == before
    assert result.returncode in ((0, 2, 3) if command == "freshness" and entrypoint == "cli" else (0,)), result.stderr
    if entrypoint != "preflight":
        payload = json.loads(result.stdout)
        assert payload["issue_number"] == 1
    assert not list(home.rglob("notify_runs.jsonl"))


@pytest.mark.parametrize("command", ("freshness", "prep"))
def test_freshness_prep_actual_nested_unset_legacy_in_process(
    freshness_prep_external_homes, monkeypatch: pytest.MonkeyPatch, command: str,
) -> None:
    from datetime import datetime, timezone
    from src.ai.ai_mode import AIMode, get_ai_mode, set_ai_mode
    from tests.unit.test_commands_freshness import _sample_items

    _, cwd, installation = freshness_prep_external_homes
    module = freshness if command == "freshness" else prep
    for name in ("programs", "reports", "archive"):
        monkeypatch.setattr(module, name.upper() + "_ROOT", installation / name)
    monkeypatch.setattr("src.core.workspace_paths._PROGRAMS_ROOT_CONFIGURED", False)
    monkeypatch.chdir(cwd)
    monkeypatch.setattr("src.core.engms_content.fetch_engms_page_summary", lambda *args, **kwargs: None)
    previous_mode = get_ai_mode()
    set_ai_mode(AIMode.DISABLED)
    try:
        if command == "freshness":
            result = freshness.generate_freshness_report(
                edition_name="acme_weekly", notify=True, dry_run=True,
                as_of=datetime(2026, 5, 5, 18, tzinfo=timezone.utc),
                work_item_loader=lambda bundle, timestamp, since: (_sample_items(timestamp), 0),
            )
            path = result.md_path
            assert result.notify_previews
        else:
            result = prep.generate_prep_brief(
                edition_name="example_lt_deck",
                as_of=datetime(2026, 5, 5, 18, tzinfo=timezone.utc),
            )
            path = result.markdown_path
    finally:
        set_ai_mode(previous_mode)
    assert result.issue_number == 1
    assert path.is_relative_to(installation / "programs")
    assert path.exists()


@pytest.mark.parametrize("command", ("freshness", "prep"))
@pytest.mark.parametrize("selection", ("relative", "blank", "installation", "checkout", "linked-checkout"))
def test_freshness_prep_installed_unsafe_roots_fail_before_effects(
    tmp_path: Path, command: str, selection: str,
) -> None:
    import os
    import subprocess
    import sys

    root = tmp_path / "programs"
    if selection == "relative":
        root = Path("programs")
    elif selection == "blank":
        root = ""
    elif selection == "installation":
        root = Path(__file__).resolve().parents[2] / "programs"
    else:
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / ".git").write_text("gitdir: synthetic", encoding="utf-8")
        root = checkout / "programs"
        if selection == "linked-checkout":
            link = tmp_path / "linked"
            if sys.platform == "win32":
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(checkout)],
                    check=True, capture_output=True,
                )
            else:
                link.symlink_to(checkout, target_is_directory=True)
            root = link / "programs"
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    result = subprocess.run(
        [sys.executable, "-c", """
import cli
def forbidden(*args, **kwargs):
    raise AssertionError("Unsafe workspace reached startup or generation")
for name in ("_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance", "_maybe_run_session_catchup"):
    setattr(cli, name, forbidden)
from src.commands import freshness, prep
freshness.generate_freshness_report = forbidden
prep.generate_prep_brief = forbidden
cli.app()
""", "--no-catchup", command, "--edition", "fixture"],
        cwd=tmp_path, env=dict(os.environ, VERTEX_PROGRAMS_ROOT=str(root)),
        capture_output=True, text=True, timeout=90,
    )
    assert result.returncode in (1, 2)
    assert "workspace" in result.stderr or "VERTEX_PROGRAMS_ROOT" in result.stderr
    assert "Unsafe workspace reached" not in result.stderr
    assert sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")) == before


def test_authoring_preflights_do_not_intercept_offline_config_help(monkeypatch) -> None:
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    cli = importlib.import_module("cli")
    forbidden = Mock(side_effect=AssertionError("Unrelated offline command must retain its routing"))
    for name in (
        "_preflight_edit_workspace", "_preflight_override_workspace", "_maybe_register_tier_decision_sink",
        "_maybe_run_scheduled_db_maintenance", "_maybe_run_session_catchup", "_preflight_review_sections_workspace",
    ):
        monkeypatch.setattr(cli, name, forbidden, raising=False)

    result = CliRunner().invoke(cli.app, ["config", "--help"])

    assert result.exit_code == 0, result.output + repr(result.exception)
    forbidden.assert_not_called()


@pytest.mark.parametrize("command", ["edit", "override"])
def test_authoring_handlers_preflight_before_direct_dispatch(tmp_path: Path, monkeypatch, capsys, command: str) -> None:
    module = edit if command == "edit" else override
    for name in ("programs", "reports", "archive"):
        monkeypatch.setattr(module, name.upper() + "_ROOT", tmp_path / name)
    monkeypatch.setattr(module, "PROGRAMS_ROOT", tmp_path / "conflicting")
    dispatch = Mock(side_effect=AssertionError("Must reject before direct dispatch"))
    monkeypatch.setattr(module, "run_" + command, dispatch)
    arguments = {"edition": "fixture"}
    if command == "edit":
        arguments["section"] = "exec_summary"

    with pytest.raises(typer.Exit) as error:
        getattr(module, command + "_command")(**arguments)

    assert error.value.exit_code == 2
    output = capsys.readouterr()
    assert "workspace roots" in output.err
    assert not output.out
    dispatch.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("subcommand", ("show", "set", "clear", "export"))
@pytest.mark.parametrize("selection", ("programs", "reports", "archive", "relative-programs", "relative-reports", "relative-archive"))
def test_review_sections_installed_admission_precedes_startup_and_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, subcommand: str, selection: str,
) -> None:
    cli = importlib.import_module("cli")
    for name in ("programs", "reports", "archive"):
        root = tmp_path / name
        if selection == name:
            root = tmp_path / "conflicting" / name
        elif selection == f"relative-{name}":
            root = Path(name)
        monkeypatch.setattr(review_sections, name.upper() + "_ROOT", root)
    forbidden = Mock(side_effect=AssertionError("Review-sections admission must precede effects"))
    for name in ("_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance", "_maybe_run_session_catchup"):
        monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr(review_sections, "_load_review_context", forbidden)
    monkeypatch.setattr(review_sections, "export_review_section", forbidden)
    arguments = ["review-sections", subcommand, "--edition", "fixture"]
    if subcommand != "show":
        arguments.extend(["--section", "ws:fixture" if subcommand == "export" else "exec_summary"])
    if subcommand == "set":
        arguments.extend(["--state", "approved"])
    result = CliRunner().invoke(cli.app, arguments)
    assert result.exit_code == 2, result.output + repr(result.exception)
    assert "workspace roots" in result.stderr
    assert not result.stdout
    forbidden.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("subcommand", ("show", "set", "clear", "export"))
def test_review_sections_installed_admission_uses_own_roots_and_preserves_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, subcommand: str,
) -> None:
    cli = importlib.import_module("cli")
    for name in ("programs", "reports", "archive"):
        monkeypatch.setattr(review_sections, name.upper() + "_ROOT", tmp_path / name)
        for module in (report, review_full):
            monkeypatch.setattr(module, name.upper() + "_ROOT", tmp_path / "unrelated" / name)
    order = []
    for name in ("_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance", "_maybe_run_session_catchup"):
        monkeypatch.setattr(cli, name, lambda *args, _name=name, **kwargs: order.append(_name))

    def dispatch(*args, **kwargs):
        order.append("dispatch")
        raise RuntimeError("Selected review-section dispatch")

    monkeypatch.setattr(review_sections, "_load_review_context", dispatch)
    monkeypatch.setattr(review_sections, "export_review_section", dispatch)
    arguments = ["review-sections", subcommand, "--edition", "fixture"]
    if subcommand != "show":
        arguments.extend(["--section", "ws:fixture" if subcommand == "export" else "exec_summary"])
    if subcommand == "set":
        arguments.extend(["--state", "pending"])
    result = CliRunner().invoke(cli.app, arguments)
    assert isinstance(result.exception, RuntimeError), result.output + repr(result.exception)
    assert str(result.exception) == "Selected review-section dispatch"
    assert order == [
        "_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance",
        "_maybe_run_session_catchup", "dispatch",
    ]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("subcommand", ("show", "set", "clear", "export"))
def test_review_sections_installed_unexpected_admission_error_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, subcommand: str,
) -> None:
    cli = importlib.import_module("cli")
    preflight = Mock(side_effect=RuntimeError("Unexpected review-section preflight defect"))
    monkeypatch.setattr(cli, "_preflight_review_sections_workspace", preflight, raising=False)
    forbidden = Mock(side_effect=AssertionError("No effects before admission"))
    for name in ("_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance", "_maybe_run_session_catchup"):
        monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr(review_sections, "_load_review_context", forbidden)
    monkeypatch.setattr(review_sections, "export_review_section", forbidden)
    result = CliRunner().invoke(cli.app, ["review-sections", subcommand])
    assert isinstance(result.exception, RuntimeError), result.output + repr(result.exception)
    assert str(result.exception) == "Unexpected review-section preflight defect"
    preflight.assert_called_once_with()
    forbidden.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_review_sections_preflight_preserves_unexpected_mapper_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(review_sections, "resolve_report_workspace_roots", Mock(side_effect=RuntimeError("mapper defect")))
    with pytest.raises(RuntimeError, match="mapper defect"):
        review_sections._preflight_review_sections_workspace()


@pytest.mark.parametrize("arguments", [
    ["setup", "--demo"], ["setup", "--dry-run", "--manual"],
    ["setup", "--manual"], ["onboard", "--edition", "synthetic_weekly"],
])
def test_creation_never_runs_unscoped_startup_hooks(tmp_path: Path, monkeypatch, arguments: list[str]) -> None:
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    cli = importlib.import_module("cli")
    import src.commands.onboard as onboard
    import src.commands.setup as setup
    from src.core.workspace_paths import REPO_ROOT

    monkeypatch.setattr(onboard, "PROGRAMS_ROOT", REPO_ROOT / "programs", raising=False)
    hooks = [Mock(side_effect=AssertionError("Onboarding must not access unrelated startup state.")) for _ in range(3)]
    for name, hook in zip(
        ("_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance", "_maybe_run_session_catchup"),
        hooks, strict=True,
    ):
        monkeypatch.setattr(cli, name, hook)
    monkeypatch.setattr(setup, "_collect_identity", Mock(side_effect=RuntimeError("Offline collection boundary")))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.app, arguments)
    if "--demo" in arguments:
        assert result.exit_code == 0, result.output + repr(result.exception)
    elif "--dry-run" in arguments:
        assert isinstance(result.exception, RuntimeError), result.output + repr(result.exception)
        assert str(result.exception) == "Offline collection boundary"
    else:
        assert result.exit_code == 2, result.output + repr(result.exception)
        assert "workspace" in result.output
    for hook in hooks:
        hook.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("command", ["freshness", "triage", "prep", "review-full", "diff", "edit", "override", "apply-overrides"])
@pytest.mark.parametrize("conflict", [
    "reports", "archive", "relative-programs", "relative-reports", "relative-archive",
])
def test_installed_operational_conflict_precedes_startup_and_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, conflict: str,
) -> None:
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    cli = importlib.import_module("cli")
    module = {"freshness": freshness, "triage": triage, "prep": prep, "review-full": review_full, "diff": diff, "edit": edit, "override": override, "apply-overrides": report}[command]
    roots = {name: tmp_path / name for name in ("programs", "reports", "archive")}
    if conflict.startswith("relative-"):
        name = conflict.removeprefix("relative-")
        roots[name] = Path(name)
    else:
        roots[conflict] = tmp_path / "other" / conflict
    for name, path in roots.items():
        monkeypatch.setattr(module, name.upper() + "_ROOT", path, raising=False)
    dependencies = [Mock(side_effect=AssertionError("Workspace preflight must precede effects.")) for _ in range(4)]
    for name, dependency in zip(
        ("_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance", "_maybe_run_session_catchup"),
        dependencies[:3], strict=True,
    ):
        monkeypatch.setattr(cli, name, dependency)
    generation_name = {
        "prep": "generate_prep_brief", "review-full": "generate_review_full", "diff": "build_offline_diff_summary",
        "edit": "run_edit", "override": "run_override", "apply-overrides": "get_program_output_dir",
    }.get(command, f"generate_{command}_report")
    monkeypatch.setattr(module, generation_name, dependencies[3])
    arguments = [command, "--edition", "fixture"]
    if command == "diff":
        arguments.extend(["--issue", "7"])
    elif command == "edit":
        arguments.extend(["--section", "exec_summary"])
    result = CliRunner().invoke(cli.app, arguments)
    assert result.exit_code == 2, result.output + repr(result.exception)
    assert "workspace roots" in result.stderr
    assert not result.stdout
    for dependency in dependencies:
        dependency.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("command", ["freshness", "triage", "prep", "review-full", "diff", "edit", "override", "apply-overrides"])
def test_installed_operational_preflight_uses_own_coherent_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str,
) -> None:
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    cli = importlib.import_module("cli")
    module = {"freshness": freshness, "triage": triage, "prep": prep, "review-full": review_full, "diff": diff, "edit": edit, "override": override, "apply-overrides": report}[command]
    for name in ("programs", "reports", "archive"):
        monkeypatch.setattr(module, name.upper() + "_ROOT", tmp_path / name, raising=False)
    unrelated = review_full if module is report else report
    monkeypatch.setattr(unrelated, "REPORTS_ROOT", tmp_path / "unrelated" / "reports")
    hooks = [Mock() for _ in range(3)]
    for name, hook in zip(
        ("_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance", "_maybe_run_session_catchup"),
        hooks, strict=True,
    ):
        monkeypatch.setattr(cli, name, hook)
    generation = Mock(side_effect=RuntimeError("Selected generation boundary"))
    generation_name = {
        "prep": "generate_prep_brief", "review-full": "generate_review_full", "diff": "build_offline_diff_summary",
        "edit": "run_edit", "override": "run_override", "apply-overrides": "get_program_output_dir",
    }.get(command, f"generate_{command}_report")
    monkeypatch.setattr(module, generation_name, generation)
    arguments = [command, "--edition", "fixture"]
    if command == "diff":
        arguments.extend(["--issue", "7"])
    elif command == "edit":
        arguments.extend(["--section", "exec_summary"])
    result = CliRunner().invoke(cli.app, arguments)
    assert isinstance(result.exception, RuntimeError), result.output
    assert str(result.exception) == "Selected generation boundary"
    generation.assert_called_once()
    for hook in hooks:
        hook.assert_called_once()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("command", ["freshness", "triage", "prep", "review-full", "diff", "edit", "override", "apply-overrides"])
def test_unexpected_operational_preflight_errors_propagate_before_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str,
) -> None:
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    cli = importlib.import_module("cli")
    preflight = Mock(side_effect=RuntimeError("Unexpected preflight defect"))
    preflight_command = "report" if command == "apply-overrides" else command.replace("-", "_")
    monkeypatch.setattr(cli, f"_preflight_{preflight_command}_workspace", preflight, raising=False)
    forbidden = Mock(side_effect=AssertionError("No startup before preflight"))
    monkeypatch.setattr(cli, "_maybe_register_tier_decision_sink", forbidden)
    arguments = [command, "--edition", "fixture"]
    if command == "diff":
        arguments.extend(["--issue", "7"])
    elif command == "edit":
        arguments.extend(["--section", "exec_summary"])
    result = CliRunner().invoke(cli.app, arguments)
    assert isinstance(result.exception, RuntimeError)
    assert str(result.exception) == "Unexpected preflight defect"
    preflight.assert_called_once()
    forbidden.assert_not_called()
    assert not list(tmp_path.iterdir())
