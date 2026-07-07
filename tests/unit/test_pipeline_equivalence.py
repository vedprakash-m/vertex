from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from src.commands.report import generate_report_draft, generate_report_draft_v2
from tests.golden.test_report_pipeline_snapshots import FrozenDateTime
from tests.support.ado_cassettes import load_cassette_work_items
from tests.support.report_test_setup import disable_kusto_in_report_copy, stage_v2_report_workspace


EDITION_NAME = "acme_weekly"
FROZEN_NOW = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
FROZEN_MANIFEST_ID = UUID("11111111-1111-1111-1111-111111111111")


def test_pipeline_v2_matches_legacy_report_output(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    legacy_output_root = tmp_path / "output-legacy"
    v2_output_root = tmp_path / "output-v2"
    disable_kusto_in_report_copy(reports_root)

    monkeypatch.setattr("src.commands.report.datetime", FrozenDateTime)
    monkeypatch.setattr("src.core.stages.resolution_stage.datetime", FrozenDateTime)
    monkeypatch.setattr("uuid.uuid4", lambda: FROZEN_MANIFEST_ID)

    legacy = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=tmp_path / "programs",
        as_of=FROZEN_NOW,
        work_item_loader=lambda bundle, timestamp: load_cassette_work_items("cold_start", timestamp),
        open_browser=False,
    )
    staged = generate_report_draft_v2(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=tmp_path / "programs",
        as_of=FROZEN_NOW,
        work_item_loader=lambda bundle, timestamp: load_cassette_work_items("cold_start", timestamp),
        open_browser=False,
    )

    assert staged.exit_code == legacy.exit_code
    assert staged.html_body == legacy.html_body
    assert staged.markdown_body == legacy.markdown_body
    assert staged.email_subject == legacy.email_subject
    assert staged.email_preheader == legacy.email_preheader
    assert staged.warnings == legacy.warnings
    assert staged.manifest.qg_results == legacy.manifest.qg_results
    assert staged.manifest.ado_calls == legacy.manifest.ado_calls
    assert staged.manifest.ai_calls == legacy.manifest.ai_calls