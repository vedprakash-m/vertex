from __future__ import annotations

from datetime import date, datetime, timezone

from src.core.ado_reconcile import build_ado_reconcile_report, render_ado_reconcile_report
from src.core.config_loader import ScorecardDimensionSettings, ScorecardSettings
from src.core.models import RiskLevel, WorkItem
from src.core.models_v2 import ClaimEntry, Workstream
from src.core.overrides_store import DimensionOverride, OverridesDocument, ScorecardOverrides


def test_build_ado_reconcile_report_flags_override_eta_and_area_mismatches() -> None:
    item = WorkItem(
        id=1001,
        type="Feature",
        title="Demo item",
        state="Active",
        assigned_to="owner@example.com",
        assigned_to_email="owner@example.com",
        area_path="One\\Demo\\Platform",
        iteration_path="Sprint 1",
        target_date=date(2026, 5, 27),
        risk_level=RiskLevel.MEDIUM,
        tags=[],
        custom_fields={"changed_date": "2026-05-10T00:00:00+00:00"},
        revisions=[],
        comments=[],
        fetched_at=datetime(2026, 5, 15, 18, 0, tzinfo=timezone.utc),
    )
    report = build_ado_reconcile_report(
        program_id="demo",
        items=(item,),
        workstreams=(
            Workstream(
                id="delivery",
                name="Delivery",
                area_paths=("One\\Demo\\Delivery",),
            ),
        ),
        scorecards=(
            ScorecardSettings(
                name="Demo Scorecard",
                dimensions=(
                    ScorecardDimensionSettings(
                        name="Delivery",
                        description=None,
                        ado_filter="area_path contains 'Platform'",
                    ),
                ),
            ),
        ),
        overrides_document=OverridesDocument(
            issue_number=7,
            top_3_now=(),
            scorecards=(
                ScorecardOverrides(
                    name="Demo Scorecard",
                    dimensions=(
                        DimensionOverride(name="Delivery", risk=RiskLevel.HIGH),
                    ),
                ),
            ),
        ),
        open_claims=(
            ClaimEntry(
                id="claim-1",
                program_id="demo",
                edition_id="demo_weekly",
                issue_number=7,
                workstream_id="delivery",
                text="Delivery is on track for 2026-05-20.",
                entity_refs=("WI:1001",),
                claim_date=date(2026, 5, 13),
                owner_alias="owner",
                due_date=date(2026, 5, 20),
            ),
        ),
    )
    rendered = render_ado_reconcile_report(report)

    assert [entry.kind for entry in report.discrepancies] == ["claim_eta", "override_risk", "workstream_area"]
    assert "Reconciliation: demo | 3 discrepancies found" in rendered
    assert "Vertex override (Demo Scorecard / Delivery): high | ADO risk: medium" in rendered
    assert "Claim ETA (claim-1): 2026-05-20 | ADO TargetDate: 2026-05-27" in rendered
    assert "Vertex ws (claim-1): delivery | ADO area: One\\Demo\\Platform" in rendered