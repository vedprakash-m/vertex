from __future__ import annotations

from datetime import date, datetime, timezone

from src.core.issue_projection import build_issue_projection
from src.core.models import Confidence, FreshnessItem, FreshnessReport, RiskLevel, WorkItem
from src.core.models_v2 import ActionItem, ActionSourceType, ActionStatus, ClaimEntry, DecisionAsk, RiskCategory, RiskEntry, RiskImpact, RiskProbability, RiskStatus, Signal


def test_build_issue_projection_aggregates_issue_sources() -> None:
    items = (
        WorkItem(
            id=101,
            type="Feature",
            title="Ramp gate remains blocked",
            state="Blocked",
            assigned_to="Vertex Maintainer",
            assigned_to_email="maintainer@example.com",
            area_path="Acme\\Ramp",
            iteration_path="FY26\\Sprint 10",
            target_date=date(2026, 5, 20),
            risk_level=RiskLevel.HIGH,
            tags=["blocked"],
            custom_fields={},
        ),
    )
    freshness_report = FreshnessReport(
        issue_number=77,
        items=(
            FreshnessItem(
                work_item_id=101,
                rule_id="FR-21",
                severity="block",
                message="ETA is in the past (3 days overdue).",
                suggested_fix="Update target date.",
            ),
            FreshnessItem(
                work_item_id=101,
                rule_id="FR-46",
                severity="block",
                message="Active item has no assigned owner.",
                suggested_fix="Assign a DRI.",
            ),
            FreshnessItem(
                work_item_id=101,
                rule_id="FR-22",
                severity="warn",
                message="No recent activity.",
                suggested_fix="Refresh the item.",
            ),
        ),
        blocks=2,
        warns=1,
        infos=0,
    )
    icm_signals = (
        Signal(
            id="signal-1",
            timestamp=datetime(2026, 5, 10, 15, 0, tzinfo=timezone.utc),
            source="icm/incident",
            program_id="acme",
            workstream_id="ramp",
            entity_refs=("ICM:12345",),
            text="IcM 12345: Sev2 incident active for ramp validation.",
            raw_ref="icm:12345",
            confidence=Confidence.HIGH,
            metadata={"severity": 2},
        ),
    )
    open_asks = (
        DecisionAsk(
            id="ask-1",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            text="Need LT decision on fallback launch date.",
            entity_refs=("WI:101",),
            ask_date=date(2026, 5, 10),
            owner_alias="maintainer",
        ),
    )
    overdue_actions = (
        ActionItem(
            id="action-1",
            program_id="acme",
            text="Confirm BIOS remediation completion",
            owner_alias="maintainer",
            due_date=date(2026, 5, 9),
            status=ActionStatus.OPEN,
            source_signal_id=None,
            source_type=ActionSourceType.MANUAL,
            linked_work_item_ids=(101,),
            linked_claim_id=None,
            linked_risk_id=None,
            workstream_id="ramp",
            created_at=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
            resolved_at=None,
            resolution_note=None,
        ),
    )
    open_claims = (
        ClaimEntry(
            id="claim-1",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            workstream_id="ramp",
            text="Ramp gate will clear before the LT review.",
            entity_refs=("WI:101",),
            claim_date=date(2026, 5, 5),
            owner_alias="maintainer",
            due_date=date(2026, 5, 12),
        ),
    )
    risk_entries = (
        RiskEntry(
            id="risk-1",
            program_id="acme",
            title="Ramp remediation may miss the LT gate",
            description="Telemetry stabilization remains a gating dependency.",
            probability=RiskProbability.LIKELY,
            impact=RiskImpact.HIGH,
            category=RiskCategory.TECHNICAL,
            owner_alias="maintainer",
            mitigation_plan="Track the gating work daily until the blocker clears.",
            mitigation_due_date=date(2026, 5, 12),
            linked_workstream_ids=("ramp",),
            linked_work_item_ids=(101,),
            linked_milestone_ids=(),
            linked_claim_ids=("claim-1",),
            linked_action_ids=("action-1",),
            status=RiskStatus.OPEN,
            identified_date=date(2026, 5, 4),
            identified_in_vertex_issue=76,
            last_reviewed_date=date(2026, 5, 10),
            entity_refs=("WI:101",),
        ),
    )

    projections = build_issue_projection(
        items=items,
        freshness_report=freshness_report,
        icm_signals=icm_signals,
        open_asks=open_asks,
        overdue_actions=overdue_actions,
        open_claims=open_claims,
        risk_entries=risk_entries,
        ado_item_base_url="https://dev.azure.com/your-org/One/_workitems/edit",
    )

    assert [entry.source_type for entry in projections] == [
        "ado_blocked",
        "freshness_block",
        "icm_incident",
        "decision_ask",
        "overdue_action",
    ]

    blocked = projections[0]
    assert blocked.work_item_id == 101
    assert blocked.severity == "block"
    assert blocked.owner_alias == "maintainer@example.com"
    assert blocked.ado_url == "https://dev.azure.com/your-org/One/_workitems/edit/101"
    assert blocked.linked_entity_ids == ("ask-1", "action-1", "claim-1", "risk-1")
    assert blocked.confidence is Confidence.HIGH

    freshness = projections[1]
    assert freshness.work_item_id == 101
    assert freshness.summary == (
        'WI:101 "Ramp gate remains blocked" — ETA is in the past (3 days overdue).; '
        "Active item has no assigned owner."
    )
    assert freshness.ado_url == "https://dev.azure.com/your-org/One/_workitems/edit/101"
    assert freshness.linked_entity_ids == ("ask-1", "action-1", "claim-1", "risk-1")
    assert freshness.confidence is Confidence.HIGH

    icm = projections[2]
    assert icm.work_item_id is None
    assert icm.workstream_id == "ramp"
    assert icm.severity == "block"
    assert icm.ado_url is None
    assert icm.confidence is Confidence.HIGH

    ask = projections[3]
    assert ask.work_item_id == 101
    assert ask.ado_url == "https://dev.azure.com/your-org/One/_workitems/edit/101"
    assert ask.linked_entity_ids == ("ask-1",)
    assert "owner maintainer" in ask.summary
    assert ask.confidence is Confidence.HIGH

    action = projections[4]
    assert action.work_item_id == 101
    assert action.workstream_id == "ramp"
    assert action.ado_url == "https://dev.azure.com/your-org/One/_workitems/edit/101"
    assert action.linked_entity_ids == ("action-1",)
    assert action.confidence is Confidence.HIGH


def test_build_issue_projection_ignores_non_block_freshness_findings_and_non_icm_signals() -> None:
    projections = build_issue_projection(
        items=(),
        freshness_report=FreshnessReport(
            issue_number=77,
            items=(
                FreshnessItem(
                    work_item_id=101,
                    rule_id="FR-22",
                    severity="warn",
                    message="No recent activity.",
                    suggested_fix="Refresh the item.",
                ),
            ),
            blocks=0,
            warns=1,
            infos=0,
        ),
        icm_signals=(
            Signal(
                id="signal-2",
                timestamp=datetime(2026, 5, 10, 15, 0, tzinfo=timezone.utc),
                source="workiq",
                program_id="acme",
                workstream_id=None,
                entity_refs=("WI:101",),
                text="WorkIQ summary.",
                raw_ref=None,
                confidence=Confidence.MEDIUM,
            ),
        ),
        open_asks=(),
        overdue_actions=(),
    )

    assert projections == ()