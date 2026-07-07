from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from src.core.archive_store import write_confirmed_issue
from src.core.continuation_contract import build_continuation_contract
from src.core.models import ConfirmedDimension, EditionType, RunManifest, RiskLevel, Snapshot, SnapshotItem
from src.core.overrides_store import OverridesDocument, RemovedDimension
from src.core.trusted_baseline_store import advance_trusted_baseline
from tests.support.report_test_setup import stage_v2_report_workspace


EDITION_NAME = "acme_weekly"


def test_build_continuation_contract_reports_scorecard_composition_drift(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    output_dir = tmp_path / "output" / EDITION_NAME
    output_dir.mkdir(parents=True, exist_ok=True)

    _write_confirmed_issue(
        archive_root,
        issue_number=1,
        dimensions=(
            ("Acme Readiness", "Deployment Velocity"),
            ("Acme Readiness", "Legacy Dimension"),
        ),
    )
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
            removed_dimensions=(
                RemovedDimension(
                    scorecard_name="Acme Readiness",
                    dimension_name="Legacy Dimension",
                ),
            ),
        ),
        workstream_data=(),
        output_dir=output_dir,
        current_scorecard_dimensions=(
            ("Acme Readiness", "Deployment Velocity"),
            ("Acme Readiness", "New Dimension"),
        ),
        current_section_ids=("exec_summary",),
    )

    assert contract is not None
    assert contract.scorecard_composition.frozen_from_issue == 1
    assert contract.scorecard_composition.inherited_dimensions == (
        ("Acme Readiness", "Deployment Velocity"),
        ("Acme Readiness", "Legacy Dimension"),
    )
    assert contract.scorecard_composition.proposed_additions == (("Acme Readiness", "New Dimension"),)
    assert contract.scorecard_composition.proposed_removals == ()
    assert contract.scorecard_composition.removed_by_override == (("Acme Readiness", "Legacy Dimension"),)


def _write_confirmed_issue(
    archive_root: Path,
    *,
    issue_number: int,
    dimensions: tuple[tuple[str, str], ...],
) -> None:
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=issue_number,
        snapshot=Snapshot(
            issue_number=issue_number,
            generated_at=datetime(2026, 5, 14, 8, 0, tzinfo=timezone.utc),
            ado_data_as_of=datetime(2026, 5, 14, 7, 45, tzinfo=timezone.utc),
            edition_type=EditionType.DETAILED,
            items=(
                SnapshotItem(
                    id=1000 + issue_number,
                    type="Feature",
                    title=f"Dimension source {issue_number}",
                    state="Active",
                    assigned_to="Vertex Maintainer",
                    area_path="One\\Adventure\\Acme",
                    target_date=date(2026, 6, 30),
                    risk_level=RiskLevel.LOW,
                    tags=["acme"],
                ),
            ),
            scorecards=tuple(
                ConfirmedDimension(
                    scorecard_name=scorecard_name,
                    name=dimension_name,
                    risk=RiskLevel.LOW,
                    prior_risk=RiskLevel.LOW,
                    item_count=1,
                    ado_query_url="https://dev.azure.com/your-org/One/_queries/query-id",
                )
                for scorecard_name, dimension_name in dimensions
            ),
        ),
        html_body="<html><body>Rendered</body></html>",
        markdown_body="# Rendered\n",
        manifest=RunManifest(
            manifest_id=f"manifest-{issue_number}",
            issue_number=issue_number,
            edition=EDITION_NAME,
            started_at=datetime(2026, 5, 14, 7, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 5, 14, 8, 0, tzinfo=timezone.utc),
            config_hash="config",
            snapshot_hash="snapshot",
            html_hash="html",
            md_hash="md",
            ado_calls=1,
            ai_calls=0,
            ai_cost_usd=0.0,
            freshness_summary={"blocks": 0, "warns": 0, "infos": 0},
            qg_results={"QG-B1": True, "QG-B2": True, "QG-B3": True},
            git_sha=None,
        ),
        archive_root=archive_root,
    )