from __future__ import annotations
from tests.support.synthetic_report_source import synthetic_report_root as repo_root

import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.commands.edit import run_edit
from src.commands.report import generate_report_draft
from src.core.narrative_store import get_narratives_dir
from tests.support.report_test_setup import disable_kusto_in_report_copy, stage_v2_report_workspace
from tests.unit.test_commands_report import _sample_items


EDITION_NAME = "acme_weekly"


def test_edit_rejects_conflicting_context_before_state_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import Mock
    from src.commands import edit
    from src.core.exceptions import ConfigError

    reader = Mock(side_effect=AssertionError("Unexpected state access"))
    monkeypatch.setattr(edit, "_latest_draft_issue_number", reader)
    with pytest.raises(ConfigError, match="Conflicting report workspace roots"):
        run_edit(
            edition_name=EDITION_NAME, section="exec_summary", issue_number=7,
            reports_root=tmp_path / "reports", archive_root=tmp_path / "archive",
            programs_root=tmp_path / "custom-programs",
        )
    reader.assert_not_called()
    assert not tuple(tmp_path.iterdir())


def test_edit_target_uses_selected_root_without_touching_flat_decoy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.commands import edit
    from src.commands.edit import _prepare_edit_target
    from tests.unit.test_narrative_store import _selected_artifact_root

    root = _selected_artifact_root(tmp_path)
    reports = tmp_path / "elsewhere" / "reports"
    decoy = reports / "fixture_weekly" / "narratives" / "issue_007" / "exec_summary.md"
    decoy.parent.mkdir(parents=True)
    decoy.write_text("decoy", encoding="utf-8")
    monkeypatch.setattr(edit, "_scaffold_content", lambda *_args, **_kwargs: "selected")
    path, section, created = _prepare_edit_target(
        edition_name="fixture_weekly", section="exec_summary", issue_number=7,
        reports_root=reports, programs_root=root,
    )
    assert path == root / "fixture" / "narratives" / "issue_007" / "exec_summary.md"
    assert section == "exec_summary"
    assert created and path.is_file()
    assert decoy.read_text(encoding="utf-8") == "decoy"


def test_edit_target_missing_selected_context_does_not_create_flat_directory(tmp_path: Path) -> None:
    from src.commands.edit import _prepare_edit_target
    from src.core.exceptions import ConfigError

    root = tmp_path / "custom-programs"
    root.mkdir()
    with pytest.raises(ConfigError):
        _prepare_edit_target(
            edition_name="missing", section="exec_summary", issue_number=7,
            reports_root=tmp_path / "reports", programs_root=root,
        )
    assert not (tmp_path / "reports").exists()


@pytest.mark.parametrize("injected", [False, True])
@pytest.mark.parametrize("program_backed", [False, True])
def test_edit_rerun_preserves_builtin_root_and_injected_runner_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, injected: bool, program_backed: bool,
) -> None:
    from unittest.mock import Mock
    from src.commands import edit

    root = tmp_path / "programs"
    reports, archive = tmp_path / "reports", tmp_path / "archive"
    if program_backed:
        editions = root / "fixture" / "editions"
        editions.mkdir(parents=True)
        (editions / "fixture_weekly.yaml").write_text("program_id: fixture\n", encoding="utf-8")
    target = (
        root / "fixture" / "narratives" / "issue_007" / "exec_summary.md"
        if program_backed else reports / "fixture_weekly" / "narratives" / "issue_007" / "exec_summary.md"
    )
    target.parent.mkdir(parents=True)
    target.write_text("selected", encoding="utf-8")
    monkeypatch.setattr(edit, "_latest_draft_issue_number", lambda *_args, **_kwargs: 7)
    monkeypatch.setattr(edit, "is_issue_confirmed", lambda *_args, **_kwargs: False)
    generator = Mock()
    monkeypatch.setattr(edit, "generate_report_draft", generator)
    calls = []

    def runner(edition: str, issue: int, selected_reports: Path, selected_archive: Path) -> None:
        calls.append((edition, issue, selected_reports, selected_archive))

    result = run_edit(
        edition_name="fixture_weekly", section="exec_summary", issue_number=7,
        reports_root=reports, archive_root=archive, programs_root=root,
        editor_runner=lambda *_args: False, prompt_runner=lambda _message: True,
        rerun_runner=runner if injected else None,
    )
    assert result.reran_dry_run
    if injected:
        assert calls == [("fixture_weekly", 7, reports, archive)]
        generator.assert_not_called()
    else:
        generator.assert_called_once_with(
            edition_name="fixture_weekly", issue_number=7, reports_root=reports,
            archive_root=archive, programs_root=root, open_browser=False,
        )


@pytest.mark.parametrize("explicit_none", [False, True])
def test_edit_target_omission_preserves_flat_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_none: bool,
) -> None:
    from src.commands import edit

    monkeypatch.setattr(edit, "_scaffold_content", lambda *_args, **_kwargs: "legacy")
    reports = tmp_path / "reports"
    path, _, created = edit._prepare_edit_target(
        edition_name="flat", section="exec_summary", issue_number=7, reports_root=reports,
        **({"programs_root": None} if explicit_none else {}),
    )
    assert path == reports / "flat" / "narratives" / "issue_007" / "exec_summary.md"
    assert created and path.read_text(encoding="utf-8") == "legacy"


def test_run_edit_opens_current_exec_summary(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = tmp_path / "programs"
    disable_kusto_in_report_copy(reports_root)

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    opened_paths: list[tuple[Path, bool]] = []
    result = run_edit(
        edition_name=EDITION_NAME,
        section="exec_summary",
        issue_number=None,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        editor_runner=lambda path, read_only: opened_paths.append((path, read_only)) or True,
        prompt_runner=lambda message: False,
    )

    assert result.issue_number == 1
    assert result.path.name == "exec_summary.md"
    assert result.created is False
    assert result.opened_in_editor is True
    assert result.reran_dry_run is False
    assert opened_paths == [(result.path, False)]


def test_run_edit_scaffolds_missing_continuity_chapter_file_from_dimension_name(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = tmp_path / "programs"
    disable_kusto_in_report_copy(reports_root)

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    from src.core.config_loader import load_bundle
    monkeypatch.setattr("src.commands.edit.load_bundle", lambda edition_name, *, reports_root, programs_root: load_bundle(edition_name, reports_root=reports_root, programs_root=programs_root))
    section_id = "deployment_readiness"
    narrative_path = get_narratives_dir(EDITION_NAME, 1, reports_root) / f"chapter_{section_id}.md"
    if narrative_path.exists():
        narrative_path.unlink()

    result = run_edit(
        edition_name=EDITION_NAME,
        section="Deployment Velocity",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        editor_runner=lambda path, read_only: True,
        prompt_runner=lambda message: False,
    )

    assert result.section_id == section_id
    assert result.created is True
    assert narrative_path.exists()
    assert "<!-- vertex:scaffold Issue 1 - Deployment Readiness -->" in narrative_path.read_text(encoding="utf-8")
    assert "Optional chapter summary or note." in narrative_path.read_text(encoding="utf-8")


def test_run_edit_accepts_direct_continuity_chapter_id(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = tmp_path / "programs"
    disable_kusto_in_report_copy(reports_root)
    from src.core.config_loader import load_bundle
    monkeypatch.setattr("src.commands.edit.load_bundle", lambda edition_name, *, reports_root, programs_root: load_bundle(edition_name, reports_root=reports_root, programs_root=programs_root))

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    result = run_edit(
        edition_name=EDITION_NAME,
        section="deployment_readiness",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        editor_runner=lambda path, read_only: True,
        prompt_runner=lambda message: False,
    )

    assert result.section_id == "deployment_readiness"
    assert result.path.name == "chapter_deployment_readiness.md"


def test_run_edit_recreates_continuity_exec_summary_scaffold(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = tmp_path / "programs"
    disable_kusto_in_report_copy(reports_root)

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    narrative_path = get_narratives_dir(EDITION_NAME, 1, reports_root) / "exec_summary.md"
    narrative_path.unlink()

    result = run_edit(
        edition_name=EDITION_NAME,
        section="exec_summary",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        editor_runner=lambda path, read_only: True,
        prompt_runner=lambda message: False,
    )

    assert result.created is True
    scaffold = narrative_path.read_text(encoding="utf-8")
    assert "<!-- vertex:scaffold Issue 1" in scaffold
    assert "{WHAT_CHANGED_SIGNAL_1}" in scaffold
    assert "{WHAT_CHANGED_SIGNAL_2}" in scaffold
    assert "{WHAT_CHANGED_SIGNAL_3}" in scaffold
    assert "{DECISION_OR_ASK}" in scaffold
