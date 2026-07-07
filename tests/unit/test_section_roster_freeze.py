from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.core.continuation_contract import build_continuation_contract
from src.core.overrides_store import OverridesDocument
from src.core.trusted_baseline_store import advance_trusted_baseline
from tests.support.report_test_setup import stage_v2_report_workspace


EDITION_NAME = "acme_weekly"


def test_build_continuation_contract_reports_section_roster_drift(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    output_dir = tmp_path / "output" / EDITION_NAME
    output_dir.mkdir(parents=True, exist_ok=True)

    prior_dir = reports_root.parent / "programs" / "acme" / "narratives" / "issue_001"
    prior_dir.mkdir(parents=True, exist_ok=True)
    (prior_dir / "exec_summary.md").write_text("Prior summary", encoding="utf-8")
    (prior_dir / "ws_deployment_velocity.md").write_text("Prior deployment section", encoding="utf-8")
    (prior_dir / "chapter_deployment_readiness.md").write_text("Prior chapter section", encoding="utf-8")
    advance_trusted_baseline(
        EDITION_NAME,
        1,
        established_at=datetime(2026, 5, 14, 8, 0, tzinfo=timezone.utc),
        established_by="operator",
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )

    contract = build_continuation_contract(
        edition_name=EDITION_NAME,
        issue_number=2,
        started_at=datetime(2026, 5, 14, 9, 0, tzinfo=timezone.utc),
        reports_root=reports_root,
        archive_root=archive_root,
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
        overrides_document=OverridesDocument(
            issue_number=2,
            top_3_now=(),
            scorecards=(),
            removed_sections=("deployment_readiness",),
        ),
        workstream_data=(),
        output_dir=output_dir,
        current_scorecard_dimensions=(),
        current_section_ids=("deployment_velocity", "exec_summary", "new_section"),
    )

    assert contract is not None
    assert contract.section_roster.inherited_sections == (
        "chapter_deployment_readiness.md",
        "exec_summary.md",
        "ws_deployment_velocity.md",
    )
    assert contract.section_roster.seeded_from_prior is False
    assert contract.section_roster.added_sections == ("new_section",)
    assert contract.section_roster.removed_sections == ()