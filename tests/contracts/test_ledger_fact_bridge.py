from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core.ledger.event_log import ConfidenceTier, EventEnvelope, TemporalConfidence, build_event_envelope, write_event
from src.core.ledger.fact_bridge import (
    append_bridged_assumption_event,
    append_bridged_commitment_event,
    append_bridged_decision_event,
    append_bridged_dependency_event,
    append_bridged_milestone_event,
    append_bridged_risk_event,
    append_bridged_workstream_event,
    build_bridge_assumption_fact_input,
    build_bridge_commitment_fact_input,
    build_bridge_dependency_fact_input,
    build_bridge_fact_input,
    build_bridge_decision_fact_input,
    build_bridge_milestone_fact_input,
    build_bridge_risk_fact_input,
    build_bridge_workstream_fact_input,
    bridge_fact_controls_for_event,
    sync_bridged_risk_corroboration,
)
from src.core.program_fact_store import ProgramFactInput, ProgramFactStore
from src.core.ledger.source_refs import OperatorAssertionRef, WorkIQRef
from src.core.truth_levels import TruthLevel
from src.core.truth_model import TruthContext, build_truth_context, derive_truth_level, load_source_authority_policy
from src.core.entity_registry import EntityRegistry
from src.core.program_reality import CanonicalEntity


def _event(confidence: ConfidenceTier, *, actor: str = "operator") -> EventEnvelope:
    now = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)
    return EventEnvelope(
        event_id=f"evt-{confidence.value}",
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=now,
        recorded_at=now,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=confidence,
        actor=actor,
        payload={"risk_id": "risk:r1", "title": "Risk one"},
        source_ref=OperatorAssertionRef(asserted_by=actor, asserted_at=now),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )


def _empty_context() -> TruthContext:
    return TruthContext(
        baseline_locked_keys=frozenset(),
        suspended_sources=frozenset(),
        corroborated_keys=frozenset(),
    )


def test_bridge_fact_controls_operator_confirmed_maps_to_human_accepted() -> None:
    controls = bridge_fact_controls_for_event(_event(ConfidenceTier.OPERATOR_CONFIRMED, actor="alex"))

    assert controls.precedence.value == "active_pm_judgment"
    assert controls.review_state.value == "accepted"
    assert controls.created_by == "ledger_bridge"
    assert controls.write_authority == "human"
    assert controls.accepted_by == "alex"


def test_bridge_fact_controls_source_authoritative_maps_to_bridge_accepted() -> None:
    controls = bridge_fact_controls_for_event(_event(ConfidenceTier.SOURCE_AUTHORITATIVE))

    assert controls.precedence.value == "verified_system_signal"
    assert controls.review_state.value == "accepted"
    assert controls.created_by == "ledger_bridge"
    assert controls.write_authority == "bridge"
    assert controls.accepted_by is None


def test_bridge_fact_controls_ai_extracted_maps_to_proposed_bridge() -> None:
    controls = bridge_fact_controls_for_event(_event(ConfidenceTier.AI_EXTRACTED))

    assert controls.precedence.value == "raw_telemetry"
    assert controls.review_state.value == "proposed"
    assert controls.created_by == "ledger_bridge"
    assert controls.write_authority == "bridge"
    assert controls.accepted_by is None


def test_bridge_fact_controls_inferred_maps_to_proposed_bridge() -> None:
    controls = bridge_fact_controls_for_event(_event(ConfidenceTier.INFERRED))

    assert controls.precedence.value == "raw_telemetry"
    assert controls.review_state.value == "proposed"
    assert controls.created_by == "ledger_bridge"
    assert controls.write_authority == "bridge"
    assert controls.accepted_by is None


def test_build_bridge_fact_input_operator_confirmed_derives_human_confirmed() -> None:
    fact = build_bridge_fact_input(
        _event(ConfidenceTier.OPERATOR_CONFIRMED, actor="alex"),
        fact_type="action.item",
        entity_refs=("WI:123",),
        payload={"source": "ado", "title": "Action one"},
    )

    assert derive_truth_level(fact, _empty_context(), policy=load_source_authority_policy()) == TruthLevel.HUMAN_CONFIRMED


def test_build_bridge_fact_input_source_authoritative_derives_source_validated() -> None:
    fact = build_bridge_fact_input(
        _event(ConfidenceTier.SOURCE_AUTHORITATIVE),
        fact_type="action.item",
        entity_refs=("WI:123",),
        payload={"source": "ado", "title": "Action one"},
    )

    assert derive_truth_level(fact, _empty_context(), policy=load_source_authority_policy()) == TruthLevel.SOURCE_VALIDATED


def test_build_bridge_fact_input_ai_extracted_stays_raw_observed_without_corroboration() -> None:
    fact = build_bridge_fact_input(
        _event(ConfidenceTier.AI_EXTRACTED),
        fact_type="action.item",
        entity_refs=("WI:123",),
        payload={"source": "workiq", "title": "Action one"},
    )

    assert derive_truth_level(fact, _empty_context(), policy=load_source_authority_policy()) == TruthLevel.RAW_OBSERVED


def test_build_bridge_fact_input_inferred_stays_raw_observed() -> None:
    fact = build_bridge_fact_input(
        _event(ConfidenceTier.INFERRED),
        fact_type="action.item",
        entity_refs=("WI:123",),
        payload={"source": "workiq", "title": "Action one"},
    )

    assert derive_truth_level(fact, _empty_context(), policy=load_source_authority_policy()) == TruthLevel.RAW_OBSERVED


def test_build_bridge_risk_fact_input_from_raised_event() -> None:
    fact = build_bridge_risk_fact_input(
        _event(
            ConfidenceTier.SOURCE_AUTHORITATIVE,
        ).__class__(
            event_id="evt-risk-raised",
            program_id="acme",
            event_type="risk.raised.v1",
            occurred_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="ado-sync",
            payload={
                "risk_id": "risk:r1",
                "title": "Supplier delay",
                "severity": "high",
                "likelihood": "possible",
                "owner_person_id": "alex",
                "workstream_id": "ws-launch",
                "description": "Vendor ship date is slipping",
            },
            source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)),
            prev_event_hash="sha256:prev",
            content_hash="sha256:content",
        )
    )

    assert fact.fact_type == "risk.entry"
    assert fact.entity_refs == ("RISK:risk:r1",)
    assert fact.payload["status"] == "open"
    assert fact.payload["impact"] == "high"
    assert fact.payload["probability"] == "possible"
    assert fact.payload["linked_workstream_ids"] == ["ws-launch"]


def test_build_bridge_risk_fact_input_status_change_updates_existing_fact() -> None:
    current = ProgramFactInput(
        fact_type="risk.entry",
        entity_refs=("RISK:risk:r1",),
        payload={
            "id": "risk:r1",
            "program_id": "acme",
            "title": "Supplier delay",
            "description": "Vendor ship date is slipping",
            "probability": "possible",
            "impact": "high",
            "category": "technical",
            "owner_alias": "alex",
            "mitigation_plan": None,
            "mitigation_due_date": None,
            "linked_workstream_ids": ["ws-launch"],
            "linked_work_item_ids": [],
            "linked_milestone_ids": [],
            "linked_claim_ids": [],
            "linked_action_ids": [],
            "status": "open",
            "identified_date": "2026-06-10",
            "identified_in_vertex_issue": None,
            "last_reviewed_date": "2026-06-10",
            "entity_refs": ["RISK:risk:r1"],
            "source_signal_ids": [],
        },
    )
    event = EventEnvelope(
        event_id="evt-risk-status",
        program_id="acme",
        event_type="risk.status_changed.v1",
        occurred_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="operator",
        payload={"risk_id": "risk:r1", "new_status": "accepted", "severity": "medium"},
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    fact = build_bridge_risk_fact_input(event, current_fact=current)

    assert fact.payload["status"] == "accepted"
    assert fact.payload["impact"] == "medium"
    assert fact.write_authority == "human"


def test_build_bridge_risk_fact_input_owner_change_updates_existing_fact() -> None:
    current = ProgramFactInput(
        fact_type="risk.entry",
        entity_refs=("RISK:risk:r1",),
        payload={
            "id": "risk:r1",
            "program_id": "acme",
            "title": "Supplier delay",
            "description": "Vendor ship date is slipping",
            "probability": "possible",
            "impact": "high",
            "category": "technical",
            "owner_alias": "alex",
            "mitigation_plan": None,
            "mitigation_due_date": None,
            "linked_workstream_ids": ["ws-launch"],
            "linked_work_item_ids": [],
            "linked_milestone_ids": [],
            "linked_claim_ids": [],
            "linked_action_ids": [],
            "status": "open",
            "identified_date": "2026-06-10",
            "identified_in_vertex_issue": None,
            "last_reviewed_date": "2026-06-10",
            "entity_refs": ["RISK:risk:r1"],
            "source_signal_ids": [],
        },
    )
    event = EventEnvelope(
        event_id="evt-risk-owner",
        program_id="acme",
        event_type="risk.owner_changed.v1",
        occurred_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={"risk_id": "risk:r1", "new_owner_person_id": "jamie"},
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    fact = build_bridge_risk_fact_input(event, current_fact=current)

    assert fact.payload["owner_alias"] == "jamie"


def test_build_bridge_risk_fact_input_mitigated_updates_existing_fact() -> None:
    current = ProgramFactInput(
        fact_type="risk.entry",
        entity_refs=("RISK:risk:r1",),
        payload={
            "id": "risk:r1",
            "program_id": "acme",
            "title": "Supplier delay",
            "description": "Vendor ship date is slipping",
            "probability": "possible",
            "impact": "high",
            "category": "technical",
            "owner_alias": "alex",
            "mitigation_plan": None,
            "mitigation_due_date": None,
            "linked_workstream_ids": ["ws-launch"],
            "linked_work_item_ids": [],
            "linked_milestone_ids": [],
            "linked_claim_ids": [],
            "linked_action_ids": [],
            "status": "open",
            "identified_date": "2026-06-10",
            "identified_in_vertex_issue": None,
            "last_reviewed_date": "2026-06-10",
            "entity_refs": ["RISK:risk:r1"],
            "source_signal_ids": [],
        },
    )
    event = EventEnvelope(
        event_id="evt-risk-mitigated",
        program_id="acme",
        event_type="risk.mitigated.v1",
        occurred_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="operator",
        payload={"risk_id": "risk:r1", "mitigation_summary": "Weekly escalation", "mitigated_by": "action-1"},
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    fact = build_bridge_risk_fact_input(event, current_fact=current)

    assert fact.payload["status"] == "mitigated"
    assert fact.payload["mitigation_plan"] == "Weekly escalation"
    assert fact.payload["linked_action_ids"] == ["action-1"]


def test_build_bridge_risk_fact_input_closed_updates_existing_fact() -> None:
    current = ProgramFactInput(
        fact_type="risk.entry",
        entity_refs=("RISK:risk:r1",),
        payload={
            "id": "risk:r1",
            "program_id": "acme",
            "title": "Supplier delay",
            "description": "Vendor ship date is slipping",
            "probability": "possible",
            "impact": "high",
            "category": "technical",
            "owner_alias": "alex",
            "mitigation_plan": "Weekly escalation",
            "mitigation_due_date": None,
            "linked_workstream_ids": ["ws-launch"],
            "linked_work_item_ids": [],
            "linked_milestone_ids": [],
            "linked_claim_ids": [],
            "linked_action_ids": ["action-1"],
            "status": "mitigated",
            "identified_date": "2026-06-10",
            "identified_in_vertex_issue": None,
            "last_reviewed_date": "2026-06-10",
            "entity_refs": ["RISK:risk:r1"],
            "source_signal_ids": [],
        },
    )
    event = EventEnvelope(
        event_id="evt-risk-closed",
        program_id="acme",
        event_type="risk.closed.v1",
        occurred_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={"risk_id": "risk:r1", "closure_reason": "mitigated"},
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    fact = build_bridge_risk_fact_input(event, current_fact=current)

    assert fact.payload["status"] == "closed"
    assert fact.payload["closure_reason"] == "mitigated"


def test_build_bridge_risk_fact_input_requires_current_fact_for_non_creation_events() -> None:
    event = EventEnvelope(
        event_id="evt-risk-status-missing-base",
        program_id="acme",
        event_type="risk.status_changed.v1",
        occurred_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={"risk_id": "risk:r1", "new_status": "accepted"},
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    with pytest.raises(ValueError, match="requires an existing risk.entry fact"):
        build_bridge_risk_fact_input(event)


def test_append_bridged_risk_event_creates_fact_revision(tmp_path) -> None:
    db_root = tmp_path
    event = EventEnvelope(
        event_id="evt-risk-raised-append",
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={
            "risk_id": "risk:r2",
            "title": "Deployment gate drift",
            "severity": "high",
            "description": "Validation backlog is growing",
        },
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    result = append_bridged_risk_event(event, db_root=db_root)
    snapshot = ProgramFactStore("acme", db_root=db_root).snapshot(as_of=event.recorded_at)

    assert result.action == "created"
    assert len(snapshot.facts) == 1
    assert snapshot.facts[0].payload["id"] == "risk:r2"
    assert snapshot.facts[0].write_authority == "bridge"


def test_append_bridged_risk_event_noops_against_existing_matching_fact(tmp_path) -> None:
    db_root = tmp_path
    event = EventEnvelope(
        event_id="evt-risk-raised-noop",
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={
            "risk_id": "risk:r3",
            "title": "Deployment gate drift",
            "severity": "high",
            "description": "Validation backlog is growing",
        },
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )
    existing = build_bridge_risk_fact_input(event)
    store = ProgramFactStore("acme", db_root=db_root)
    store.append_fact(
        ProgramFactInput(
            fact_type=existing.fact_type,
            entity_refs=existing.entity_refs,
            payload=existing.payload,
            scope=existing.scope,
            source_signal_ids=existing.source_signal_ids,
            confidence=existing.confidence,
            precedence=existing.precedence,
            review_state=existing.review_state,
            natural_key=existing.natural_key,
            created_by="vertex.risk_register",
            privacy_classification=existing.privacy_classification,
            accepted_by=existing.accepted_by,
            write_authority=existing.write_authority,
        ),
        recorded_at=event.recorded_at,
    )

    result = append_bridged_risk_event(event, db_root=db_root)
    snapshot = store.snapshot(as_of=event.recorded_at)

    assert result.action == "noop"
    assert len(snapshot.facts) == 1


def test_build_bridge_decision_fact_input_from_made_event() -> None:
    event = EventEnvelope(
        event_id="evt-decision-made",
        program_id="acme",
        event_type="decision.made.v1",
        occurred_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={
            "decision_id": "decision:d1",
            "title": "Ship pilot",
            "decision_text": "Ship after review",
            "decided_by": ["operator", "alex"],
            "forum": "LT",
            "alternatives_considered": ["delay", "scope cut"],
        },
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    fact = build_bridge_decision_fact_input(event)

    assert fact.fact_type == "decision.entry"
    assert fact.entity_refs == ("DECISION:decision:d1",)
    assert fact.payload["status"] == "decided"
    assert fact.payload["context"] == "LT"
    assert fact.payload["decided_by"] == "operator, alex"


def test_build_bridge_decision_fact_input_revision_updates_existing_fact() -> None:
    current = ProgramFactInput(
        fact_type="decision.entry",
        entity_refs=("DECISION:decision:d1",),
        payload={
            "id": "decision:d1",
            "program_id": "acme",
            "title": "Ship pilot",
            "context": "LT",
            "decision": "Ship now",
            "rationale": None,
            "alternatives_considered": [],
            "decided_by": "operator",
            "decision_date": "2026-06-10",
            "status": "decided",
            "superseded_by": None,
            "linked_claim_id": None,
            "linked_risk_id": None,
            "linked_action_ids": [],
            "workstream_id": None,
            "entity_refs": ["DECISION:decision:d1"],
            "review_by": None,
            "linked_milestone_ids": [],
            "last_reviewed_date": None,
            "expected_outcome_refs": [],
        },
    )
    event = EventEnvelope(
        event_id="evt-decision-revised",
        program_id="acme",
        event_type="decision.revised.v1",
        occurred_at=datetime(2026, 6, 10, 13, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 13, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="operator",
        payload={"decision_id": "decision:d1", "revision_text": "Ship after mitigation", "reason": "Need one more check"},
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 6, 11, 13, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    fact = build_bridge_decision_fact_input(event, current_fact=current)

    assert fact.payload["decision"] == "Ship after mitigation"
    assert fact.payload["rationale"] == "Need one more check"
    assert fact.payload["status"] == "decided"
    assert fact.write_authority == "human"


def test_build_bridge_decision_fact_input_superseded_updates_existing_fact() -> None:
    current = ProgramFactInput(
        fact_type="decision.entry",
        entity_refs=("DECISION:decision:d1",),
        payload={
            "id": "decision:d1",
            "program_id": "acme",
            "title": "Ship pilot",
            "context": "LT",
            "decision": "Ship after mitigation",
            "rationale": "Need one more check",
            "alternatives_considered": [],
            "decided_by": "operator",
            "decision_date": "2026-06-10",
            "status": "decided",
            "superseded_by": None,
            "linked_claim_id": None,
            "linked_risk_id": None,
            "linked_action_ids": [],
            "workstream_id": None,
            "entity_refs": ["DECISION:decision:d1"],
            "review_by": None,
            "linked_milestone_ids": [],
            "last_reviewed_date": None,
            "expected_outcome_refs": [],
        },
    )
    event = EventEnvelope(
        event_id="evt-decision-superseded",
        program_id="acme",
        event_type="decision.superseded.v1",
        occurred_at=datetime(2026, 6, 10, 14, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 14, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={"decision_id": "decision:d1", "supersedes_decision_id": "decision:d0", "reason": "new plan"},
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 11, 14, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    fact = build_bridge_decision_fact_input(event, current_fact=current)

    assert fact.payload["status"] == "superseded"
    assert fact.payload["superseded_by"] == "decision:d0"
    assert fact.payload["rationale"] == "new plan"


def test_build_bridge_decision_fact_input_requires_current_fact_for_updates() -> None:
    event = EventEnvelope(
        event_id="evt-decision-revised-missing-base",
        program_id="acme",
        event_type="decision.revised.v1",
        occurred_at=datetime(2026, 6, 10, 13, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 13, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="operator",
        payload={"decision_id": "decision:d1", "revision_text": "Ship after mitigation", "reason": "Need one more check"},
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 6, 11, 13, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    with pytest.raises(ValueError, match="requires an existing decision.entry fact"):
        build_bridge_decision_fact_input(event)


def test_append_bridged_decision_event_creates_fact_revision(tmp_path) -> None:
    db_root = tmp_path
    event = EventEnvelope(
        event_id="evt-decision-made-append",
        program_id="acme",
        event_type="decision.made.v1",
        occurred_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={
            "decision_id": "decision:d2",
            "title": "Launch pilot",
            "decision_text": "Launch after review",
            "decided_by": ["alex"],
        },
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    result = append_bridged_decision_event(event, db_root=db_root)
    snapshot = ProgramFactStore("acme", db_root=db_root).snapshot(as_of=event.recorded_at)

    assert result.action == "created"
    assert len(snapshot.facts) == 1
    assert snapshot.facts[0].fact_type == "decision.entry"
    assert snapshot.facts[0].payload["id"] == "decision:d2"


def test_build_bridge_assumption_fact_input_from_stated_event() -> None:
    event = EventEnvelope(
        event_id="evt-assumption-stated",
        program_id="acme",
        event_type="assumption.stated.v1",
        occurred_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={"assumption_id": "assumption:a1", "statement": "Capacity holds", "validation_plan": "Load test"},
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    fact = build_bridge_assumption_fact_input(event)

    assert fact.fact_type == "assumption.entry"
    assert fact.entity_refs == ("ASSUMPTION:assumption:a1",)
    assert fact.payload["status"] == "unvalidated"
    assert fact.payload["validation_method"] == "Load test"


def test_build_bridge_assumption_fact_input_validated_updates_existing_fact() -> None:
    current = ProgramFactInput(
        fact_type="assumption.entry",
        entity_refs=("ASSUMPTION:assumption:a1",),
        payload={
            "id": "assumption:a1",
            "program_id": "acme",
            "text": "Capacity holds",
            "validation_method": "Load test",
            "validation_due": None,
            "status": "unvalidated",
            "category": None,
            "linked_risk_id": None,
            "linked_workstream_ids": [],
            "linked_milestone_id": None,
            "owner_alias": None,
            "identified_date": "2026-06-10",
            "entity_refs": ["ASSUMPTION:assumption:a1"],
            "resolved_date": None,
            "linked_milestone_ids": [],
            "last_reviewed_date": None,
        },
    )
    event = EventEnvelope(
        event_id="evt-assumption-validated",
        program_id="acme",
        event_type="assumption.validated.v1",
        occurred_at=datetime(2026, 6, 10, 13, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 13, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="operator",
        payload={"assumption_id": "assumption:a1", "evidence": "Load test passed"},
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 6, 11, 13, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    fact = build_bridge_assumption_fact_input(event, current_fact=current)

    assert fact.payload["status"] == "confirmed"
    assert fact.payload["validation_method"] == "Load test passed"
    assert fact.write_authority == "human"


def test_build_bridge_assumption_fact_input_invalidated_updates_existing_fact() -> None:
    current = ProgramFactInput(
        fact_type="assumption.entry",
        entity_refs=("ASSUMPTION:assumption:a1",),
        payload={
            "id": "assumption:a1",
            "program_id": "acme",
            "text": "Capacity holds",
            "validation_method": "Load test",
            "validation_due": None,
            "status": "unvalidated",
            "category": None,
            "linked_risk_id": None,
            "linked_workstream_ids": [],
            "linked_milestone_id": None,
            "owner_alias": None,
            "identified_date": "2026-06-10",
            "entity_refs": ["ASSUMPTION:assumption:a1"],
            "resolved_date": None,
            "linked_milestone_ids": [],
            "last_reviewed_date": None,
        },
    )
    event = EventEnvelope(
        event_id="evt-assumption-invalidated",
        program_id="acme",
        event_type="assumption.invalidated.v1",
        occurred_at=datetime(2026, 6, 10, 14, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 14, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={"assumption_id": "assumption:a1", "evidence": "Load test failed", "impact": "Need redesign"},
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 11, 14, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    fact = build_bridge_assumption_fact_input(event, current_fact=current)

    assert fact.payload["status"] == "invalidated"
    assert fact.payload["validation_method"] == "Load test failed"
    assert fact.payload["category"] == "Need redesign"


def test_build_bridge_assumption_fact_input_requires_current_fact_for_updates() -> None:
    event = EventEnvelope(
        event_id="evt-assumption-validated-missing-base",
        program_id="acme",
        event_type="assumption.validated.v1",
        occurred_at=datetime(2026, 6, 10, 13, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 13, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="operator",
        payload={"assumption_id": "assumption:a1", "evidence": "Load test passed"},
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 6, 11, 13, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    with pytest.raises(ValueError, match="requires an existing assumption.entry fact"):
        build_bridge_assumption_fact_input(event)


def test_append_bridged_assumption_event_creates_fact_revision(tmp_path) -> None:
    db_root = tmp_path
    event = EventEnvelope(
        event_id="evt-assumption-stated-append",
        program_id="acme",
        event_type="assumption.stated.v1",
        occurred_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={"assumption_id": "assumption:a2", "statement": "Supply holds"},
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    result = append_bridged_assumption_event(event, db_root=db_root)
    snapshot = ProgramFactStore("acme", db_root=db_root).snapshot(as_of=event.recorded_at)

    assert result.action == "created"
    assert len(snapshot.facts) == 1
    assert snapshot.facts[0].fact_type == "assumption.entry"
    assert snapshot.facts[0].payload["id"] == "assumption:a2"


def test_build_bridge_milestone_fact_input_from_created_event() -> None:
    event = EventEnvelope(
        event_id="evt-milestone-created",
        program_id="acme",
        event_type="milestone.created.v1",
        occurred_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={
            "milestone_id": "milestone:m1",
            "name": "Pilot ready",
            "target_date": "2026-07-01",
            "workstream_id": "ws-launch",
            "exit_criteria": ["Go live approved"],
        },
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    fact = build_bridge_milestone_fact_input(event)

    assert fact.fact_type == "milestone.entry"
    assert fact.entity_refs == ("MILESTONE:milestone:m1",)
    assert fact.payload["status"] == "on_track"
    assert fact.payload["linked_workstream_ids"] == ["ws-launch"]


def test_build_bridge_milestone_fact_input_date_revision_updates_existing_fact() -> None:
    current = ProgramFactInput(
        fact_type="milestone.entry",
        entity_refs=("MILESTONE:milestone:m1",),
        payload={
            "id": "milestone:m1",
            "program_id": "acme",
            "name": "Pilot ready",
            "target_date": "2026-07-01",
            "owner_alias": "unknown",
            "status": "on_track",
            "exit_criteria": ["Go live approved"],
            "linked_workstream_ids": ["ws-launch"],
            "linked_work_item_ids": [],
            "notes": None,
            "last_reviewed_date": None,
        },
    )
    event = EventEnvelope(
        event_id="evt-milestone-date-revised",
        program_id="acme",
        event_type="milestone.date_revised.v1",
        occurred_at=datetime(2026, 6, 10, 13, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 13, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="operator",
        payload={"milestone_id": "milestone:m1", "new_target_date": "2026-07-15", "reason": "Validation slipped"},
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 6, 11, 13, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    fact = build_bridge_milestone_fact_input(event, current_fact=current)

    assert fact.payload["target_date"] == "2026-07-15"
    assert fact.payload["notes"] == "Validation slipped"
    assert fact.write_authority == "human"


def test_build_bridge_milestone_fact_input_status_change_updates_existing_fact() -> None:
    current = ProgramFactInput(
        fact_type="milestone.entry",
        entity_refs=("MILESTONE:milestone:m1",),
        payload={
            "id": "milestone:m1",
            "program_id": "acme",
            "name": "Pilot ready",
            "target_date": "2026-07-15",
            "owner_alias": "unknown",
            "status": "on_track",
            "exit_criteria": ["Go live approved"],
            "linked_workstream_ids": ["ws-launch"],
            "linked_work_item_ids": [],
            "notes": None,
            "last_reviewed_date": None,
        },
    )
    event = EventEnvelope(
        event_id="evt-milestone-status-changed",
        program_id="acme",
        event_type="milestone.status_changed.v1",
        occurred_at=datetime(2026, 6, 10, 14, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 14, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={"milestone_id": "milestone:m1", "new_status": "at_risk", "reason": "Dependency late"},
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 11, 14, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    fact = build_bridge_milestone_fact_input(event, current_fact=current)

    assert fact.payload["status"] == "at_risk"
    assert fact.payload["notes"] == "Dependency late"


def test_build_bridge_milestone_fact_input_completed_updates_existing_fact() -> None:
    current = ProgramFactInput(
        fact_type="milestone.entry",
        entity_refs=("MILESTONE:milestone:m1",),
        payload={
            "id": "milestone:m1",
            "program_id": "acme",
            "name": "Pilot ready",
            "target_date": "2026-07-15",
            "owner_alias": "unknown",
            "status": "at_risk",
            "exit_criteria": ["Go live approved"],
            "linked_workstream_ids": ["ws-launch"],
            "linked_work_item_ids": [],
            "notes": "Dependency late",
            "last_reviewed_date": None,
        },
    )
    event = EventEnvelope(
        event_id="evt-milestone-completed",
        program_id="acme",
        event_type="milestone.completed.v1",
        occurred_at=datetime(2026, 6, 10, 15, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 15, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={"milestone_id": "milestone:m1", "completed_on": "2026-06-30", "evidence": "Go live approved"},
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 11, 15, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    fact = build_bridge_milestone_fact_input(event, current_fact=current)

    assert fact.payload["status"] == "completed"
    assert fact.payload["notes"] == "Go live approved"


def test_build_bridge_milestone_fact_input_creates_stub_for_updates_without_prior_create() -> None:
    """W2-7: update events arriving before milestone.created.v1 must produce a stub-based fact.

    Mirrors program_views._ensure_milestone_stub: synthesise a minimal stub so the update
    can be applied without losing the event (out-of-order delivery / gap-fill scenario).
    """
    event = EventEnvelope(
        event_id="evt-milestone-status-missing-base",
        program_id="acme",
        event_type="milestone.status_changed.v1",
        occurred_at=datetime(2026, 6, 10, 14, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 14, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={"milestone_id": "milestone:m1", "new_status": "at_risk"},
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 11, 14, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    fact = build_bridge_milestone_fact_input(event)  # must NOT raise

    assert fact.fact_type == "milestone.entry"
    assert "MILESTONE:milestone:m1" in fact.entity_refs
    assert fact.payload["status"] == "at_risk", "event status applied on top of stub"
    assert fact.payload["id"] == "milestone:m1"
    assert fact.payload["name"] is None, "stub has no name"
    assert fact.payload["target_date"] is None, "stub has no target_date"


def test_append_bridged_milestone_event_creates_fact_revision(tmp_path) -> None:
    db_root = tmp_path
    event = EventEnvelope(
        event_id="evt-milestone-created-append",
        program_id="acme",
        event_type="milestone.created.v1",
        occurred_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={"milestone_id": "milestone:m2", "name": "Pilot GA", "target_date": "2026-08-01"},
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    result = append_bridged_milestone_event(event, db_root=db_root)
    snapshot = ProgramFactStore("acme", db_root=db_root).snapshot(as_of=event.recorded_at)

    assert result.action == "created"
    assert len(snapshot.facts) == 1
    assert snapshot.facts[0].fact_type == "milestone.entry"
    assert snapshot.facts[0].payload["id"] == "milestone:m2"


def test_build_bridge_dependency_fact_input_from_declared_event() -> None:
    event = EventEnvelope(
        event_id="evt-dependency-declared",
        program_id="acme",
        event_type="dependency.declared.v1",
        occurred_at=datetime(2026, 6, 10, 16, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 16, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={
            "dependency_id": "dependency:d1",
            "from_entity": "workstream:ws1",
            "to_entity": "milestone:m1",
            "description": "Launch depends on milestone exit",
            "needed_by": "2026-07-01",
        },
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 11, 16, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    fact = build_bridge_dependency_fact_input(event)

    assert fact.fact_type == "dependency.link"
    assert fact.entity_refs == ("DEPENDENCY:dependency:d1",)
    assert fact.payload["from_workstream_id"] == "ws1"
    assert fact.payload["to_milestone_id"] == "m1"
    assert fact.payload["status"] == "active"
    assert fact.payload["schedule_status"] == "ok"


def test_build_bridge_dependency_fact_input_status_change_updates_existing_fact() -> None:
    current = ProgramFactInput(
        fact_type="dependency.link",
        entity_refs=("DEPENDENCY:dependency:d1",),
        payload={
            "id": "dependency:d1",
            "from_program_id": "acme",
            "from_workstream_id": "ws1",
            "from_item_id": None,
            "from_milestone_id": None,
            "to_program_id": "acme",
            "to_workstream_id": None,
            "to_item_id": None,
            "to_milestone_id": "m1",
            "dependency_type": "blocks",
            "risk_if_broken": "Launch depends on milestone exit",
            "mitigation": None,
            "status": "active",
            "owner_alias": None,
            "resolution_path": None,
            "planned_resolution_date": "2026-07-01",
            "schedule_status": "ok",
            "linked_risk_ids": [],
        },
    )
    event = EventEnvelope(
        event_id="evt-dependency-status-changed",
        program_id="acme",
        event_type="dependency.status_changed.v1",
        occurred_at=datetime(2026, 6, 10, 17, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 17, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="operator",
        payload={"dependency_id": "dependency:d1", "new_status": "blocked", "reason": "Exit criteria not met"},
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 6, 11, 17, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    fact = build_bridge_dependency_fact_input(event, current_fact=current)

    assert fact.payload["status"] == "broken"
    assert fact.payload["schedule_status"] == "blocked"
    assert fact.payload["mitigation"] == "Exit criteria not met"
    assert fact.write_authority == "human"


def test_build_bridge_dependency_fact_input_requires_current_fact_for_updates() -> None:
    event = EventEnvelope(
        event_id="evt-dependency-status-missing-base",
        program_id="acme",
        event_type="dependency.status_changed.v1",
        occurred_at=datetime(2026, 6, 10, 17, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 17, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={"dependency_id": "dependency:d1", "new_status": "blocked"},
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 11, 17, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    with pytest.raises(ValueError, match="requires an existing dependency.link fact"):
        build_bridge_dependency_fact_input(event)


def test_append_bridged_dependency_event_creates_fact_revision(tmp_path) -> None:
    db_root = tmp_path
    event = EventEnvelope(
        event_id="evt-dependency-created-append",
        program_id="acme",
        event_type="dependency.declared.v1",
        occurred_at=datetime(2026, 6, 10, 16, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 16, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={
            "dependency_id": "dependency:d2",
            "from_entity": "workstream:ws2",
            "to_entity": "milestone:m2",
            "description": "GA depends on validation milestone",
        },
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 11, 16, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    result = append_bridged_dependency_event(event, db_root=db_root)
    snapshot = ProgramFactStore("acme", db_root=db_root).snapshot(as_of=event.recorded_at)

    assert result.action == "created"
    assert len(snapshot.facts) == 1
    assert snapshot.facts[0].fact_type == "dependency.link"
    assert snapshot.facts[0].payload["id"] == "dependency:d2"


def test_build_bridge_workstream_fact_input_from_created_event() -> None:
    event = EventEnvelope(
        event_id="evt-workstream-created",
        program_id="acme",
        event_type="workstream.created.v1",
        occurred_at=datetime(2026, 6, 10, 18, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 18, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={"workstream_id": "ws-launch", "name": "Launch", "owner_person_id": "person:alice"},
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 11, 18, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    fact = build_bridge_workstream_fact_input(event)

    assert fact.fact_type == "workstream.entry"
    assert fact.entity_refs == ("WS:ws-launch",)
    assert fact.payload["owner_person_id"] == "person:alice"
    assert fact.payload["status"] == "active"


def test_build_bridge_workstream_fact_input_status_change_updates_existing_fact() -> None:
    current = ProgramFactInput(
        fact_type="workstream.entry",
        entity_refs=("WS:ws-launch",),
        payload={
            "id": "ws-launch",
            "name": "Launch",
            "owner_person_id": "person:alice",
            "status": "active",
            "aliases": [],
            "area_paths": [],
            "ado_team": None,
            "ado_pipeline_ids": [],
            "ado_repository_ids": [],
            "pm_owner": None,
            "eng_owner": None,
            "accountable_owner": None,
            "accountable_email": None,
            "responsible_owners": [],
            "consulted_owners": [],
            "informed_owners": [],
            "dri_email": None,
            "alternate_owner": None,
            "always_notify": [],
            "description": None,
            "why_it_matters": None,
            "history_summary": None,
            "leadership_sensitivity": None,
            "current_blocker": None,
            "ado_saved_query_ids": [],
            "last_reviewed_date": None,
            "signal_sources": None,
        },
    )
    event = EventEnvelope(
        event_id="evt-workstream-status-changed",
        program_id="acme",
        event_type="workstream.status_changed.v1",
        occurred_at=datetime(2026, 6, 10, 19, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 19, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="operator",
        payload={"workstream_id": "ws-launch", "new_status": "blocked", "reason": "Partner sign-off missing"},
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 6, 11, 19, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    fact = build_bridge_workstream_fact_input(event, current_fact=current)

    assert fact.payload["status"] == "blocked"
    assert fact.payload["current_blocker"] == "Partner sign-off missing"
    assert fact.write_authority == "human"


def test_build_bridge_workstream_fact_input_owner_change_updates_existing_fact() -> None:
    current = ProgramFactInput(
        fact_type="workstream.entry",
        entity_refs=("WS:ws-launch",),
        payload={
            "id": "ws-launch",
            "name": "Launch",
            "owner_person_id": "person:alice",
            "status": "active",
            "aliases": [],
            "area_paths": [],
            "ado_team": None,
            "ado_pipeline_ids": [],
            "ado_repository_ids": [],
            "pm_owner": None,
            "eng_owner": None,
            "accountable_owner": None,
            "accountable_email": None,
            "responsible_owners": [],
            "consulted_owners": [],
            "informed_owners": [],
            "dri_email": None,
            "alternate_owner": None,
            "always_notify": [],
            "description": None,
            "why_it_matters": None,
            "history_summary": None,
            "leadership_sensitivity": None,
            "current_blocker": None,
            "ado_saved_query_ids": [],
            "last_reviewed_date": None,
            "signal_sources": None,
        },
    )
    event = EventEnvelope(
        event_id="evt-workstream-owner-changed",
        program_id="acme",
        event_type="workstream.owner_changed.v1",
        occurred_at=datetime(2026, 6, 10, 20, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 20, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={"workstream_id": "ws-launch", "new_owner_person_id": "person:alex"},
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 11, 20, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    fact = build_bridge_workstream_fact_input(event, current_fact=current)

    assert fact.payload["owner_person_id"] == "person:alex"


def test_build_bridge_workstream_fact_input_requires_current_fact_for_updates() -> None:
    event = EventEnvelope(
        event_id="evt-workstream-status-missing-base",
        program_id="acme",
        event_type="workstream.status_changed.v1",
        occurred_at=datetime(2026, 6, 10, 19, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 19, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={"workstream_id": "ws-launch", "new_status": "blocked"},
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 11, 19, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    with pytest.raises(ValueError, match="requires an existing workstream.entry fact"):
        build_bridge_workstream_fact_input(event)


def test_append_bridged_workstream_event_creates_fact_revision(tmp_path) -> None:
    db_root = tmp_path
    event = EventEnvelope(
        event_id="evt-workstream-created-append",
        program_id="acme",
        event_type="workstream.created.v1",
        occurred_at=datetime(2026, 6, 10, 18, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 18, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={"workstream_id": "ws-build", "name": "Build", "owner_person_id": "person:jamie"},
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 11, 18, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    result = append_bridged_workstream_event(event, db_root=db_root)
    snapshot = ProgramFactStore("acme", db_root=db_root).snapshot(as_of=event.recorded_at)

    assert result.action == "created"
    assert len(snapshot.facts) == 1
    assert snapshot.facts[0].fact_type == "workstream.entry"
    assert snapshot.facts[0].payload["id"] == "ws-build"


def _commitment_registry() -> EntityRegistry:
    return EntityRegistry(
        program_entities=(
            CanonicalEntity(
                entity_id="person:alice",
                entity_type="person",
                canonical_name="Alice Vance",
                aliases=("alice",),
                scope="program",
            ),
        ),
        org_entities=(),
    )


def test_build_bridge_commitment_fact_input_from_made_event() -> None:
    event = EventEnvelope(
        event_id="evt-commitment-made",
        program_id="acme",
        event_type="commitment.made.v1",
        occurred_at=datetime(2026, 6, 10, 21, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 21, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={
            "commitment_id": "commitment:c1",
            "text": "Ship pilot",
            "owner_person_id": "person:alice",
            "due_date": "2026-07-01",
            "made_in": "LT",
        },
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 11, 21, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    fact = build_bridge_commitment_fact_input(event, registry=_commitment_registry())

    assert fact.fact_type == "commitment.entry"
    assert fact.natural_key == "commitment.entry|commitment:c1"
    assert fact.payload["direction"] == "outbound"
    assert fact.payload["due_date"] == "2026-07-01"


def test_build_bridge_commitment_fact_input_slip_updates_existing_fact() -> None:
    current = ProgramFactInput(
        fact_type="commitment.entry",
        entity_refs=("commitment:c1",),
        payload={
            "commitment_id": "commitment:c1",
            "title": "Ship pilot",
            "dri": "person:alice",
            "due_date": "2026-07-01",
            "direction": "outbound",
            "status": "open",
            "description": "LT",
            "entity_ref": "person:alice",
            "slip_history": [],
        },
        natural_key="commitment.entry|commitment:c1",
    )
    event = EventEnvelope(
        event_id="evt-commitment-slipped",
        program_id="acme",
        event_type="commitment.slipped.v1",
        occurred_at=datetime(2026, 6, 12, 9, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 12, 9, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="operator",
        payload={"commitment_id": "commitment:c1", "new_due_date": "2026-07-15", "reason": "Validation lag"},
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 6, 12, 9, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    fact = build_bridge_commitment_fact_input(event, registry=_commitment_registry(), current_fact=current)

    assert fact.payload["due_date"] == "2026-07-15"
    assert fact.payload["status"] == "slipped"
    assert fact.payload["slip_history"][0]["old_due_date"] == "2026-07-01"
    assert fact.write_authority == "human"


def test_build_bridge_commitment_fact_input_requires_current_fact_for_updates() -> None:
    event = EventEnvelope(
        event_id="evt-commitment-slip-missing-base",
        program_id="acme",
        event_type="commitment.slipped.v1",
        occurred_at=datetime(2026, 6, 12, 9, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 12, 9, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={"commitment_id": "commitment:c1", "new_due_date": "2026-07-15"},
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 12, 9, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    with pytest.raises(ValueError, match="requires an existing commitment.entry fact"):
        build_bridge_commitment_fact_input(event, registry=_commitment_registry())


def test_build_bridge_commitment_fact_input_rejects_ambiguous_direction() -> None:
    registry = EntityRegistry(
        program_entities=(
            CanonicalEntity(
                entity_id="person:unknown",
                entity_type="mystery",
                canonical_name="Unknown Owner",
                aliases=(),
                scope="program",
            ),
        ),
        org_entities=(),
    )
    event = EventEnvelope(
        event_id="evt-commitment-made-ambiguous",
        program_id="acme",
        event_type="commitment.made.v1",
        occurred_at=datetime(2026, 6, 10, 21, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 21, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={
            "commitment_id": "commitment:c2",
            "text": "Ship pilot",
            "owner_person_id": "person:unknown",
            "due_date": "2026-07-01",
        },
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 11, 21, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    with pytest.raises(ValueError, match="could not infer direction"):
        build_bridge_commitment_fact_input(event, registry=registry)


def test_append_bridged_commitment_event_creates_fact_revision(tmp_path) -> None:
    db_root = tmp_path
    programs_root = tmp_path / "programs"
    knowledge_dir = programs_root / "acme" / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    (knowledge_dir / "entities.yaml").write_text(
        "entities:\n"
        "  - entity_id: 'person:alice'\n"
        "    entity_type: 'person'\n"
        "    canonical_name: 'Alice Vance'\n"
        "    aliases: ['alice']\n"
        "    scope: 'program'\n",
        encoding="utf-8",
    )
    event = EventEnvelope(
        event_id="evt-commitment-made-append",
        program_id="acme",
        event_type="commitment.made.v1",
        occurred_at=datetime(2026, 6, 10, 21, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 21, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={
            "commitment_id": "commitment:c3",
            "text": "Ship pilot",
            "owner_person_id": "person:alice",
            "due_date": "2026-07-01",
        },
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 11, 21, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    result = append_bridged_commitment_event(event, db_root=db_root, programs_root=programs_root)
    snapshot = ProgramFactStore("acme", db_root=db_root).snapshot(as_of=event.recorded_at)

    assert result.action == "created"
    assert len(snapshot.facts) == 1
    assert snapshot.facts[0].fact_type == "commitment.entry"
    assert snapshot.facts[0].payload["commitment_id"] == "commitment:c3"


def _commitment_registry() -> EntityRegistry:
    return EntityRegistry(
        program_entities=(
            CanonicalEntity(
                entity_id="person:alice",
                entity_type="person",
                canonical_name="Alice Vance",
                aliases=(),
                scope="program",
            ),
        ),
        org_entities=(),
    )


def test_build_bridge_commitment_fact_input_from_made_event() -> None:
    event = EventEnvelope(
        event_id="evt-commitment-made",
        program_id="acme",
        event_type="commitment.made.v1",
        occurred_at=datetime(2026, 6, 10, 21, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 21, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={
            "commitment_id": "commitment:c1",
            "text": "Ship pilot exit",
            "owner_person_id": "person:alice",
            "due_date": "2026-07-01",
            "made_in": "LT",
        },
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 11, 21, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    fact = build_bridge_commitment_fact_input(event, registry=_commitment_registry())

    assert fact.fact_type == "commitment.entry"
    assert fact.natural_key == "commitment.entry|commitment:c1"
    assert fact.payload["direction"] == "outbound"
    assert fact.payload["due_date"] == "2026-07-01"


def test_build_bridge_commitment_fact_input_rejects_ambiguous_direction() -> None:
    event = EventEnvelope(
        event_id="evt-commitment-made-ambiguous",
        program_id="acme",
        event_type="commitment.made.v1",
        occurred_at=datetime(2026, 6, 10, 21, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 21, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={
            "commitment_id": "commitment:c2",
            "text": "Ship pilot exit",
            "owner_person_id": "person:unknown",
            "due_date": "2026-07-01",
        },
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 11, 21, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    with pytest.raises(ValueError, match="could not infer direction"):
        build_bridge_commitment_fact_input(event, registry=_commitment_registry())


def test_build_bridge_commitment_fact_input_slipped_updates_existing_fact() -> None:
    current = ProgramFactInput(
        fact_type="commitment.entry",
        entity_refs=("commitment:c1",),
        payload={
            "commitment_id": "commitment:c1",
            "title": "Ship pilot exit",
            "dri": "person:alice",
            "due_date": "2026-07-01",
            "direction": "outbound",
            "status": "open",
            "description": "LT",
            "entity_ref": None,
            "slip_history": [],
        },
        natural_key="commitment.entry|commitment:c1",
    )
    event = EventEnvelope(
        event_id="evt-commitment-slipped",
        program_id="acme",
        event_type="commitment.slipped.v1",
        occurred_at=datetime(2026, 6, 12, 9, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 12, 9, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="operator",
        payload={"commitment_id": "commitment:c1", "new_due_date": "2026-07-15", "reason": "Partner slipped"},
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 6, 12, 9, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    fact = build_bridge_commitment_fact_input(event, registry=_commitment_registry(), current_fact=current)

    assert fact.payload["status"] == "slipped"
    assert fact.payload["due_date"] == "2026-07-15"
    assert fact.payload["slip_history"][0]["old_due_date"] == "2026-07-01"
    assert fact.write_authority == "human"


def test_build_bridge_commitment_fact_input_requires_current_fact_for_updates() -> None:
    event = EventEnvelope(
        event_id="evt-commitment-slipped-missing-base",
        program_id="acme",
        event_type="commitment.slipped.v1",
        occurred_at=datetime(2026, 6, 12, 9, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 12, 9, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={"commitment_id": "commitment:c1", "new_due_date": "2026-07-15"},
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 12, 9, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    with pytest.raises(ValueError, match="requires an existing commitment.entry fact"):
        build_bridge_commitment_fact_input(event, registry=_commitment_registry())


def test_append_bridged_commitment_event_creates_fact_revision(tmp_path) -> None:
    db_root = tmp_path
    programs_root = tmp_path / "programs"
    knowledge_dir = programs_root / "acme" / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    (knowledge_dir / "entities.yaml").write_text(
        "entities:\n"
        "  - entity_id: 'person:alice'\n"
        "    entity_type: 'person'\n"
        "    canonical_name: 'Alice Vance'\n"
        "    aliases: []\n"
        "    scope: 'program'\n",
        encoding="utf-8",
    )
    event = EventEnvelope(
        event_id="evt-commitment-made-append",
        program_id="acme",
        event_type="commitment.made.v1",
        occurred_at=datetime(2026, 6, 10, 21, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 21, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="ado-sync",
        payload={
            "commitment_id": "commitment:c3",
            "text": "Ship pilot exit",
            "owner_person_id": "person:alice",
            "due_date": "2026-07-01",
        },
        source_ref=OperatorAssertionRef(asserted_by="ado-sync", asserted_at=datetime(2026, 6, 11, 21, 0, tzinfo=timezone.utc)),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )

    result = append_bridged_commitment_event(event, db_root=db_root, programs_root=programs_root)
    snapshot = ProgramFactStore("acme", db_root=db_root).snapshot(as_of=event.recorded_at)

    assert result.action == "created"
    assert len(snapshot.facts) == 1
    assert snapshot.facts[0].fact_type == "commitment.entry"
    assert snapshot.facts[0].payload["commitment_id"] == "commitment:c3"


def test_sync_bridged_risk_corroboration_promotes_ai_risk_after_second_distinct_source(tmp_path) -> None:
    db_root = tmp_path
    programs_root = tmp_path / "programs"
    first = build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.AI_EXTRACTED,
        actor="workiq",
        payload={"risk_id": "risk:r4", "title": "AI risk", "severity": "high"},
        source_ref=WorkIQRef(
            artifact_id="mail-1",
            artifact_kind="email_excerpt",
            retrieved_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc),
            vault_hash="sha256:workiq-mail-1",
        ),
        dedupe_payload={"risk_id": "risk:r4", "title": "AI risk", "severity": "high"},
    )
    second = build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=datetime(2026, 6, 10, 13, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 11, 13, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.AI_EXTRACTED,
        actor="workiq",
        payload={"risk_id": "risk:r4", "title": "AI risk", "severity": "high"},
        source_ref=WorkIQRef(
            artifact_id="mail-2",
            artifact_kind="email_excerpt",
            retrieved_at=datetime(2026, 6, 11, 13, 0, tzinfo=timezone.utc),
            vault_hash="sha256:workiq-mail-2",
        ),
        dedupe_payload={"risk_id": "risk:r4", "title": "AI risk", "severity": "high"},
    )

    persisted_first = write_event(first, programs_root=programs_root).envelope
    risk_result = append_bridged_risk_event(persisted_first, db_root=db_root)
    assert risk_result.action == "created"
    assert sync_bridged_risk_corroboration(persisted_first, db_root=db_root, programs_root=programs_root) is None

    persisted_second = write_event(second, programs_root=programs_root).envelope
    corroboration_result = sync_bridged_risk_corroboration(
        persisted_second,
        db_root=db_root,
        programs_root=programs_root,
    )
    snapshot = ProgramFactStore("acme", db_root=db_root).snapshot(as_of=persisted_second.recorded_at)
    proposed = risk_result.revision
    ctx = build_truth_context("acme", fact_snapshot=snapshot)

    assert corroboration_result is not None
    corroboration_facts = [fact for fact in snapshot.facts if fact.fact_type == "fact.corroboration"]
    assert len(corroboration_facts) == 1
    assert corroboration_facts[0].payload["corroboration_count"] == 2
    assert len(corroboration_facts[0].payload["source_document_keys"]) == 2
    assert derive_truth_level(proposed, ctx, policy=load_source_authority_policy()) == TruthLevel.CORROBORATED


# ===========================================================================
# W2-4 / G-shadow-isolation: PROPOSED facts must not escape projectors
# ===========================================================================


class TestShadowIsolationProjectorDefenseInDepth:
    """W2-4 / G-shadow-isolation: defense-in-depth layer in projectors.

    The snapshot SQL already filters ``review_state=ACCEPTED``.  These tests
    confirm that the projectors themselves also exclude PROPOSED facts — so a
    snapshot constructed from mixed-state raw facts (e.g. in unit tests, in
    migration scripts, or if the SQL filter is bypassed) can never silently
    surface a shadow fact.

    Each test builds a ``ProgramFactSnapshot`` with only a PROPOSED fact and
    asserts the projector returns empty — the only safe behavior when the
    defense-in-depth layer is working correctly.
    """

    _NOW = datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)

    def _proposed_fact(self, fact_type: str, natural_key: str) -> "ProgramFactRevision":
        from src.core.program_fact_store import (
            FactLifecycleState,
            FactPrecedence,
            FactReviewState,
            ProgramFactRevision,
        )
        return ProgramFactRevision(
            revision_id="rev-proposed-001",
            fact_id="fact-proposed-001",
            program_id="acme",
            fact_type=fact_type,
            entity_refs=("WI:99",),
            scope="program",
            natural_key=natural_key,
            recorded_at=self._NOW,
            valid_from=None,
            valid_until=None,
            superseded_at=None,
            projection_history=(),
            proposed_against_revision_id=None,
            created_by="test",
            payload={"shadow": True},
            source_signal_ids=(),
            confidence=None,
            precedence=FactPrecedence.RAW_TELEMETRY,
            review_state=FactReviewState.PROPOSED,
            lifecycle_state=FactLifecycleState.ACTIVE,
        )

    def _snapshot_with_only_proposed(self, fact_type: str, natural_key: str) -> "ProgramFactSnapshot":
        from src.core.program_fact_store import ProgramFactSnapshot
        return ProgramFactSnapshot(
            program_id="acme",
            as_of=self._NOW,
            facts=(self._proposed_fact(fact_type, natural_key),),
        )

    def test_project_risk_entries_returns_empty_for_proposed_only_snapshot(self) -> None:
        from src.core.program_fact_store import project_risk_entries
        result = project_risk_entries(self._snapshot_with_only_proposed("risk.entry", "risk:shadow"))
        assert result == (), (
            "W2-4 / G-shadow-isolation: project_risk_entries must return empty when "
            "snapshot contains only PROPOSED facts (defense-in-depth layer)"
        )

    def test_project_milestones_returns_empty_for_proposed_only_snapshot(self) -> None:
        from src.core.program_fact_store import project_milestones
        result = project_milestones(self._snapshot_with_only_proposed("milestone.entry", "ms:shadow"))
        assert result == (), (
            "W2-4: project_milestones must return empty when only PROPOSED facts present"
        )

    def test_project_workstreams_returns_empty_for_proposed_only_snapshot(self) -> None:
        from src.core.program_fact_store import project_workstreams
        result = project_workstreams(self._snapshot_with_only_proposed("workstream.entry", "ws:shadow"))
        assert result == (), (
            "W2-4: project_workstreams must return empty when only PROPOSED facts present"
        )

    def test_project_dependencies_returns_empty_for_proposed_only_snapshot(self) -> None:
        from src.core.program_fact_store import project_dependencies
        result = project_dependencies(self._snapshot_with_only_proposed("dependency.link", "dep:shadow"))
        assert result == (), (
            "W2-4: project_dependencies must return empty when only PROPOSED facts present"
        )

    def test_project_decision_entries_returns_empty_for_proposed_only_snapshot(self) -> None:
        from src.core.program_fact_store import project_decision_entries
        result = project_decision_entries(self._snapshot_with_only_proposed("decision.entry", "dec:shadow"))
        assert result == (), (
            "W2-4: project_decision_entries must return empty when only PROPOSED facts present"
        )

    def test_project_assumptions_returns_empty_for_proposed_only_snapshot(self) -> None:
        from src.core.program_fact_store import project_assumptions
        result = project_assumptions(self._snapshot_with_only_proposed("assumption.entry", "asn:shadow"))
        assert result == (), (
            "W2-4: project_assumptions must return empty when only PROPOSED facts present"
        )

    def test_project_judgments_returns_empty_for_proposed_only_snapshot(self) -> None:
        from src.core.program_fact_store import project_judgments
        result = project_judgments(self._snapshot_with_only_proposed("judgment.dimension", "jdg:shadow"))
        assert result == (), (
            "W2-4: project_judgments must return empty when only PROPOSED facts present"
        )

    def test_project_workstream_associations_returns_empty_for_proposed_only_snapshot(self) -> None:
        from src.core.program_fact_store import project_workstream_associations
        result = project_workstream_associations(
            self._snapshot_with_only_proposed("workstream.association", "wsa:shadow")
        )
        assert result == (), (
            "W2-4: project_workstream_associations must return empty when only PROPOSED facts present"
        )

    def test_project_skip_issues_returns_empty_for_proposed_only_snapshot(self) -> None:
        from src.core.program_fact_store import project_skip_issues
        result = project_skip_issues(self._snapshot_with_only_proposed("skip.issue", "skip:shadow"))
        assert result == (), (
            "W2-4: project_skip_issues must return empty when only PROPOSED facts present"
        )

    def test_project_baseline_trust_events_returns_empty_for_proposed_only_snapshot(self) -> None:
        from src.core.program_fact_store import project_baseline_trust_events
        result = project_baseline_trust_events(
            self._snapshot_with_only_proposed("baseline.trust_event", "bte:shadow")
        )
        assert result == (), (
            "W2-4: project_baseline_trust_events must return empty when only PROPOSED facts present"
        )

    def test_project_action_items_returns_empty_for_proposed_only_snapshot(self) -> None:
        from src.core.program_fact_store import project_action_items
        result = project_action_items(self._snapshot_with_only_proposed("action.item", "ai:shadow"))
        assert result == (), (
            "W2-4: project_action_items must return empty when only PROPOSED facts present"
        )


class TestBridgeEventCoverageDisposition:
    """W2-5: deliverable./incident. events must never be silently dropped.

    The bridge dispatcher must log a WARNING when it receives an event type
    that is known-but-not-yet-projectable, so that operators can observe
    via log tailing that events are reaching the bridge even before the full
    projector exists.  True silence == undetectable loss.
    """

    _NOW = datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)

    def _envelope(self, event_type: str) -> EventEnvelope:
        return EventEnvelope(
            event_id="evt-w25-test",
            program_id="acme",
            event_type=event_type,
            occurred_at=self._NOW,
            recorded_at=self._NOW,
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
            actor="operator",
            payload={"stub": True},
            source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=self._NOW),
            prev_event_hash="sha256:prev",
            content_hash="sha256:content",
        )

    def test_deliverable_event_logs_warning_not_silently_dropped(
        self, tmp_path: pytest.TempPathFactory, caplog: pytest.LogCaptureFixture
    ) -> None:
        """deliverable.status_changed.v1 must emit a WARNING — not be silently ignored."""
        import logging
        from src.commands.ledger import _maybe_bridge_event_to_fact_store
        programs_root = tmp_path / "programs"
        programs_root.mkdir()
        envelope = self._envelope("deliverable.status_changed.v1")
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("VERTEX_LEDGER_FACT_BRIDGE", "1")
            with caplog.at_level(logging.WARNING, logger="src.commands.ledger"):
                _maybe_bridge_event_to_fact_store(envelope, programs_root=programs_root)
        assert any(
            "deliverable.status_changed.v1" in record.message
            for record in caplog.records
            if record.levelno >= logging.WARNING
        ), (
            "W2-5: deliverable.status_changed.v1 must emit a WARNING log "
            "— silent omission makes event loss undetectable"
        )

    def test_incident_event_logs_warning_not_silently_dropped(
        self, tmp_path: pytest.TempPathFactory, caplog: pytest.LogCaptureFixture
    ) -> None:
        """incident.opened.v1 must emit a WARNING — not be silently ignored."""
        import logging
        from src.commands.ledger import _maybe_bridge_event_to_fact_store
        programs_root = tmp_path / "programs"
        programs_root.mkdir()
        envelope = self._envelope("incident.opened.v1")
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("VERTEX_LEDGER_FACT_BRIDGE", "1")
            with caplog.at_level(logging.WARNING, logger="src.commands.ledger"):
                _maybe_bridge_event_to_fact_store(envelope, programs_root=programs_root)
        assert any(
            "incident.opened.v1" in record.message
            for record in caplog.records
            if record.levelno >= logging.WARNING
        ), (
            "W2-5: incident.opened.v1 must emit a WARNING log "
            "— silent omission makes event loss undetectable"
        )

    def test_delivery_warning_includes_event_id_and_program(
        self, tmp_path: pytest.TempPathFactory, caplog: pytest.LogCaptureFixture
    ) -> None:
        """WARNING log for unprojecteable events must contain event_id and program_id for triage."""
        import logging
        from src.commands.ledger import _maybe_bridge_event_to_fact_store
        programs_root = tmp_path / "programs"
        programs_root.mkdir()
        envelope = self._envelope("deliverable.completed.v1")
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("VERTEX_LEDGER_FACT_BRIDGE", "1")
            with caplog.at_level(logging.WARNING, logger="src.commands.ledger"):
                _maybe_bridge_event_to_fact_store(envelope, programs_root=programs_root)
        warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        combined = " ".join(warning_messages)
        assert "evt-w25-test" in combined, "W2-5: warning must include event_id for triage"
        assert "acme" in combined, "W2-5: warning must include program_id for triage"

    def test_discovery_passthrough_emits_no_warning(
        self, tmp_path: pytest.TempPathFactory, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Known-passthrough prefixes (discovery., ledger., etc.) must not produce spurious warnings."""
        import logging
        from src.commands.ledger import _maybe_bridge_event_to_fact_store
        programs_root = tmp_path / "programs"
        programs_root.mkdir()
        passthrough_types = [
            "discovery.candidate_approved.v1",
            "ledger.event_verified.v1",
            "nudge.sent.v1",
            "signal.received.v1",
            "edition.published.v1",
        ]
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("VERTEX_LEDGER_FACT_BRIDGE", "1")
            with caplog.at_level(logging.WARNING, logger="src.commands.ledger"):
                for et in passthrough_types:
                    _maybe_bridge_event_to_fact_store(
                        self._envelope(et), programs_root=programs_root
                    )
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == [], (
            "W2-5: known-passthrough event types must not produce spurious WARNING logs; "
            f"got: {[r.message for r in warnings]}"
        )
