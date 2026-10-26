from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import Mock

import dotenv
import pytest
import typer
from typer.testing import CliRunner

from src.commands import confirm, report
from src.core.exceptions import ConfigError
from src.core.models import ArchiveIndex
from src.core.overrides_store import OverridesDocument


def _invoke_api(api: str, reports_root: Path, archive_root: Path, **kwargs) -> None:
    if api == "confirm":
        confirm.confirm_issue(
            "fixture", 7, dry_run=True, reports_root=reports_root, archive_root=archive_root, **kwargs,
        )
    else:
        confirm._build_confirm_artifacts(
            "fixture", 7, None, OverridesDocument(issue_number=7, top_3_now=(), scorecards=()),
            {}, reports_root, archive_root, **kwargs,
        )


@pytest.mark.parametrize("api", ["confirm", "artifacts"])
@pytest.mark.parametrize("conflict", ["reports", "archive", "relative-programs", "relative-reports", "relative-archive"])
def test_conflicting_or_relative_confirm_roots_stop_before_stores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, api: str, conflict: str,
) -> None:
    roots = {"programs_root": tmp_path / "programs", "reports_root": tmp_path / "reports", "archive_root": tmp_path / "archive"}
    if conflict.startswith("relative-"):
        key = conflict.removeprefix("relative-") + "_root"
        roots[key] = Path(key)
    else:
        roots[conflict + "_root"] = tmp_path / "other" / conflict
    forbidden = Mock(side_effect=AssertionError("Workspace validation must precede stores."))
    for name in ("load_bundle", "_assert_state_authority_for_confirm", "_load_draft_state", "_load_confirm_draft_manifest", "resolve_edition"):
        monkeypatch.setattr(confirm, name, forbidden)
    with pytest.raises(ConfigError):
        _invoke_api(api, **roots)
    forbidden.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("api", ["confirm", "artifacts"])
@pytest.mark.parametrize("selection", ["omitted", "none"])
def test_default_program_root_is_not_rederived_from_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, api: str, selection: str,
) -> None:
    monkeypatch.setattr(confirm, "_CONFIRM_PROGRAMS_ROOT", tmp_path / "selected" / "programs")
    forbidden = Mock(side_effect=AssertionError("A conflicting shared root must not be replaced."))
    monkeypatch.setattr(confirm, "load_bundle", forbidden)
    monkeypatch.setattr(confirm, "_load_confirm_draft_manifest", forbidden)
    kwargs = {"programs_root": None} if selection == "none" else {}
    with pytest.raises(ConfigError):
        _invoke_api(api, tmp_path / "reports", tmp_path / "archive", **kwargs)
    forbidden.assert_not_called()


@pytest.mark.parametrize("api", ["confirm", "artifacts"])
@pytest.mark.parametrize("selection", ["omitted", "none", "explicit"])
def test_coherent_confirm_context_preserves_shared_or_explicit_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, api: str, selection: str,
) -> None:
    root = tmp_path / "programs"
    monkeypatch.setattr(confirm, "_CONFIRM_PROGRAMS_ROOT", root if selection != "explicit" else tmp_path / "other" / "programs")
    dependency = Mock(side_effect=RuntimeError("stop at selected evidence boundary"))
    monkeypatch.setattr(confirm, "load_bundle" if api == "confirm" else "_load_confirm_draft_manifest", dependency)
    kwargs = {} if selection == "omitted" else {"programs_root": root if selection == "explicit" else None}
    with pytest.raises(RuntimeError, match="selected evidence boundary"):
        _invoke_api(api, tmp_path / "reports", tmp_path / "archive", **kwargs)
    assert dependency.call_args.kwargs["programs_root"] == root
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("installed", [False, True])
@pytest.mark.parametrize("conflict", ["reports", "archive"])
@pytest.mark.parametrize("mode", ["--dry-run", "--force"])
def test_confirm_cli_conflict_precedes_banner_archive_and_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installed: bool, conflict: str, mode: str,
) -> None:
    monkeypatch.setattr(confirm, "_CONFIRM_PROGRAMS_ROOT", tmp_path / "programs")
    monkeypatch.setattr(confirm, "REPORTS_ROOT", tmp_path / ("other" if conflict == "reports" else "") / "reports")
    monkeypatch.setattr(confirm, "ARCHIVE_ROOT", tmp_path / ("other" if conflict == "archive" else "") / "archive")
    dependencies = [Mock(side_effect=AssertionError("Conflict must precede any store/startup.")) for _ in range(5)]
    monkeypatch.setattr(confirm, "_resolve_edition_paths", dependencies[0])
    monkeypatch.setattr(confirm, "read_archive_index", dependencies[1])
    if installed:
        monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
        cli = importlib.import_module("cli")
        for name, dependency in zip(
            ("_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance", "_maybe_run_session_catchup"),
            dependencies[2:], strict=True,
        ):
            monkeypatch.setattr(cli, name, dependency)
        app = cli.app
    else:
        app = typer.Typer()
        app.command("confirm")(confirm.confirm_command)
        app.callback()(lambda: None)
    result = CliRunner().invoke(app, ["confirm", "--edition", "fixture", "--issue", "7", mode])
    assert result.exit_code == 2, result.output + repr(result.exception)
    assert "Conflicting report workspace roots" in result.stderr
    assert not result.stdout
    for dependency in dependencies:
        dependency.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_installed_confirm_preflight_uses_confirm_defaults_not_report_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    cli = importlib.import_module("cli")
    monkeypatch.setattr(confirm, "_CONFIRM_PROGRAMS_ROOT", tmp_path / "programs")
    monkeypatch.setattr(confirm, "REPORTS_ROOT", tmp_path / "reports")
    monkeypatch.setattr(confirm, "ARCHIVE_ROOT", tmp_path / "archive")
    monkeypatch.setattr(report, "REPORTS_ROOT", tmp_path / "unrelated" / "reports")
    hooks = [Mock() for _ in range(3)]
    for name, hook in zip(
        ("_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance", "_maybe_run_session_catchup"),
        hooks, strict=True,
    ):
        monkeypatch.setattr(cli, name, hook)
    monkeypatch.setattr(confirm, "_resolve_edition_paths", Mock(return_value=None))
    monkeypatch.setattr(confirm, "read_archive_index", Mock(side_effect=RuntimeError("stop before archive IO")))
    result = CliRunner().invoke(cli.app, ["confirm", "--edition", "fixture", "--issue", "7", "--dry-run"])
    assert isinstance(result.exception, RuntimeError)
    assert str(result.exception) == "stop before archive IO"
    for hook in hooks:
        hook.assert_called_once()


@pytest.mark.parametrize("force", [False, True])
def test_non_dry_confirm_cannot_bypass_invalid_context_with_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, force: bool,
) -> None:
    forbidden = Mock(side_effect=AssertionError("Invalid context must precede authority/storage."))
    monkeypatch.setattr(confirm, "load_bundle", forbidden)
    monkeypatch.setattr(confirm, "_assert_state_authority_for_confirm", forbidden)
    with pytest.raises(ConfigError):
        confirm.confirm_issue(
            "fixture", 7, dry_run=False, force=force, programs_root=tmp_path / "programs",
            reports_root=tmp_path / "reports", archive_root=tmp_path / "other" / "archive",
        )
    forbidden.assert_not_called()


def test_later_confirm_context_error_is_stderr_nonzero_not_success_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(confirm, "_CONFIRM_PROGRAMS_ROOT", tmp_path / "programs")
    monkeypatch.setattr(confirm, "REPORTS_ROOT", tmp_path / "reports")
    monkeypatch.setattr(confirm, "ARCHIVE_ROOT", tmp_path / "archive")
    monkeypatch.setattr(confirm, "_resolve_edition_paths", Mock(return_value=None))
    monkeypatch.setattr(confirm, "read_archive_index", Mock(return_value=ArchiveIndex(edition="fixture", issues=())))
    monkeypatch.setattr(confirm, "archive_integrity_waived", lambda: True)
    monkeypatch.setattr(confirm, "confirm_issue", Mock(side_effect=ConfigError("selected context unavailable")))
    app = typer.Typer()
    app.command("confirm")(confirm.confirm_command)
    app.callback()(lambda: None)
    result = CliRunner().invoke(app, ["confirm", "--edition", "fixture", "--issue", "7", "--dry-run"])
    assert result.exit_code == 2
    assert "selected context unavailable" in result.stderr
    assert not result.stdout
