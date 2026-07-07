from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from src.core.ado_proposal import ADOUpdateEntry, ADOUpdateProposal, write_proposal_manifest
from src.core.models import RiskLevel, WorkItem
from src.core.models_v2 import ActionItem, ActionSourceType, ActionStatus, DecisionAsk, Milestone, MilestoneStatus, RiskCategory, RiskEntry, RiskImpact, RiskProbability, RiskStatus
from src.core.owner_pack import OwnerPackCalibrationSummary, OwnerPackVitalitySummary, build_owner_pack, render_owner_pack_markdown, write_owner_pack


def test_build_owner_pack_collects_owner_items_asks_and_proposals(tmp_path: Path) -> None:
    write_proposal_manifest(
        ADOUpdateProposal(
            id="prop-demo",
            program_id="demo",
            edition_id="demo_weekly",
            issue_number=7,
            update_type="comment",
            created_at=datetime(2026, 5, 13, 18, 0, tzinfo=timezone.utc),
            expires_at=datetime(2026, 5, 16, 18, 0, tzinfo=timezone.utc),
            entries=(
                ADOUpdateEntry(
                    work_item_id=1001,
                    action="add_comment",
                    field_or_tag="comment",
                    current_value=None,
                    proposed_value="Vertex demo_weekly issue #007",
                    reason="Cited in confirmed issue #007.",
                    revision_id=11,
                ),
            ),
        ),
        programs_root=(tmp_path / "programs"),
    )
    pack = build_owner_pack(
        program_id="demo",
        owner_alias="priya",
        items=(
            _item(1001, "High risk delivery", "priya@example.com", RiskLevel.HIGH, "2026-05-01T00:00:00+00:00"),
            _item(1002, "Healthy item", "priya@example.com", RiskLevel.LOW, "2026-05-12T00:00:00+00:00"),
            _item(1003, "Other owner item", "alex@example.com", RiskLevel.HIGH, "2026-05-01T00:00:00+00:00"),
        ),
        risk_register_entries=(
            RiskEntry(
                id="risk-1",
                program_id="demo",
                title="Rollout dependency may slip",
                description="Shared dependency is still missing a committed delivery date.",
                probability=RiskProbability.LIKELY,
                impact=RiskImpact.HIGH,
                category=RiskCategory.DEPENDENCY,
                owner_alias="priya",
                mitigation_plan="Escalate dependency review in next sync.",
                mitigation_due_date=date(2026, 5, 18),
                linked_workstream_ids=("ws_demo",),
                linked_work_item_ids=(1001,),
                linked_milestone_ids=(),
                linked_claim_ids=(),
                linked_action_ids=("action-1",),
                status=RiskStatus.OPEN,
                identified_date=date(2026, 5, 10),
                identified_in_vertex_issue=7,
                last_reviewed_date=date(2026, 5, 11),
                entity_refs=("WI:1001",),
            ),
            RiskEntry(
                id="risk-2",
                program_id="demo",
                title="Other owner risk",
                description="Should be filtered out.",
                probability=RiskProbability.POSSIBLE,
                impact=RiskImpact.MEDIUM,
                category=RiskCategory.RESOURCE,
                owner_alias="alex",
                mitigation_plan=None,
                mitigation_due_date=None,
                linked_workstream_ids=("ws_demo",),
                linked_work_item_ids=(1003,),
                linked_milestone_ids=(),
                linked_claim_ids=(),
                linked_action_ids=(),
                status=RiskStatus.OPEN,
                identified_date=date(2026, 5, 10),
                identified_in_vertex_issue=7,
                last_reviewed_date=date(2026, 5, 11),
                entity_refs=("WI:1003",),
            ),
        ),
        milestones=(
            Milestone(
                id="m1",
                program_id="demo",
                name="GA readiness",
                target_date=date(2026, 5, 22),
                owner_alias="priya",
                status=MilestoneStatus.AT_RISK,
                exit_criteria=("Ship dependency cleared",),
                linked_workstream_ids=("ws_demo",),
                linked_work_item_ids=(1001,),
                notes="Coordinate with the shared dependency owner.",
            ),
            Milestone(
                id="m2",
                program_id="demo",
                name="Readiness signoff",
                target_date=date(2026, 5, 29),
                owner_alias="alex",
                status=MilestoneStatus.ON_TRACK,
                exit_criteria=("Readiness checklist complete",),
                linked_workstream_ids=("ws_demo",),
                linked_work_item_ids=(1002,),
                notes=None,
            ),
            Milestone(
                id="m3",
                program_id="demo",
                name="Filtered milestone",
                target_date=date(2026, 6, 5),
                owner_alias="alex",
                status=MilestoneStatus.ON_TRACK,
                exit_criteria=("Other owner work",),
                linked_workstream_ids=("ws_demo",),
                linked_work_item_ids=(1003,),
                notes=None,
            ),
        ),
        open_actions=(
            ActionItem(
                id="action-1",
                program_id="demo",
                text="Follow up on rollout readiness.",
                owner_alias="priya",
                due_date=date(2026, 5, 16),
                status=ActionStatus.OPEN,
                source_signal_id=None,
                source_type=ActionSourceType.MANUAL,
                linked_work_item_ids=(1001,),
                linked_claim_id=None,
                linked_risk_id=None,
                workstream_id="ws_demo",
                created_at=datetime(2026, 5, 14, 18, 0, tzinfo=timezone.utc),
                resolved_at=None,
                resolution_note=None,
            ),
            ActionItem(
                id="action-2",
                program_id="demo",
                text="Other owner action.",
                owner_alias="alex",
                due_date=date(2026, 5, 17),
                status=ActionStatus.OPEN,
                source_signal_id=None,
                source_type=ActionSourceType.MANUAL,
                linked_work_item_ids=(1003,),
                linked_claim_id=None,
                linked_risk_id=None,
                workstream_id="ws_demo",
                created_at=datetime(2026, 5, 14, 18, 0, tzinfo=timezone.utc),
                resolved_at=None,
                resolution_note=None,
            ),
        ),
        assumptions=(),
        resolution_candidate_action_ids=frozenset({"action-1"}),
        open_decision_asks=(
            DecisionAsk(
                id="ask-1",
                program_id="demo",
                edition_id="demo_weekly",
                issue_number=7,
                text="Need decision on rollout timing.",
                entity_refs=("WI:1001",),
                ask_date=date(2026, 5, 13),
                owner_alias="priya",
            ),
        ),
        generated_at=datetime(2026, 5, 15, 18, 0, tzinfo=timezone.utc),
        programs_root=tmp_path / "programs",
        vitality_summary=OwnerPackVitalitySummary(
            composite_score=61,
            total_items=2,
            fresh_items=1,
            avg_richness=55.0,
            total_leakage=1,
            workiq_signal_count=2,
        ),
        calibration_summary=OwnerPackCalibrationSummary(
            owner_alias="priya",
            claim_accuracy=0.6,
            sample_size=5,
            met=3,
            contradicted=1,
            stale=1,
            slip_modifier=0.10,
        ),
    )
    markdown = render_owner_pack_markdown(pack)
    output_path = write_owner_pack(pack, programs_root=(tmp_path / "programs"))

    assert len(pack.items) == 2
    assert [item.id for item in pack.open_risks] == [1001]
    assert [entry.id for entry in pack.risk_register_entries] == ["risk-1"]
    assert [entry.milestone_id for entry in pack.milestone_contributions] == ["m1", "m2"]
    assert [item.id for item in pack.stale_items] == [1001]
    assert [action.id for action in pack.open_actions] == ["action-1"]
    assert [ask.id for ask in pack.open_decision_asks] == ["ask-1"]
    assert [entry.proposal_id for entry in pack.proposal_entries] == ["prop-demo"]
    assert "## Vitality Summary" in markdown
    assert "Composite 61% | 1/2 fresh | avg richness 55.0 | leakage 1/2" in markdown
    assert "## Open Actions" in markdown
    assert "## Calibration Profile" in markdown
    assert "priya: 60% met (3/5) | 1 contradicted | 1 stale | slip modifier +0.10" in markdown
    assert "Follow up on rollout readiness." in markdown
    assert "candidate for resolution" in markdown
    assert "## Open Risks" in markdown
    assert "## Risk Register Entries" in markdown
    assert "Rollout dependency may slip" in markdown
    assert "mitigation: Escalate dependency review in next sync." in markdown
    assert "## Milestone Contributions" in markdown
    assert "GA readiness" in markdown
    assert "linked WI:1002" in markdown
    assert "notes: Coordinate with the shared dependency owner." in markdown
    assert "Need decision on rollout timing." in markdown
    assert output_path == (tmp_path / "programs") / "demo" / "owner_packs" / "priya.md"


def test_build_owner_pack_includes_raci_scoped_related_entries() -> None:
    pack = build_owner_pack(
        program_id="demo",
        owner_alias="priya",
        items=(
            _item(1002, "Workstream-owned delivery", "alex@example.com", RiskLevel.HIGH, "2026-05-01T00:00:00+00:00"),
        ),
        risk_register_entries=(
            RiskEntry(
                id="risk-raci",
                program_id="demo",
                title="Scoped dependency risk",
                description="This risk belongs to the accountable workstream.",
                probability=RiskProbability.LIKELY,
                impact=RiskImpact.HIGH,
                category=RiskCategory.DEPENDENCY,
                owner_alias="alex",
                mitigation_plan=None,
                mitigation_due_date=None,
                linked_workstream_ids=("ws_demo",),
                linked_work_item_ids=(1002,),
                linked_milestone_ids=(),
                linked_claim_ids=(),
                linked_action_ids=("action-raci",),
                status=RiskStatus.OPEN,
                identified_date=date(2026, 5, 10),
                identified_in_vertex_issue=7,
                last_reviewed_date=date(2026, 5, 11),
                entity_refs=("WI:1002",),
            ),
        ),
        milestones=(
            Milestone(
                id="m-raci",
                program_id="demo",
                name="Scoped milestone",
                target_date=date(2026, 5, 22),
                owner_alias="alex",
                status=MilestoneStatus.AT_RISK,
                exit_criteria=("Scoped exit",),
                linked_workstream_ids=("ws_demo",),
                linked_work_item_ids=(),
                notes=None,
            ),
        ),
        open_actions=(
            ActionItem(
                id="action-raci",
                program_id="demo",
                text="Follow up inside accountable workstream.",
                owner_alias="alex",
                due_date=date(2026, 5, 16),
                status=ActionStatus.OPEN,
                source_signal_id=None,
                source_type=ActionSourceType.MANUAL,
                linked_work_item_ids=(1002,),
                linked_claim_id=None,
                linked_risk_id=None,
                workstream_id="ws_demo",
                created_at=datetime(2026, 5, 14, 18, 0, tzinfo=timezone.utc),
                resolved_at=None,
                resolution_note=None,
            ),
        ),
        assumptions=(),
        resolution_candidate_action_ids=frozenset(),
        open_decision_asks=(
            DecisionAsk(
                id="ask-raci",
                program_id="demo",
                edition_id="demo_weekly",
                issue_number=7,
                text="Need accountable decision on scoped workstream.",
                entity_refs=("WI:1002",),
                ask_date=date(2026, 5, 13),
                owner_alias="alex",
            ),
        ),
        scoped_workstream_ids=("ws_demo",),
        scoped_item_ids=(1002,),
        generated_at=datetime(2026, 5, 15, 18, 0, tzinfo=timezone.utc),
    )

    markdown = render_owner_pack_markdown(pack)

    assert [item.id for item in pack.items] == [1002]
    assert [entry.id for entry in pack.risk_register_entries] == ["risk-raci"]
    assert [milestone.milestone_id for milestone in pack.milestone_contributions] == ["m-raci"]
    assert [action.id for action in pack.open_actions] == ["action-raci"]
    assert [ask.id for ask in pack.open_decision_asks] == ["ask-raci"]
    assert "raci ws_demo" in markdown
    assert "Need accountable decision on scoped workstream." in markdown


def _item(work_item_id: int, title: str, owner: str, risk_level: RiskLevel, changed_date: str) -> WorkItem:
    return WorkItem(
        id=work_item_id,
        type="Feature",
        title=title,
        state="Active",
        assigned_to=owner,
        assigned_to_email=owner,
        area_path="One\\Demo\\WS",
        iteration_path="Sprint 1",
        target_date=date(2026, 6, 1),
        risk_level=risk_level,
        tags=[],
        custom_fields={"changed_date": changed_date},
        revisions=[],
        comments=[],
        fetched_at=datetime(2026, 5, 15, 18, 0, tzinfo=timezone.utc),
    )

