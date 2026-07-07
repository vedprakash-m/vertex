from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from src.commands.report import generate_report_draft
from tests.golden.test_report_pipeline_snapshots import FrozenDateTime
from tests.support.ado_cassettes import load_cassette_work_items
from tests.support.report_test_setup import disable_kusto_in_report_copy, stage_v2_report_workspace


GOLDEN_DIR = Path(__file__).resolve().parent / "snapshots"
FROZEN_NOW = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
FROZEN_MANIFEST_ID = UUID("11111111-1111-1111-1111-111111111111")
EDITION_NAME = "acme_weekly"


def test_report_output_matches_characterization_golden(update_golden: bool, monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    monkeypatch.setattr("src.commands.report.datetime", FrozenDateTime)
    monkeypatch.setattr("uuid.uuid4", lambda: FROZEN_MANIFEST_ID)

    draft = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=FROZEN_NOW,
        work_item_loader=lambda bundle, timestamp: load_cassette_work_items("cold_start", timestamp),
        open_browser=False,
    )

    golden_path = GOLDEN_DIR / "report_draft_issue_001.golden"
    if update_golden:
        golden_path.write_text(draft.html_body, encoding="utf-8")

    golden_html = golden_path.read_text(encoding="utf-8")

    assert draft.html_body == golden_html
    assert {
        "issue_number": draft.issue_number,
        "exit_code": draft.exit_code,
        "manifest_id": draft.manifest.manifest_id,
        "edition": draft.manifest.edition,
        "ado_calls": draft.manifest.ado_calls,
        "ai_calls": draft.manifest.ai_calls,
        "ai_cost_usd": draft.manifest.ai_cost_usd,
        "qg_results": draft.manifest.qg_results,
        "warning_count": len(draft.warnings),
        "has_manifest_path": draft.manifest_path is not None,
        "has_snapshot_path": draft.snapshot_path is not None,
    } == {
        "issue_number": 1,
        "exit_code": 3,
        "manifest_id": "11111111111111111111111111111111",
        "edition": "acme_weekly",
        "ado_calls": 1,
        "ai_calls": 0,
        "ai_cost_usd": 0.0,
        "qg_results": {
            "QG-1": False,
            "QG-10": True,
            "QG-11": True,
            "QG-12": True,
            "QG-13": False,
            "QG-14": True,
            "QG-15": True,
            "QG-16": False,
            "QG-17": True,
            "QG-19": True,
            "QG-2": True,
            "QG-23": True,
            "QG-24": False,
            "QG-25": True,
            "QG-26": True,
            "QG-WS5B": True,
            "QG-28": True,
            "QG-3": False,
            "QG-4": True,
            "QG-5": True,
            "QG-6": True,
            "QG-7": True,
            "QG-8": True,
            "QG-9": True,
            "QG-DM-1": True,
            "QG-DM-4": True,
            "QG-DM-5": True,
            "QG-DM-6": True,
            "QG-DM-7": True,
            "QG-DM-10": False,
            "QG-DM-13": True,
        },
        "warning_count": 163,
        "has_manifest_path": True,
        "has_snapshot_path": True,
    }

