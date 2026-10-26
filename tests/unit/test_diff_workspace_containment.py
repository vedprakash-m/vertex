from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import typer

from src.commands import diff
from src.core import workspace_paths
from src.core.exceptions import ConfigError, StateError


def _forbidden_access(monkeypatch: pytest.MonkeyPatch) -> dict[str, Mock]:
    dependencies = {}
    for name in (
        "_latest_draft_issue_number", "_load_draft_state", "_load_previous_dry_run_state",
        "load_bundle", "_resolve_confirmed_reference", "read_snapshot", "load_overrides",
        "_build_scorecard_packets", "_build_scorecard_data", "load_scorecard_trends",
        "load_narratives", "load_archived_narratives", "_load_report_signal_context",
    ):
        dependencies[name] = Mock(side_effect=AssertionError(f"Forbidden access: {name}"))
        monkeypatch.setattr(diff, name, dependencies[name])
    return dependencies


@pytest.mark.parametrize("mode", ["offline", "publish"])
@pytest.mark.parametrize("invalid", ["program-relative", "report-relative", "program-conflict", "report-conflict"])
@pytest.mark.parametrize("issue", [None, 7])
def test_diff_api_rejects_invalid_roots_before_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, invalid: str, issue: int | None,
) -> None:
    roots = {"programs_root": tmp_path / "programs", "reports_root": tmp_path / "reports"}
    name = "programs_root" if invalid.startswith("program") else "reports_root"
    roots[name] = Path(name) if invalid.endswith("relative") else tmp_path / "other" / name
    dependencies = _forbidden_access(monkeypatch)
    before = tuple(tmp_path.rglob("*"))

    with pytest.raises(ConfigError, match="absolute|Conflicting"):
        if mode == "offline":
            diff.build_offline_diff_summary(edition_name="weekly", issue_number=issue, section=None, **roots)
        else:
            diff.build_publish_diff_summary(
                edition_name="weekly", issue_number=issue, since="last-confirmed",
                archive_root=tmp_path / "archive", **roots,
            )

    for dependency in dependencies.values():
        dependency.assert_not_called()
    assert tuple(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("archive", [Path("archive"), Path("other") / "archive"])
def test_publish_diff_rejects_invalid_archive_before_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archive: Path,
) -> None:
    dependencies = _forbidden_access(monkeypatch)
    root = archive if archive == Path("archive") else tmp_path / archive
    with pytest.raises(ConfigError, match="absolute|Conflicting"):
        diff.build_publish_diff_summary(
            edition_name="weekly", issue_number=7, since="last-confirmed",
            programs_root=tmp_path / "programs", reports_root=tmp_path / "reports", archive_root=root,
        )
    for dependency in dependencies.values():
        dependency.assert_not_called()


@pytest.mark.parametrize("entry", ["preflight", "handler"])
@pytest.mark.parametrize("root", ["PROGRAMS_ROOT", "REPORTS_ROOT", "ARCHIVE_ROOT"])
@pytest.mark.parametrize("invalid", ["relative", "conflict"])
def test_diff_preflight_and_direct_handler_reject_before_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    entry: str, root: str, invalid: str,
) -> None:
    for name in ("PROGRAMS_ROOT", "REPORTS_ROOT", "ARCHIVE_ROOT"):
        monkeypatch.setattr(diff, name, tmp_path / name.lower().removesuffix("_root"))
    monkeypatch.setattr(diff, root, Path("relative") if invalid == "relative" else tmp_path / "other" / root)
    dependencies = _forbidden_access(monkeypatch)
    with pytest.raises(typer.Exit) as error:
        if entry == "preflight":
            diff._preflight_diff_workspace()
        else:
            diff.diff_command(edition="weekly", issue=7, since="last-draft", section=None, format="json")
    assert error.value.exit_code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert ("absolute" if invalid == "relative" else "Conflicting") in output.err
    for dependency in dependencies.values():
        dependency.assert_not_called()
    assert not tuple(tmp_path.rglob("*"))


def test_offline_diff_preserves_explicit_program_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "programs"
    state = Mock(return_value={})
    history = Mock(return_value={"exec_summary_text": "before"})
    bundle = Mock(return_value=SimpleNamespace(config=SimpleNamespace(scorecards=())))
    monkeypatch.setattr(diff, "_load_draft_state", state)
    monkeypatch.setattr(diff, "_load_previous_dry_run_state", history)
    monkeypatch.setattr(diff, "load_bundle", bundle)
    monkeypatch.setattr(diff, "load_narratives", Mock(return_value={"exec_summary.md": "after"}))
    monkeypatch.setattr(diff, "load_overrides", Mock(return_value=None))

    result = diff.build_offline_diff_summary(
        edition_name="weekly", issue_number=7, section=None, programs_root=root, reports_root=tmp_path / "reports",
    )

    assert "Before: before" in result
    assert "After:  after" in result
    state.assert_called_once_with(edition_name="weekly", issue_number=7, programs_root=root)
    history.assert_called_once_with(edition_name="weekly", issue_number=7, programs_root=root)
    bundle.assert_called_once_with("weekly", reports_root=tmp_path / "reports", programs_root=root)


def test_publish_diff_preserves_selected_configuration_and_history_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "programs"
    reports = tmp_path / "reports"
    archive = tmp_path / "archive"
    stamp = datetime(2026, 10, 24, tzinfo=timezone.utc)
    snapshot_path = tmp_path / "baseline.json"
    snapshot_path.write_text("{}", encoding="utf-8")
    snapshot = SimpleNamespace(items=(), scorecards=())
    bundle = SimpleNamespace(config=SimpleNamespace(ado=SimpleNamespace(date_window_days=14)))
    monkeypatch.setattr(diff, "_load_draft_state", Mock(return_value={"items": [], "ado_data_as_of": stamp.isoformat()}))
    reference = Mock(return_value=SimpleNamespace(snapshot_path=snapshot_path, issue_number=6, generated_at=stamp))
    monkeypatch.setattr(diff, "_resolve_confirmed_reference", reference)
    monkeypatch.setattr(diff, "read_snapshot", Mock(return_value=snapshot))
    config = Mock(return_value=bundle)
    monkeypatch.setattr(diff, "load_bundle", config)
    monkeypatch.setattr(diff, "_build_continuity_deltas", Mock(return_value=()))
    monkeypatch.setattr(diff, "load_overrides", Mock(return_value=None))
    monkeypatch.setattr(diff, "_build_scorecard_packets", Mock(return_value={}))
    scorecards = Mock(return_value=((), (), ()))
    monkeypatch.setattr(diff, "_build_scorecard_data", scorecards)
    monkeypatch.setattr(diff, "load_scorecard_trends", Mock(return_value={}))
    monkeypatch.setattr(diff, "load_narratives", Mock(return_value={}))
    monkeypatch.setattr(diff, "load_archived_narratives", Mock(return_value={}))
    signals = Mock(return_value=None)
    monkeypatch.setattr(diff, "_load_report_signal_context", signals)
    monkeypatch.setattr(diff, "build_publish_diff_report", Mock())
    monkeypatch.setattr(diff, "render_publish_diff_report", Mock(return_value="selected"))

    assert diff.build_publish_diff_summary(
        edition_name="weekly", issue_number=7, since="last-confirmed",
        programs_root=root, reports_root=reports, archive_root=archive,
    ) == "selected"

    config.assert_called_once_with("weekly", reports_root=reports, programs_root=root)
    assert reference.call_args.kwargs["archive_root"] is archive
    assert scorecards.call_args.kwargs["reports_root"] is reports
    assert scorecards.call_args.kwargs["archive_root"] is archive
    assert signals.call_args.kwargs["archive_root"] is archive
    assert signals.call_args.kwargs["reports_root"] is reports


@pytest.mark.parametrize("mode", ["offline", "publish"])
def test_diff_api_retains_captured_shared_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    api = diff.build_offline_diff_summary if mode == "offline" else diff.build_publish_diff_summary
    captured = api.__kwdefaults__["programs_root"]
    assert captured is workspace_paths.PROGRAMS_ROOT
    monkeypatch.setenv("VERTEX_PROGRAMS_ROOT", str(tmp_path / "programs"))
    monkeypatch.setattr(diff, "PROGRAMS_ROOT", tmp_path / "programs")
    state = Mock(side_effect=StateError("Selected boundary"))
    monkeypatch.setattr(diff, "_load_draft_state", state)
    arguments = {"section": None} if mode == "offline" else {
        "since": "last-confirmed", "archive_root": captured.parent / "archive",
    }
    with pytest.raises(StateError, match="Selected boundary"):
        api(edition_name="weekly", issue_number=7, reports_root=captured.parent / "reports", **arguments)
    assert state.call_args.kwargs["programs_root"] is captured
    assert not tuple(tmp_path.rglob("*"))


@pytest.mark.parametrize("mode", ["offline", "publish"])
def test_omitted_program_root_does_not_relocate_to_explicit_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    dependencies = _forbidden_access(monkeypatch)
    with pytest.raises(ConfigError, match="Conflicting"):
        if mode == "offline":
            diff.build_offline_diff_summary(
                edition_name="weekly", issue_number=7, section=None, reports_root=tmp_path / "reports",
            )
        else:
            diff.build_publish_diff_summary(
                edition_name="weekly", issue_number=7, since="last-confirmed",
                reports_root=tmp_path / "reports", archive_root=tmp_path / "archive",
            )
    for dependency in dependencies.values():
        dependency.assert_not_called()


def test_coherent_preflight_is_read_only_and_unexpected_errors_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("PROGRAMS_ROOT", "REPORTS_ROOT", "ARCHIVE_ROOT"):
        monkeypatch.setattr(diff, name, tmp_path / name.lower().removesuffix("_root"))
    dependencies = _forbidden_access(monkeypatch)
    diff._preflight_diff_workspace()
    monkeypatch.setattr(diff, "validate_report_workspace", Mock(side_effect=RuntimeError("Unexpected defect")))
    with pytest.raises(RuntimeError, match="Unexpected defect"):
        diff._preflight_diff_workspace()
    for dependency in dependencies.values():
        dependency.assert_not_called()
    assert not tuple(tmp_path.rglob("*"))


@pytest.mark.parametrize("mode", ["offline", "publish"])
def test_coherent_diff_latest_issue_preserves_no_history_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    latest = Mock(return_value=7)
    state = Mock(return_value={})
    monkeypatch.setattr(diff, "_latest_draft_issue_number", latest)
    monkeypatch.setattr(diff, "_load_draft_state", state)
    history = Mock(return_value=None)
    config = Mock(side_effect=AssertionError("No configuration needed without history"))
    monkeypatch.setattr(diff, "load_bundle", config)
    if mode == "offline":
        monkeypatch.setattr(diff, "_load_previous_dry_run_state", history)
        result = diff.build_offline_diff_summary(
            edition_name="weekly", issue_number=None, section=None,
            programs_root=tmp_path / "programs", reports_root=tmp_path / "reports",
        )
        assert result == "VERTEX DIFF - No previous dry-run found for Issue 007.\n"
    else:
        monkeypatch.setattr(diff, "_resolve_confirmed_reference", history)
        result = diff.build_publish_diff_summary(
            edition_name="weekly", issue_number=None, since="last-confirmed",
            programs_root=tmp_path / "programs", reports_root=tmp_path / "reports", archive_root=tmp_path / "archive",
        )
        assert result == "VERTEX DIFF - No confirmed baseline found for last-confirmed before Issue 007.\n"
    latest.assert_called_once_with("weekly", programs_root=tmp_path / "programs")
    assert state.call_args.kwargs["programs_root"] == tmp_path / "programs"
    config.assert_not_called()
