from __future__ import annotations

from datetime import date, datetime, timezone

from src.core import raid_graph as raid_graph_module
from src.core.models_v2 import (
    ActionItem,
    ActionSourceType,
    ActionStatus,
    Assumption,
    AssumptionStatus,
    DecisionEntry,
    DecisionStatus,
    RiskCategory,
    RiskEntry,
    RiskImpact,
    RiskProbability,
    RiskStatus,
)
from src.core.raid_graph import build_raid_chain_index_from_entries


def test_build_raid_chain_index_traverses_assumptions_actions_and_decisions() -> None:
    risk = RiskEntry(
        id="risk-1",
        program_id="demo",
        title="Dependency handoff",
        description="An external handoff could slip.",
        probability=RiskProbability.LIKELY,
        impact=RiskImpact.HIGH,
        category=RiskCategory.DEPENDENCY,
        owner_alias="demo",
        mitigation_plan="Track the linked action.",
        mitigation_due_date=date(2026, 5, 20),
        linked_workstream_ids=("ws-demo",),
        linked_work_item_ids=(1001,),
        linked_milestone_ids=(),
        linked_claim_ids=(),
        linked_action_ids=("action-1",),
        status=RiskStatus.OPEN,
        identified_date=date(2026, 5, 1),
        identified_in_vertex_issue=7,
        last_reviewed_date=date(2026, 5, 5),
        entity_refs=("WI:1001",),
    )
    action = ActionItem(
        id="action-1",
        program_id="demo",
        text="Complete the dependency handoff checklist.",
        owner_alias="demo",
        due_date=date(2026, 5, 19),
        status=ActionStatus.IN_PROGRESS,
        source_signal_id=None,
        source_type=ActionSourceType.MANUAL,
        linked_work_item_ids=(1001,),
        linked_claim_id=None,
        linked_risk_id="risk-1",
        workstream_id="ws-demo",
        created_at=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        resolved_at=None,
        resolution_note=None,
    )
    decision = DecisionEntry(
        id="decision-1",
        program_id="demo",
        title="Dependency handoff owner",
        context="Clarify who owns the remaining cross-team handoff.",
        decision="Owner stays with deployment lead.",
        rationale=None,
        alternatives_considered=(),
        decided_by="demo",
        decision_date=date(2026, 5, 12),
        status=DecisionStatus.PROPOSED,
        superseded_by=None,
        linked_claim_id=None,
        linked_risk_id=None,
        linked_action_ids=("action-1",),
        workstream_id="ws-demo",
        entity_refs=(),
    )
    assumption = Assumption(
        id="assumption-1",
        program_id="demo",
        text="The partner team can complete handoff this week.",
        validation_method=None,
        validation_due=None,
        status=AssumptionStatus.UNVALIDATED,
        linked_risk_id="risk-1",
        linked_milestone_id=None,
        owner_alias="demo",
        identified_date=date(2026, 5, 2),
        entity_refs=(),
    )

    chains = build_raid_chain_index_from_entries(
        risks=(risk,),
        actions=(action,),
        decisions=(decision,),
        assumptions=(assumption,),
    )

    chain = chains["risk-1"]
    assert chain.has_mitigating_action is True
    assert [(link.node_type, link.node_id, link.hop) for link in chain.links] == [
        ("risk", "risk-1", 0),
        ("assumption", "assumption-1", 1),
        ("action", "action-1", 1),
        ("decision", "decision-1", 2),
    ]


def test_build_raid_chain_index_reports_cycles_and_unmitigated_risks() -> None:
    risk = RiskEntry(
        id="risk-1",
        program_id="demo",
        title="Dependency handoff",
        description="An external handoff could slip.",
        probability=RiskProbability.LIKELY,
        impact=RiskImpact.HIGH,
        category=RiskCategory.DEPENDENCY,
        owner_alias="demo",
        mitigation_plan="Track the linked action.",
        mitigation_due_date=None,
        linked_workstream_ids=("ws-demo",),
        linked_work_item_ids=(),
        linked_milestone_ids=(),
        linked_claim_ids=(),
        linked_action_ids=("action-1",),
        status=RiskStatus.OPEN,
        identified_date=date(2026, 5, 1),
        identified_in_vertex_issue=None,
        last_reviewed_date=None,
        entity_refs=(),
    )
    action = ActionItem(
        id="action-1",
        program_id="demo",
        text="Complete the dependency handoff checklist.",
        owner_alias="demo",
        due_date=None,
        status=ActionStatus.OPEN,
        source_signal_id=None,
        source_type=ActionSourceType.MANUAL,
        linked_work_item_ids=(),
        linked_claim_id=None,
        linked_risk_id="risk-1",
        workstream_id=None,
        created_at=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        resolved_at=None,
        resolution_note=None,
    )
    decision = DecisionEntry(
        id="decision-1",
        program_id="demo",
        title="Dependency handoff owner",
        context="Clarify who owns the remaining cross-team handoff.",
        decision="Owner stays with deployment lead.",
        rationale=None,
        alternatives_considered=(),
        decided_by="demo",
        decision_date=date(2026, 5, 12),
        status=DecisionStatus.PROPOSED,
        superseded_by=None,
        linked_claim_id=None,
        linked_risk_id=None,
        linked_action_ids=("action-1",),
        workstream_id=None,
        entity_refs=(),
    )

    chains = build_raid_chain_index_from_entries(
        risks=(risk,),
        actions=(action,),
        decisions=(decision,),
        assumptions=(),
    )

    chain = chains["risk-1"]
    assert chain.has_mitigating_action is False
    assert chain.warnings == (
        "Cycle detected in RAID chain: risk-1 -> action-1 -> decision-1 -> action-1. Chain truncated at cycle entry.",
    )


def test_build_raid_chain_index_uses_program_facts(monkeypatch, tmp_path) -> None:
    action_snapshot = object()
    assumption_snapshot = object()
    decision_snapshot = object()
    risk_snapshot = object()
    captured: list[tuple[str, tuple[str, ...]]] = []

    monkeypatch.setattr(
        raid_graph_module,
        "load_program_facts",
        lambda program_id, *, programs_root, fact_types: captured.append((program_id, fact_types)) or {
            ("action.item",): action_snapshot,
            ("assumption.entry",): assumption_snapshot,
            ("decision.entry",): decision_snapshot,
            ("risk.entry",): risk_snapshot,
        }[fact_types],
    )
    monkeypatch.setattr(raid_graph_module, "project_action_items", lambda snapshot: () if snapshot is action_snapshot else ())
    monkeypatch.setattr(raid_graph_module, "project_assumptions", lambda snapshot: () if snapshot is assumption_snapshot else ())
    monkeypatch.setattr(raid_graph_module, "project_decision_entries", lambda snapshot: () if snapshot is decision_snapshot else ())
    monkeypatch.setattr(raid_graph_module, "project_risk_entries", lambda snapshot: () if snapshot is risk_snapshot else ())

    chains = raid_graph_module.build_raid_chain_index("demo", programs_root=tmp_path / "programs")

    assert chains == {}
    assert captured == [
        ("demo", ("risk.entry",)),
        ("demo", ("action.item",)),
        ("demo", ("decision.entry",)),
        ("demo", ("assumption.entry",)),
    ]