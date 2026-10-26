from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import Mock

import dotenv
import pytest
from typer.testing import CliRunner

from src.commands import diff, freshness, prep, report, review_full, triage


@pytest.mark.parametrize("command", ["freshness", "triage", "prep", "review-full", "diff"])
@pytest.mark.parametrize("conflict", [
    "reports", "archive", "relative-programs", "relative-reports", "relative-archive",
])
def test_installed_operational_conflict_precedes_startup_and_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, conflict: str,
) -> None:
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    cli = importlib.import_module("cli")
    module = {"freshness": freshness, "triage": triage, "prep": prep, "review-full": review_full, "diff": diff}[command]
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
    }.get(command, f"generate_{command}_report")
    monkeypatch.setattr(module, generation_name, dependencies[3])
    arguments = [command, "--edition", "fixture"]
    if command == "diff":
        arguments.extend(["--issue", "7"])
    result = CliRunner().invoke(cli.app, arguments)
    assert result.exit_code == 2, result.output + repr(result.exception)
    assert "workspace roots" in result.stderr
    assert not result.stdout
    for dependency in dependencies:
        dependency.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("command", ["freshness", "triage", "prep", "review-full", "diff"])
def test_installed_operational_preflight_uses_own_coherent_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str,
) -> None:
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    cli = importlib.import_module("cli")
    module = {"freshness": freshness, "triage": triage, "prep": prep, "review-full": review_full, "diff": diff}[command]
    for name in ("programs", "reports", "archive"):
        monkeypatch.setattr(module, name.upper() + "_ROOT", tmp_path / name, raising=False)
    monkeypatch.setattr(report, "REPORTS_ROOT", tmp_path / "unrelated" / "reports")
    hooks = [Mock() for _ in range(3)]
    for name, hook in zip(
        ("_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance", "_maybe_run_session_catchup"),
        hooks, strict=True,
    ):
        monkeypatch.setattr(cli, name, hook)
    generation = Mock(side_effect=RuntimeError("Selected generation boundary"))
    generation_name = {
        "prep": "generate_prep_brief", "review-full": "generate_review_full", "diff": "build_offline_diff_summary",
    }.get(command, f"generate_{command}_report")
    monkeypatch.setattr(module, generation_name, generation)
    arguments = [command, "--edition", "fixture"]
    if command == "diff":
        arguments.extend(["--issue", "7"])
    result = CliRunner().invoke(cli.app, arguments)
    assert isinstance(result.exception, RuntimeError), result.output
    assert str(result.exception) == "Selected generation boundary"
    generation.assert_called_once()
    for hook in hooks:
        hook.assert_called_once()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("command", ["freshness", "triage", "prep", "review-full", "diff"])
def test_unexpected_operational_preflight_errors_propagate_before_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str,
) -> None:
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    cli = importlib.import_module("cli")
    preflight = Mock(side_effect=RuntimeError("Unexpected preflight defect"))
    monkeypatch.setattr(cli, f"_preflight_{command.replace('-', '_')}_workspace", preflight, raising=False)
    forbidden = Mock(side_effect=AssertionError("No startup before preflight"))
    monkeypatch.setattr(cli, "_maybe_register_tier_decision_sink", forbidden)
    arguments = [command, "--edition", "fixture"]
    if command == "diff":
        arguments.extend(["--issue", "7"])
    result = CliRunner().invoke(cli.app, arguments)
    assert isinstance(result.exception, RuntimeError)
    assert str(result.exception) == "Unexpected preflight defect"
    preflight.assert_called_once()
    forbidden.assert_not_called()
    assert not list(tmp_path.iterdir())
