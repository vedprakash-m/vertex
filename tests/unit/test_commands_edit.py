from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from src.commands.edit import run_edit
from src.commands.report import generate_report_draft
from src.core.narrative_store import get_narratives_dir
from tests.support.report_test_setup import disable_kusto_in_report_copy, stage_v2_report_workspace
from tests.unit.test_commands_report import _sample_items


EDITION_NAME = "acme_weekly"


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


def test_run_edit_scaffolds_missing_continuity_chapter_file_from_dimension_name(repo_root: Path, tmp_path: Path) -> None:
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

    section_id = "acme-adventure-xio-100-ramp-readiness-deployment-velocity"
    narrative_path = get_narratives_dir(EDITION_NAME, 1, reports_root) / f"ws_{section_id}.md"
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
    assert "[Your narrative here]" in narrative_path.read_text(encoding="utf-8")


def test_run_edit_accepts_direct_continuity_chapter_id(repo_root: Path, tmp_path: Path) -> None:
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

    result = run_edit(
        edition_name=EDITION_NAME,
        section="acme-adventure-xio-100-ramp-readiness-deployment-velocity",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        editor_runner=lambda path, read_only: True,
        prompt_runner=lambda message: False,
    )

    assert result.section_id == "acme-adventure-xio-100-ramp-readiness-deployment-velocity"
    assert result.path.name == "ws_nova-adventure-xio-100-ramp-readiness-deployment-velocity.md"


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
    assert "[WHAT MOVED paragraph]" in scaffold
    assert "[WHERE WE ARE paragraph]" in scaffold

