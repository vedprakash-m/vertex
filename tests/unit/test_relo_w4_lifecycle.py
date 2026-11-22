from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src.core.action_tracker import append_action, load_actions
from src.core.brief_intervention_store import (
    BriefInterventionStatus,
    append_brief_intervention_resolution,
    load_brief_intervention_resolutions,
)
from src.core.claim_tracker import append_claim_entry, load_claim_entries
from src.core.decision_register import load_decisions, save_decisions
from src.core.dependency_graph import load_dependencies, save_dependencies
from src.core.models_v2 import (
    ActionItem,
    ActionSourceType,
    ActionStatus,
    ClaimEntry,
    DecisionEntry,
    DecisionStatus,
    Dependency,
    DependencyEvidenceTier,
    DependencyScheduleStatus,
    DependencyStatus,
    DependencyType,
    LifecycleEventKind,
    RiskCategory,
    RiskEntry,
    RiskImpact,
    RiskProbability,
    RiskStatus,
)
from src.core.program_fact_store import (
    FactPrecedence,
    ProgramFactInput,
    ProgramFactStore,
)
from src.core.risk_register_engine import (
    load_risk_lifecycle,
    load_risk_register,
    record_risk_lifecycle,
    save_risk_register,
)
from src.core.dependency_graph import load_dependency_lifecycle, record_dependency_lifecycle
from src.core.decision_register import load_decision_lifecycle, record_decision_lifecycle
from src.core.action_tracker import load_action_lifecycle, record_action_lifecycle
from src.core.claim_tracker import load_claim_lifecycle, record_claim_lifecycle
from src.core.brief_intervention_store import (
    load_intervention_lifecycle,
    record_intervention_lifecycle,
)


NOW = datetime(2026, 2, 1, 12, tzinfo=timezone.utc)


def _risk() -> RiskEntry:
    return RiskEntry(
        id="risk-1",
        program_id="acme",
        title="Migration deadline",
        description="The migration may miss the deadline.",
        probability=RiskProbability.LIKELY,
        impact=RiskImpact.HIGH,
        category=RiskCategory.SCHEDULE,
        owner_alias="owner",
        mitigation_plan="Move validation earlier.",
        mitigation_due_date=date(2026, 2, 5),
        linked_workstream_ids=(),
        linked_work_item_ids=(),
        linked_milestone_ids=(),
        linked_claim_ids=(),
        linked_action_ids=(),
        status=RiskStatus.OPEN,
        identified_date=date(2026, 1, 1),
        identified_in_vertex_issue=1,
        last_reviewed_date=date(2026, 1, 1),
        entity_refs=(),
    )


def _dependency() -> Dependency:
    return Dependency(
        id="dependency-1",
        from_program_id="acme",
        from_workstream_id="source",
        from_item_id=None,
        from_milestone_id=None,
        to_program_id="acme",
        to_workstream_id="target",
        to_item_id=None,
        to_milestone_id=None,
        dependency_type=DependencyType.BLOCKS,
        risk_if_broken="Target launch slips.",
        mitigation="Run a parallel route.",
        status=DependencyStatus.ACTIVE,
        owner_alias="owner",
        planned_resolution_date=date(2026, 2, 10),
        schedule_status=DependencyScheduleStatus.AT_RISK,
        evidence_tier=DependencyEvidenceTier.AUTHORED,
    )


def _decision() -> DecisionEntry:
    return DecisionEntry(
        id="decision-1",
        program_id="acme",
        title="Use the new gateway",
        context="The old gateway is ending.",
        decision="Move traffic to the new gateway.",
        rationale="It removes the known failure mode.",
        alternatives_considered=(),
        decided_by="owner",
        decision_date=date(2026, 1, 10),
        status=DecisionStatus.DECIDED,
        superseded_by=None,
        linked_claim_id=None,
        linked_risk_id="risk-1",
        linked_action_ids=(),
        workstream_id="source",
        entity_refs=("RISK:risk-1",),
        review_by=date(2026, 2, 20),
    )


def _action() -> ActionItem:
    return ActionItem(
        id="action-1",
        program_id="acme",
        text="Run the gateway validation.",
        owner_alias="owner",
        due_date=date(2026, 2, 3),
        status=ActionStatus.OPEN,
        source_signal_id=None,
        source_type=ActionSourceType.MANUAL,
        linked_work_item_ids=(),
        linked_claim_id=None,
        linked_risk_id="risk-1",
        workstream_id="source",
        created_at=NOW - timedelta(days=2),
        resolved_at=None,
        resolution_note=None,
    )


def _claim() -> ClaimEntry:
    return ClaimEntry(
        id="claim-1",
        program_id="acme",
        edition_id="weekly",
        issue_number=1,
        workstream_id="source",
        text="The migration will complete by February 3.",
        entity_refs=("RISK:risk-1",),
        claim_date=date(2026, 1, 20),
        owner_alias="owner",
        due_date=date(2026, 2, 3),
    )


def test_risk_closed_loop_breach_effectiveness_revisit_and_closure(tmp_path: Path) -> None:
    roots = tmp_path / "programs"
    save_risk_register("acme", (_risk(),), roots)

    record_risk_lifecycle(
        "acme",
        "risk-1",
        LifecycleEventKind.BREACH,
        actor="owner",
        evidence_refs=("evidence-1",),
        occurred_at=NOW,
        programs_root=roots,
    )
    record_risk_lifecycle(
        "acme",
        "risk-1",
        LifecycleEventKind.MITIGATION_EFFECTIVENESS,
        actor="owner",
        effective=True,
        note="Parallel route removed the constraint.",
        occurred_at=NOW + timedelta(hours=1),
        programs_root=roots,
    )
    record_risk_lifecycle(
        "acme",
        "risk-1",
        LifecycleEventKind.REVISIT,
        actor="owner",
        next_review_at=NOW + timedelta(days=7),
        occurred_at=NOW + timedelta(hours=2),
        programs_root=roots,
    )
    record_risk_lifecycle(
        "acme",
        "risk-1",
        LifecycleEventKind.CLOSURE,
        actor="owner",
        note="Constraint removed.",
        occurred_at=NOW + timedelta(hours=3),
        programs_root=roots,
    )

    risk = load_risk_register("acme", roots)[0]
    history = load_risk_lifecycle("acme", "risk-1", programs_root=roots)
    assert risk.status is RiskStatus.CLOSED
    assert risk.effectiveness == "effective"
    assert risk.outcome == "Constraint removed."
    assert [record.kind for record in history] == [
        LifecycleEventKind.BREACH,
        LifecycleEventKind.MITIGATION_EFFECTIVENESS,
        LifecycleEventKind.REVISIT,
        LifecycleEventKind.CLOSURE,
    ]
    assert history[0].evidence_refs == ("evidence-1",)


def test_remaining_domain_stores_retain_lifecycle_history_and_close(
    tmp_path: Path,
) -> None:
    roots = tmp_path / "programs"
    save_dependencies("acme", (_dependency(),), programs_root=roots)
    save_decisions("acme", (_decision(),), roots)
    append_action("acme", _action(), roots)
    append_claim_entry(_claim(), roots)
    append_brief_intervention_resolution(
        "acme",
        proposal_id="intervention-1",
        title="Escalate the launch",
        command="vertex escalate --dry-run",
        source_hash="sha256:source",
        status=BriefInterventionStatus.APPROVED,
        resolved_at=NOW,
        programs_root=roots,
    )

    for record in (
        lambda: record_dependency_lifecycle(
            "acme", "dependency-1", LifecycleEventKind.BREACH, actor="owner",
            occurred_at=NOW, programs_root=roots,
        ),
        lambda: record_decision_lifecycle(
            "acme", "decision-1", LifecycleEventKind.ESCALATION, actor="owner",
            occurred_at=NOW, programs_root=roots,
        ),
        lambda: record_action_lifecycle(
            "acme", "action-1", LifecycleEventKind.ESCALATION, actor="owner",
            occurred_at=NOW, programs_root=roots,
        ),
        lambda: record_claim_lifecycle(
            "acme", "claim-1", LifecycleEventKind.MITIGATION_EFFECTIVENESS,
            actor="owner", effective=False, occurred_at=NOW, programs_root=roots,
        ),
        lambda: record_intervention_lifecycle(
            "acme", "intervention-1", LifecycleEventKind.ESCALATION, actor="owner",
            occurred_at=NOW, programs_root=roots,
        ),
    ):
        record()

    record_dependency_lifecycle(
        "acme", "dependency-1", LifecycleEventKind.CLOSURE, actor="owner",
        note="Provider delivered.", occurred_at=NOW + timedelta(minutes=1), programs_root=roots,
    )
    record_decision_lifecycle(
        "acme", "decision-1", LifecycleEventKind.CLOSURE, actor="owner",
        note="Outcome verified.", occurred_at=NOW + timedelta(minutes=1), programs_root=roots,
    )
    record_action_lifecycle(
        "acme", "action-1", LifecycleEventKind.CLOSURE, actor="owner",
        note="Validation complete.", occurred_at=NOW + timedelta(minutes=1), programs_root=roots,
    )
    record_claim_lifecycle(
        "acme", "claim-1", LifecycleEventKind.CLOSURE, actor="owner",
        note="Delivery observed.", occurred_at=NOW + timedelta(minutes=1), programs_root=roots,
    )
    record_intervention_lifecycle(
        "acme", "intervention-1", LifecycleEventKind.CLOSURE, actor="owner",
        note="Follow-up completed.", occurred_at=NOW + timedelta(minutes=1), programs_root=roots,
    )

    assert load_dependencies("acme", roots)[0].status is DependencyStatus.RESOLVED
    assert load_decisions("acme", roots)[0].status is DecisionStatus.WITHDRAWN
    assert load_actions("acme", roots, repair_corruption=False)[0].status is ActionStatus.DONE
    assert load_claim_entries("acme", roots)[0].status == "open"
    assert load_brief_intervention_resolutions("acme", programs_root=roots)["intervention-1"].status is BriefInterventionStatus.CLOSED
    assert len(load_dependency_lifecycle("acme", "dependency-1", programs_root=roots)) == 2
    assert len(load_decision_lifecycle("acme", "decision-1", programs_root=roots)) == 2
    assert len(load_action_lifecycle("acme", "action-1", programs_root=roots)) == 2
    assert len(load_claim_lifecycle("acme", "claim-1", programs_root=roots)) == 2
    assert len(load_intervention_lifecycle("acme", "intervention-1", programs_root=roots)) == 2


def test_lifecycle_facts_are_bitemporal(tmp_path: Path) -> None:
    roots = tmp_path / "programs"
    save_risk_register("acme", (_risk(),), roots)
    record_risk_lifecycle(
        "acme", "risk-1", LifecycleEventKind.BREACH, actor="owner",
        occurred_at=NOW, programs_root=roots,
    )

    store = ProgramFactStore("acme", db_root=tmp_path)
    facts = store.snapshot(as_of=NOW + timedelta(seconds=1), valid_at=NOW + timedelta(seconds=1)).facts
    assert any(
        fact.fact_type == "risk.lifecycle" and fact.payload["event_kind"] == "breach"
        for fact in facts
    )
    assert not any(
        fact.fact_type == "risk.lifecycle"
        for fact in store.snapshot(
            as_of=NOW - timedelta(seconds=1), valid_at=NOW - timedelta(seconds=1)
        ).facts
    )
