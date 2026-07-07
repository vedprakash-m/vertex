from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.core.continuation_contract import build_continuation_contract
from src.core.overrides_store import OverridesDocument
from tests.support.report_test_setup import stage_v2_report_workspace


EDITION_NAME = "acme_weekly"


def test_build_continuation_contract_returns_none_without_trusted_baseline(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    output_dir = tmp_path / "output" / EDITION_NAME
    output_dir.mkdir(parents=True, exist_ok=True)

    contract = build_continuation_contract(
        edition_name=EDITION_NAME,
        issue_number=2,
        started_at=datetime(2026, 5, 14, 8, 0, tzinfo=timezone.utc),
        reports_root=reports_root,
        archive_root=archive_root,
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
        overrides_document=OverridesDocument(issue_number=2, top_3_now=(), scorecards=()),
        workstream_data=(),
        output_dir=output_dir,
        current_scorecard_dimensions=(),
        current_section_ids=("exec_summary",),
    )

    assert contract is None