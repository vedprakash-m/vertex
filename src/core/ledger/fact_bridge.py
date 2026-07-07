from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.claim_actuation import infer_commitment_direction
from src.core.commitment_store import build_commitment_natural_key
from src.core.entity_registry import EntityRegistry
from src.core.ledger.event_log import EventEnvelope, ConfidenceTier, read_events
from src.core.ledger.source_refs import source_document_key
from src.core.models_v2 import AssumptionStatus, DecisionStatus, DependencyScheduleStatus, DependencyStatus, DependencyType, MilestoneStatus, RiskImpact, RiskProbability, RiskStatus
from src.core.protection.supersession import apply_supersession
from src.core.program_fact_store import (
    FactLifecycleState,
    FactPrecedence,
    FactReviewState,
    ProgramFactInput,
    ProgramFactRevision,
    ProgramFactStore,
    ProgramFactWriteResult,
    build_natural_key,
)
from src.core.truth_model import get_authority_family, load_source_authority_policy


@dataclass(frozen=True, slots=True)
class BridgeFactControls:
    precedence: FactPrecedence
    review_state: FactReviewState
    created_by: str
    write_authority: str
    accepted_by: str | None


def bridge_fact_controls_for_event(event: EventEnvelope) -> BridgeFactControls:
    if event.confidence == ConfidenceTier.OPERATOR_CONFIRMED:
        return BridgeFactControls(
            precedence=FactPrecedence.ACTIVE_PM_JUDGMENT,
            review_state=FactReviewState.ACCEPTED,
            created_by="ledger_bridge",
            write_authority="human",
            accepted_by=event.actor,
        )
    if event.confidence == ConfidenceTier.SOURCE_AUTHORITATIVE:
        return BridgeFactControls(
            precedence=FactPrecedence.VERIFIED_SYSTEM_SIGNAL,
            review_state=FactReviewState.ACCEPTED,
            created_by="ledger_bridge",
            write_authority="bridge",
            accepted_by=None,
        )
    if event.confidence in {ConfidenceTier.AI_EXTRACTED, ConfidenceTier.INFERRED}:
        return BridgeFactControls(
            precedence=FactPrecedence.RAW_TELEMETRY,
            review_state=FactReviewState.PROPOSED,
            created_by="ledger_bridge",
            write_authority="bridge",
            accepted_by=None,
        )
    raise ValueError(f"Unsupported event confidence tier: {event.confidence}")


def build_bridge_fact_input(
    event: EventEnvelope,
    *,
    fact_type: str,
    entity_refs: tuple[str, ...],
    payload: dict[str, Any],
    scope: str = "program",
    source_signal_ids: tuple[str, ...] = (),
    natural_key: str | None = None,
    privacy_classification: str = "internal",
    candidate_id: str | None = None,
) -> ProgramFactInput:
    controls = bridge_fact_controls_for_event(event)
    # Always include the source ledger event_id so that fact revisions are
    # traceable back to their originating event (W2-3 / G-lineage).
    combined_signal_ids = (
        source_signal_ids
        if event.event_id in source_signal_ids
        else (event.event_id, *source_signal_ids)
    )
    # S-3 / AG-6: derive the reverse-lookup key from the originating source_ref
    # (e.g. the EML the fact was extracted from) so bridge-appended facts carry
    # the same citation lineage as authored/backfilled facts. Best-effort: a
    # malformed/unsupported source_ref must never block the fact write.
    resolved_source_document_key: str | None = None
    try:
        if event.source_ref is not None:
            resolved_source_document_key = source_document_key(event.source_ref)
    except Exception:
        resolved_source_document_key = None
    return ProgramFactInput(
        fact_type=fact_type,
        entity_refs=entity_refs,
        payload=payload,
        scope=scope,
        source_signal_ids=combined_signal_ids,
        confidence=event.confidence.value,
        precedence=controls.precedence,
        review_state=controls.review_state,
        natural_key=natural_key,
        created_by=controls.created_by,
        privacy_classification=privacy_classification,
        accepted_by=controls.accepted_by,
        write_authority=controls.write_authority,
        # W2-6 / G-lineage: typed lineage fields
        domain_event_id=event.event_id,
        candidate_id=candidate_id,
        source_document_key=resolved_source_document_key,
    )


def build_bridge_risk_fact_input(
    event: EventEnvelope,
    *,
    current_fact: ProgramFactRevision | ProgramFactInput | None = None,
) -> ProgramFactInput:
    if not event.event_type.startswith("risk."):
        raise ValueError(f"Unsupported risk bridge event type: {event.event_type}")

    payload = _bridge_risk_payload(event, current_fact=current_fact)
    return build_bridge_fact_input(
        event,
        fact_type="risk.entry",
        entity_refs=(f"RISK:{payload['id']}",),
        payload=payload,
        scope="program",
    )


def build_bridge_decision_fact_input(
    event: EventEnvelope,
    *,
    current_fact: ProgramFactRevision | ProgramFactInput | None = None,
) -> ProgramFactInput:
    if not event.event_type.startswith("decision."):
        raise ValueError(f"Unsupported decision bridge event type: {event.event_type}")

    payload = _bridge_decision_payload(event, current_fact=current_fact)
    return build_bridge_fact_input(
        event,
        fact_type="decision.entry",
        entity_refs=tuple(payload.get("entity_refs") or (f"DECISION:{payload['id']}",)),
        payload=payload,
        scope="program",
    )


def build_bridge_assumption_fact_input(
    event: EventEnvelope,
    *,
    current_fact: ProgramFactRevision | ProgramFactInput | None = None,
) -> ProgramFactInput:
    if not event.event_type.startswith("assumption."):
        raise ValueError(f"Unsupported assumption bridge event type: {event.event_type}")

    payload = _bridge_assumption_payload(event, current_fact=current_fact)
    return build_bridge_fact_input(
        event,
        fact_type="assumption.entry",
        entity_refs=tuple(payload.get("entity_refs") or (f"ASSUMPTION:{payload['id']}",)),
        payload=payload,
        scope="program",
    )


def build_bridge_milestone_fact_input(
    event: EventEnvelope,
    *,
    current_fact: ProgramFactRevision | ProgramFactInput | None = None,
) -> ProgramFactInput:
    if not event.event_type.startswith("milestone."):
        raise ValueError(f"Unsupported milestone bridge event type: {event.event_type}")

    payload = _bridge_milestone_payload(event, current_fact=current_fact)
    return build_bridge_fact_input(
        event,
        fact_type="milestone.entry",
        entity_refs=(f"MILESTONE:{payload['id']}",),
        payload=payload,
        scope="program",
    )


def build_bridge_dependency_fact_input(
    event: EventEnvelope,
    *,
    current_fact: ProgramFactRevision | ProgramFactInput | None = None,
) -> ProgramFactInput:
    if not event.event_type.startswith("dependency."):
        raise ValueError(f"Unsupported dependency bridge event type: {event.event_type}")

    payload = _bridge_dependency_payload(event, current_fact=current_fact)
    return build_bridge_fact_input(
        event,
        fact_type="dependency.link",
        entity_refs=(f"DEPENDENCY:{payload['id']}",),
        payload=payload,
        scope="program",
    )


def build_bridge_workstream_fact_input(
    event: EventEnvelope,
    *,
    current_fact: ProgramFactRevision | ProgramFactInput | None = None,
) -> ProgramFactInput:
    if not event.event_type.startswith("workstream."):
        raise ValueError(f"Unsupported workstream bridge event type: {event.event_type}")

    payload = _bridge_workstream_payload(event, current_fact=current_fact)
    return build_bridge_fact_input(
        event,
        fact_type="workstream.entry",
        entity_refs=(f"WS:{payload['id']}",),
        payload=payload,
        scope="program",
    )


def build_bridge_commitment_fact_input(
    event: EventEnvelope,
    *,
    registry: EntityRegistry,
    current_fact: ProgramFactRevision | ProgramFactInput | None = None,
) -> ProgramFactInput:
    if not event.event_type.startswith("commitment."):
        raise ValueError(f"Unsupported commitment bridge event type: {event.event_type}")

    payload = _bridge_commitment_payload(event, registry=registry, current_fact=current_fact)
    return build_bridge_fact_input(
        event,
        fact_type="commitment.entry",
        entity_refs=(payload["commitment_id"],),
        payload=payload,
        scope="program",
        natural_key=build_commitment_natural_key(payload["commitment_id"]),
    )


def _bridge_risk_payload(
    event: EventEnvelope,
    *,
    current_fact: ProgramFactRevision | ProgramFactInput | None,
) -> dict[str, Any]:
    event_payload = event.payload
    risk_id = _required_str(event_payload, "risk_id")

    if event.event_type == "risk.raised.v1":
        severity = RiskImpact.from_string(_required_str(event_payload, "severity")).value
        likelihood = event_payload.get("likelihood")
        probability = (
            RiskProbability.from_string(str(likelihood)).value
            if isinstance(likelihood, str) and likelihood.strip()
            else RiskProbability.POSSIBLE.value
        )
        owner_alias = _optional_str(event_payload, "owner_person_id") or "unknown"
        workstream_id = _optional_str(event_payload, "workstream_id")
        return {
            "id": risk_id,
            "program_id": event.program_id,
            "title": _required_str(event_payload, "title"),
            "description": _optional_str(event_payload, "description") or "",
            "probability": probability,
            "impact": severity,
            "category": "technical",
            "owner_alias": owner_alias,
            "mitigation_plan": None,
            "mitigation_due_date": None,
            "linked_workstream_ids": [workstream_id] if workstream_id is not None else [],
            "linked_work_item_ids": [],
            "linked_milestone_ids": [],
            "linked_claim_ids": [],
            "linked_action_ids": [],
            "status": RiskStatus.OPEN.value,
            "identified_date": event.occurred_at.date().isoformat(),
            "identified_in_vertex_issue": None,
            "last_reviewed_date": event.recorded_at.date().isoformat(),
            "entity_refs": [f"RISK:{risk_id}"],
            "source_signal_ids": [],
        }

    base_payload = _current_risk_payload(current_fact, risk_id=risk_id)

    if event.event_type == "risk.status_changed.v1":
        updated = dict(base_payload)
        updated["status"] = RiskStatus.from_string(_required_str(event_payload, "new_status")).value
        new_severity = _optional_str(event_payload, "severity")
        if new_severity is not None:
            updated["impact"] = RiskImpact.from_string(new_severity).value
        updated["last_reviewed_date"] = event.recorded_at.date().isoformat()
        return updated

    if event.event_type == "risk.owner_changed.v1":
        updated = dict(base_payload)
        updated["owner_alias"] = _required_str(event_payload, "new_owner_person_id")
        updated["last_reviewed_date"] = event.recorded_at.date().isoformat()
        return updated

    if event.event_type == "risk.mitigated.v1":
        updated = dict(base_payload)
        updated["status"] = RiskStatus.MITIGATED.value
        updated["mitigation_plan"] = _required_str(event_payload, "mitigation_summary")
        updated["last_reviewed_date"] = event.recorded_at.date().isoformat()
        mitigated_by = _optional_str(event_payload, "mitigated_by")
        if mitigated_by is not None:
            linked_action_ids = list(updated.get("linked_action_ids") or [])
            if mitigated_by not in linked_action_ids:
                linked_action_ids.append(mitigated_by)
            updated["linked_action_ids"] = linked_action_ids
        return updated

    if event.event_type == "risk.closed.v1":
        updated = dict(base_payload)
        updated["status"] = RiskStatus.CLOSED.value
        updated["last_reviewed_date"] = event.recorded_at.date().isoformat()
        updated["closure_reason"] = _required_str(event_payload, "closure_reason")
        return updated

    raise ValueError(f"Unsupported risk bridge event type: {event.event_type}")


def _bridge_decision_payload(
    event: EventEnvelope,
    *,
    current_fact: ProgramFactRevision | ProgramFactInput | None,
) -> dict[str, Any]:
    event_payload = event.payload
    decision_id = _required_str(event_payload, "decision_id")

    if event.event_type == "decision.made.v1":
        decided_by = event_payload.get("decided_by")
        if not isinstance(decided_by, list) or not decided_by or not all(isinstance(value, str) and value.strip() for value in decided_by):
            raise ValueError("decision.made.v1 requires a non-empty decided_by list")
        forum = _optional_str(event_payload, "forum")
        alternatives = event_payload.get("alternatives_considered") or []
        if not isinstance(alternatives, list) or not all(isinstance(value, str) for value in alternatives):
            raise ValueError("alternatives_considered must be a list of strings when present")
        return {
            "id": decision_id,
            "program_id": event.program_id,
            "title": _required_str(event_payload, "title"),
            "context": forum or "",
            "decision": _required_str(event_payload, "decision_text"),
            "rationale": None,
            "alternatives_considered": list(alternatives),
            "decided_by": ", ".join(value.strip() for value in decided_by),
            "decision_date": event.occurred_at.date().isoformat(),
            "status": DecisionStatus.DECIDED.value,
            "superseded_by": None,
            "linked_claim_id": None,
            "linked_risk_id": None,
            "linked_action_ids": [],
            "workstream_id": None,
            "entity_refs": [f"DECISION:{decision_id}"],
            "review_by": None,
            "linked_milestone_ids": [],
            "last_reviewed_date": None,
            "expected_outcome_refs": [],
        }

    base_payload = _current_decision_payload(current_fact, decision_id=decision_id)

    if event.event_type == "decision.revised.v1":
        updated = dict(base_payload)
        updated["decision"] = _required_str(event_payload, "revision_text")
        updated["rationale"] = _required_str(event_payload, "reason")
        updated["status"] = DecisionStatus.DECIDED.value
        updated["last_reviewed_date"] = event.recorded_at.date().isoformat()
        return updated

    if event.event_type == "decision.superseded.v1":
        updated = dict(base_payload)
        updated["status"] = DecisionStatus.SUPERSEDED.value
        updated["superseded_by"] = _required_str(event_payload, "supersedes_decision_id")
        updated["rationale"] = _required_str(event_payload, "reason")
        updated["last_reviewed_date"] = event.recorded_at.date().isoformat()
        return updated

    raise ValueError(f"Unsupported decision bridge event type: {event.event_type}")


def _bridge_assumption_payload(
    event: EventEnvelope,
    *,
    current_fact: ProgramFactRevision | ProgramFactInput | None,
) -> dict[str, Any]:
    event_payload = event.payload
    assumption_id = _required_str(event_payload, "assumption_id")

    if event.event_type == "assumption.stated.v1":
        validation_plan = _optional_str(event_payload, "validation_plan")
        return {
            "id": assumption_id,
            "program_id": event.program_id,
            "text": _required_str(event_payload, "statement"),
            "validation_method": validation_plan,
            "validation_due": None,
            "status": AssumptionStatus.UNVALIDATED.value,
            "category": None,
            "linked_risk_id": None,
            "linked_workstream_ids": [],
            "linked_milestone_id": None,
            "owner_alias": None,
            "identified_date": event.occurred_at.date().isoformat(),
            "entity_refs": [f"ASSUMPTION:{assumption_id}"],
            "resolved_date": None,
            "linked_milestone_ids": [],
            "last_reviewed_date": None,
        }

    base_payload = _current_assumption_payload(current_fact, assumption_id=assumption_id)

    if event.event_type == "assumption.validated.v1":
        updated = dict(base_payload)
        updated["status"] = AssumptionStatus.CONFIRMED.value
        updated["validation_method"] = _required_str(event_payload, "evidence")
        updated["last_reviewed_date"] = event.recorded_at.date().isoformat()
        return updated

    if event.event_type == "assumption.invalidated.v1":
        updated = dict(base_payload)
        updated["status"] = AssumptionStatus.INVALIDATED.value
        updated["validation_method"] = _required_str(event_payload, "evidence")
        updated["resolved_date"] = event.occurred_at.date().isoformat()
        updated["last_reviewed_date"] = event.recorded_at.date().isoformat()
        impact = _optional_str(event_payload, "impact")
        if impact is not None:
            updated["category"] = impact
        return updated

    raise ValueError(f"Unsupported assumption bridge event type: {event.event_type}")


def _bridge_milestone_payload(
    event: EventEnvelope,
    *,
    current_fact: ProgramFactRevision | ProgramFactInput | None,
) -> dict[str, Any]:
    event_payload = event.payload
    milestone_id = _required_str(event_payload, "milestone_id")

    if event.event_type == "milestone.created.v1":
        exit_criteria = event_payload.get("exit_criteria") or []
        if not isinstance(exit_criteria, list) or not all(isinstance(value, str) for value in exit_criteria):
            raise ValueError("exit_criteria must be a list of strings when present")
        workstream_id = _optional_str(event_payload, "workstream_id")
        return {
            "id": milestone_id,
            "program_id": event.program_id,
            "name": _required_str(event_payload, "name"),
            "target_date": _required_str(event_payload, "target_date"),
            "owner_alias": "unknown",
            "status": MilestoneStatus.ON_TRACK.value,
            "exit_criteria": list(exit_criteria),
            "linked_workstream_ids": [workstream_id] if workstream_id is not None else [],
            "linked_work_item_ids": [],
            "notes": None,
            "last_reviewed_date": None,
        }

    base_payload = _current_milestone_payload(current_fact, milestone_id=milestone_id, program_id=event.program_id)

    if event.event_type == "milestone.date_revised.v1":
        updated = dict(base_payload)
        updated["target_date"] = _required_str(event_payload, "new_target_date")
        updated["notes"] = _optional_str(event_payload, "reason")
        updated["last_reviewed_date"] = event.recorded_at.date().isoformat()
        return updated

    if event.event_type == "milestone.status_changed.v1":
        updated = dict(base_payload)
        updated["status"] = MilestoneStatus.from_string(_required_str(event_payload, "new_status")).value
        updated["notes"] = _optional_str(event_payload, "reason")
        updated["last_reviewed_date"] = event.recorded_at.date().isoformat()
        return updated

    if event.event_type == "milestone.completed.v1":
        updated = dict(base_payload)
        updated["status"] = MilestoneStatus.COMPLETED.value
        evidence = _optional_str(event_payload, "evidence")
        if evidence is not None:
            updated["notes"] = evidence
        updated["last_reviewed_date"] = event.recorded_at.date().isoformat()
        if updated.get("target_date") is None:
            # Source-detected (e.g. REV/email-derived) completions frequently arrive
            # with no prior milestone.created.v1 event -- there was never a formal
            # lifecycle record for the milestone, only a report that it finished.
            # _current_milestone_payload's stub leaves target_date unset in that case;
            # fall back to the completion date itself so the fact stays renderable
            # (program_fact_store._milestone_from_fact requires a non-null target_date).
            completed_on = _optional_str(event_payload, "completed_on")
            updated["target_date"] = completed_on or event.occurred_at.date().isoformat()
        return updated

    raise ValueError(f"Unsupported milestone bridge event type: {event.event_type}")


def _bridge_dependency_payload(
    event: EventEnvelope,
    *,
    current_fact: ProgramFactRevision | ProgramFactInput | None,
) -> dict[str, Any]:
    event_payload = event.payload
    dependency_id = _required_str(event_payload, "dependency_id")

    if event.event_type == "dependency.declared.v1":
        from_endpoint = _parse_dependency_entity_ref(_required_str(event_payload, "from_entity"))
        to_endpoint = _parse_dependency_entity_ref(_required_str(event_payload, "to_entity"))
        description = _optional_str(event_payload, "description")
        needed_by = _optional_str(event_payload, "needed_by")
        return {
            "id": dependency_id,
            "from_program_id": event.program_id,
            "from_workstream_id": from_endpoint["workstream_id"],
            "from_item_id": from_endpoint["item_id"],
            "from_milestone_id": from_endpoint["milestone_id"],
            "to_program_id": event.program_id,
            "to_workstream_id": to_endpoint["workstream_id"],
            "to_item_id": to_endpoint["item_id"],
            "to_milestone_id": to_endpoint["milestone_id"],
            "dependency_type": DependencyType.BLOCKS.value,
            "risk_if_broken": description or f"Dependency between {_required_str(event_payload, 'from_entity')} and {_required_str(event_payload, 'to_entity')}",
            "mitigation": None,
            "status": DependencyStatus.ACTIVE.value,
            "owner_alias": None,
            "resolution_path": None,
            "planned_resolution_date": needed_by,
            "schedule_status": DependencyScheduleStatus.OK.value,
            "linked_risk_ids": [],
        }

    base_payload = _current_dependency_payload(current_fact, dependency_id=dependency_id)

    if event.event_type == "dependency.status_changed.v1":
        updated = dict(base_payload)
        status, schedule_status = _map_dependency_status(_required_str(event_payload, "new_status"))
        updated["status"] = status
        updated["schedule_status"] = schedule_status
        reason = _optional_str(event_payload, "reason")
        if reason is not None:
            updated["mitigation"] = reason
        return updated

    raise ValueError(f"Unsupported dependency bridge event type: {event.event_type}")


def _bridge_workstream_payload(
    event: EventEnvelope,
    *,
    current_fact: ProgramFactRevision | ProgramFactInput | None,
) -> dict[str, Any]:
    event_payload = event.payload
    workstream_id = _required_str(event_payload, "workstream_id")

    if event.event_type == "workstream.created.v1":
        return {
            "id": workstream_id,
            "name": _required_str(event_payload, "name"),
            "owner_person_id": _optional_str(event_payload, "owner_person_id"),
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
            "last_reviewed_date": event.recorded_at.date().isoformat(),
            "signal_sources": None,
        }

    base_payload = _current_workstream_payload(current_fact, workstream_id=workstream_id)

    if event.event_type == "workstream.owner_changed.v1":
        updated = dict(base_payload)
        updated["owner_person_id"] = _required_str(event_payload, "new_owner_person_id")
        updated["last_reviewed_date"] = event.recorded_at.date().isoformat()
        return updated

    if event.event_type == "workstream.status_changed.v1":
        updated = dict(base_payload)
        updated["status"] = _required_str(event_payload, "new_status")
        reason = _optional_str(event_payload, "reason")
        if reason is not None:
            updated["current_blocker"] = reason
        updated["last_reviewed_date"] = event.recorded_at.date().isoformat()
        return updated

    raise ValueError(f"Unsupported workstream bridge event type: {event.event_type}")


def _bridge_commitment_payload(
    event: EventEnvelope,
    *,
    registry: EntityRegistry,
    current_fact: ProgramFactRevision | ProgramFactInput | None,
) -> dict[str, Any]:
    event_payload = event.payload
    commitment_id = _required_str(event_payload, "commitment_id")

    if event.event_type == "commitment.made.v1":
        owner_person_id = _required_str(event_payload, "owner_person_id")
        direction = infer_commitment_direction(owner_person_id, registry)
        if direction == "ambiguous":
            raise ValueError(
                f"Commitment bridge event for {commitment_id} could not infer direction from owner_person_id {owner_person_id!r}"
            )
        made_in = _optional_str(event_payload, "made_in")
        return {
            "commitment_id": commitment_id,
            "title": _required_str(event_payload, "text"),
            "dri": owner_person_id,
            "due_date": _required_str(event_payload, "due_date"),
            "direction": direction,
            "status": "open",
            "description": made_in or "",
            "entity_ref": None,
            "slip_history": [],
        }

    base_payload = _current_commitment_payload(current_fact, commitment_id=commitment_id)

    if event.event_type == "commitment.slipped.v1":
        updated = dict(base_payload)
        new_due_date = _optional_str(event_payload, "new_due_date")
        if new_due_date is not None:
            slip_history = list(updated.get("slip_history") or [])
            slip_history.append(
                {
                    "slipped_at": event.recorded_at.isoformat(),
                    "old_due_date": str(updated["due_date"]),
                    "new_due_date": new_due_date,
                    "reason": _optional_str(event_payload, "reason") or "",
                }
            )
            updated["due_date"] = new_due_date
            updated["slip_history"] = slip_history
        updated["status"] = "slipped"
        return updated

    if event.event_type == "commitment.fulfilled.v1":
        updated = dict(base_payload)
        updated["status"] = "fulfilled"
        evidence = _optional_str(event_payload, "evidence")
        if evidence is not None:
            updated["description"] = evidence
        return updated

    raise ValueError(f"Unsupported commitment bridge event type: {event.event_type}")


def _current_risk_payload(
    current_fact: ProgramFactRevision | ProgramFactInput | None,
    *,
    risk_id: str,
) -> dict[str, Any]:
    if current_fact is None:
        raise ValueError(f"Risk bridge event for {risk_id} requires an existing risk.entry fact")
    payload = dict(current_fact.payload)
    if str(payload.get("id") or "") != risk_id:
        raise ValueError(f"Risk bridge event references {risk_id} but current fact holds {payload.get('id')!r}")
    return payload


def _current_decision_payload(
    current_fact: ProgramFactRevision | ProgramFactInput | None,
    *,
    decision_id: str,
) -> dict[str, Any]:
    if current_fact is None:
        raise ValueError(f"Decision bridge event for {decision_id} requires an existing decision.entry fact")
    payload = dict(current_fact.payload)
    if str(payload.get("id") or "") != decision_id:
        raise ValueError(
            f"Decision bridge event references {decision_id} but current fact holds {payload.get('id')!r}"
        )
    return payload


def _current_assumption_payload(
    current_fact: ProgramFactRevision | ProgramFactInput | None,
    *,
    assumption_id: str,
) -> dict[str, Any]:
    if current_fact is None:
        raise ValueError(f"Assumption bridge event for {assumption_id} requires an existing assumption.entry fact")
    payload = dict(current_fact.payload)
    if str(payload.get("id") or "") != assumption_id:
        raise ValueError(
            f"Assumption bridge event references {assumption_id} but current fact holds {payload.get('id')!r}"
        )
    return payload


def _current_milestone_payload(
    current_fact: ProgramFactRevision | ProgramFactInput | None,
    *,
    milestone_id: str,
    program_id: str = "",
) -> dict[str, Any]:
    if current_fact is None:
        # Mirrors program_views._ensure_milestone_stub: a status/date/completion event
        # can arrive before the creation event (out-of-order delivery or gap in the
        # ledger).  Synthesise a minimal stub so the update can be applied.
        return {
            "id": milestone_id,
            "program_id": program_id,
            "name": None,
            "target_date": None,
            "owner_alias": "unknown",
            "status": "stub",
            "exit_criteria": [],
            "linked_workstream_ids": [],
            "linked_work_item_ids": [],
            "notes": None,
            "last_reviewed_date": None,
        }
    payload = dict(current_fact.payload)
    if str(payload.get("id") or "") != milestone_id:
        raise ValueError(
            f"Milestone bridge event references {milestone_id} but current fact holds {payload.get('id')!r}"
        )
    return payload


def _current_dependency_payload(
    current_fact: ProgramFactRevision | ProgramFactInput | None,
    *,
    dependency_id: str,
) -> dict[str, Any]:
    if current_fact is None:
        raise ValueError(f"Dependency bridge event for {dependency_id} requires an existing dependency.link fact")
    payload = dict(current_fact.payload)
    if str(payload.get("id") or "") != dependency_id:
        raise ValueError(
            f"Dependency bridge event references {dependency_id} but current fact holds {payload.get('id')!r}"
        )
    return payload


def _current_workstream_payload(
    current_fact: ProgramFactRevision | ProgramFactInput | None,
    *,
    workstream_id: str,
) -> dict[str, Any]:
    if current_fact is None:
        raise ValueError(f"Workstream bridge event for {workstream_id} requires an existing workstream.entry fact")
    payload = dict(current_fact.payload)
    if str(payload.get("id") or "") != workstream_id:
        raise ValueError(
            f"Workstream bridge event references {workstream_id} but current fact holds {payload.get('id')!r}"
        )
    return payload


def _current_commitment_payload(
    current_fact: ProgramFactRevision | ProgramFactInput | None,
    *,
    commitment_id: str,
) -> dict[str, Any]:
    if current_fact is None:
        raise ValueError(f"Commitment bridge event for {commitment_id} requires an existing commitment.entry fact")
    payload = dict(current_fact.payload)
    if str(payload.get("commitment_id") or "") != commitment_id:
        raise ValueError(
            f"Commitment bridge event references {commitment_id} but current fact holds {payload.get('commitment_id')!r}"
        )
    return payload


def _parse_dependency_entity_ref(entity_ref: str) -> dict[str, str | int | None]:
    kind, _, raw_value = entity_ref.partition(":")
    value = raw_value.strip()
    if not kind or not value:
        raise ValueError(f"Unsupported dependency entity ref: {entity_ref!r}")

    if kind == "workstream":
        return {"workstream_id": value, "item_id": None, "milestone_id": None}
    if kind == "milestone":
        return {"workstream_id": None, "item_id": None, "milestone_id": value}
    if kind in {"workitem", "work_item", "ado"}:
        try:
            item_id = int(value)
        except ValueError as error:
            raise ValueError(f"Dependency entity ref {entity_ref!r} requires an integer work item id") from error
        return {"workstream_id": None, "item_id": item_id, "milestone_id": None}
    raise ValueError(f"Unsupported dependency entity ref: {entity_ref!r}")


def _map_dependency_status(status: str) -> tuple[str, str]:
    normalized = status.strip().lower()
    if normalized == "on_track":
        return DependencyStatus.ACTIVE.value, DependencyScheduleStatus.OK.value
    if normalized == "at_risk":
        return DependencyStatus.ACTIVE.value, DependencyScheduleStatus.AT_RISK.value
    if normalized == "blocked":
        return DependencyStatus.BROKEN.value, DependencyScheduleStatus.BLOCKED.value
    if normalized == "delivered":
        return DependencyStatus.RESOLVED.value, DependencyScheduleStatus.OK.value
    raise ValueError(f"Unsupported dependency status: {status}")


def _required_str(payload: dict[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing required string field: {field_name}")
    return value.strip()


def _optional_str(payload: dict[str, Any], field_name: str) -> str | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Optional field {field_name} must be a non-empty string when present")
    return value.strip()


def append_bridged_risk_event(
    event: EventEnvelope,
    *,
    db_root: Path,
) -> ProgramFactWriteResult:
    if not event.event_type.startswith("risk."):
        raise ValueError(f"Unsupported risk bridge event type: {event.event_type}")

    store = ProgramFactStore(event.program_id, db_root=db_root)
    current_fact = _load_current_risk_fact(store, event)
    fact_input = build_bridge_risk_fact_input(event, current_fact=current_fact)
    return store.append_fact(fact_input, recorded_at=event.recorded_at)


def append_bridged_decision_event(
    event: EventEnvelope,
    *,
    db_root: Path,
) -> ProgramFactWriteResult:
    if not event.event_type.startswith("decision."):
        raise ValueError(f"Unsupported decision bridge event type: {event.event_type}")

    store = ProgramFactStore(event.program_id, db_root=db_root)
    current_fact = _load_current_decision_fact(store, event)
    fact_input = build_bridge_decision_fact_input(event, current_fact=current_fact)
    return store.append_fact(fact_input, recorded_at=event.recorded_at)


def append_bridged_assumption_event(
    event: EventEnvelope,
    *,
    db_root: Path,
) -> ProgramFactWriteResult:
    if not event.event_type.startswith("assumption."):
        raise ValueError(f"Unsupported assumption bridge event type: {event.event_type}")

    store = ProgramFactStore(event.program_id, db_root=db_root)
    current_fact = _load_current_assumption_fact(store, event)
    fact_input = build_bridge_assumption_fact_input(event, current_fact=current_fact)
    return store.append_fact(fact_input, recorded_at=event.recorded_at)


def append_bridged_milestone_event(
    event: EventEnvelope,
    *,
    db_root: Path,
) -> ProgramFactWriteResult:
    if not event.event_type.startswith("milestone."):
        raise ValueError(f"Unsupported milestone bridge event type: {event.event_type}")

    store = ProgramFactStore(event.program_id, db_root=db_root)
    current_fact = _load_current_milestone_fact(store, event)
    fact_input = build_bridge_milestone_fact_input(event, current_fact=current_fact)
    return store.append_fact(fact_input, recorded_at=event.recorded_at)


def append_bridged_dependency_event(
    event: EventEnvelope,
    *,
    db_root: Path,
) -> ProgramFactWriteResult:
    if not event.event_type.startswith("dependency."):
        raise ValueError(f"Unsupported dependency bridge event type: {event.event_type}")

    store = ProgramFactStore(event.program_id, db_root=db_root)
    current_fact = _load_current_dependency_fact(store, event)
    fact_input = build_bridge_dependency_fact_input(event, current_fact=current_fact)
    return store.append_fact(fact_input, recorded_at=event.recorded_at)


def append_bridged_workstream_event(
    event: EventEnvelope,
    *,
    db_root: Path,
) -> ProgramFactWriteResult:
    if not event.event_type.startswith("workstream."):
        raise ValueError(f"Unsupported workstream bridge event type: {event.event_type}")

    store = ProgramFactStore(event.program_id, db_root=db_root)
    current_fact = _load_current_workstream_fact(store, event)
    fact_input = build_bridge_workstream_fact_input(event, current_fact=current_fact)
    return store.append_fact(fact_input, recorded_at=event.recorded_at)


def append_bridged_commitment_event(
    event: EventEnvelope,
    *,
    db_root: Path,
    programs_root: Path,
) -> ProgramFactWriteResult:
    if not event.event_type.startswith("commitment."):
        raise ValueError(f"Unsupported commitment bridge event type: {event.event_type}")

    store = ProgramFactStore(event.program_id, db_root=db_root)
    current_fact = _load_current_commitment_fact(store, event)
    registry = EntityRegistry.load(event.program_id, programs_root=programs_root)
    fact_input = build_bridge_commitment_fact_input(event, registry=registry, current_fact=current_fact)
    return store.append_fact(fact_input, recorded_at=event.recorded_at)


def sync_bridged_risk_corroboration(
    event: EventEnvelope,
    *,
    db_root: Path,
    programs_root: Path,
) -> ProgramFactWriteResult | None:
    if not event.event_type.startswith("risk."):
        return None
    if event.confidence != ConfidenceTier.AI_EXTRACTED or event.dedupe_core_hash is None:
        return None

    risk_id = _required_str(event.payload, "risk_id")
    entity_id = f"RISK:{risk_id}"
    policy = load_source_authority_policy()
    family = get_authority_family("risk.entry", policy)
    source_document_keys = _corroborating_source_document_keys(event, programs_root=programs_root)
    store = ProgramFactStore(event.program_id, db_root=db_root)
    current_fact = _load_current_corroboration_fact(store, entity_id=entity_id, family=family, as_of=event.recorded_at)

    if len(source_document_keys) < 2 and current_fact is None:
        return None

    corroboration_fact = ProgramFactInput(
        fact_type="fact.corroboration",
        scope="program",
        entity_refs=(entity_id, f"FAMILY:{family}"),
        payload={
            "entity_id": entity_id,
            "family": family,
            "corroboration_count": len(source_document_keys),
            "source_document_keys": list(source_document_keys),
        },
        confidence=event.confidence.value,
        precedence=FactPrecedence.VERIFIED_SYSTEM_SIGNAL,
        review_state=FactReviewState.ACCEPTED,
        lifecycle_state=(FactLifecycleState.ACTIVE if len(source_document_keys) >= 2 else FactLifecycleState.CLOSED),
        created_by="ledger_bridge",
        write_authority="bridge",
    )
    return store.append_fact(corroboration_fact, recorded_at=event.recorded_at)


def _load_current_risk_fact(store: ProgramFactStore, event: EventEnvelope) -> ProgramFactRevision | None:
    if event.event_type == "risk.raised.v1":
        return None
    risk_id = _required_str(event.payload, "risk_id")
    natural_key = build_natural_key("risk.entry", entity_refs=(f"RISK:{risk_id}",), scope="program")
    snapshot = store.snapshot(as_of=event.recorded_at)
    for fact in snapshot.facts:
        if fact.fact_type == "risk.entry" and fact.natural_key == natural_key:
            return fact
    return None


def _load_current_decision_fact(store: ProgramFactStore, event: EventEnvelope) -> ProgramFactRevision | None:
    if event.event_type == "decision.made.v1":
        return None
    decision_id = _required_str(event.payload, "decision_id")
    natural_key = build_natural_key("decision.entry", entity_refs=(f"DECISION:{decision_id}",), scope="program")
    snapshot = store.snapshot(as_of=event.recorded_at)
    for fact in snapshot.facts:
        if fact.fact_type == "decision.entry" and fact.natural_key == natural_key:
            return fact
    return None


def _load_current_assumption_fact(store: ProgramFactStore, event: EventEnvelope) -> ProgramFactRevision | None:
    if event.event_type == "assumption.stated.v1":
        return None
    assumption_id = _required_str(event.payload, "assumption_id")
    natural_key = build_natural_key("assumption.entry", entity_refs=(f"ASSUMPTION:{assumption_id}",), scope="program")
    snapshot = store.snapshot(as_of=event.recorded_at)
    for fact in snapshot.facts:
        if fact.fact_type == "assumption.entry" and fact.natural_key == natural_key:
            return fact
    return None


def _load_current_milestone_fact(store: ProgramFactStore, event: EventEnvelope) -> ProgramFactRevision | None:
    if event.event_type == "milestone.created.v1":
        return None
    milestone_id = _required_str(event.payload, "milestone_id")
    natural_key = build_natural_key("milestone.entry", entity_refs=(f"MILESTONE:{milestone_id}",), scope="program")
    snapshot = store.snapshot(as_of=event.recorded_at)
    for fact in snapshot.facts:
        if fact.fact_type == "milestone.entry" and fact.natural_key == natural_key:
            return fact
    return None


def _load_current_dependency_fact(store: ProgramFactStore, event: EventEnvelope) -> ProgramFactRevision | None:
    if event.event_type == "dependency.declared.v1":
        return None
    dependency_id = _required_str(event.payload, "dependency_id")
    natural_key = build_natural_key("dependency.link", entity_refs=(f"DEPENDENCY:{dependency_id}",), scope="program")
    snapshot = store.snapshot(as_of=event.recorded_at)
    for fact in snapshot.facts:
        if fact.fact_type == "dependency.link" and fact.natural_key == natural_key:
            return fact
    return None


def _load_current_workstream_fact(store: ProgramFactStore, event: EventEnvelope) -> ProgramFactRevision | None:
    if event.event_type == "workstream.created.v1":
        return None
    workstream_id = _required_str(event.payload, "workstream_id")
    natural_key = build_natural_key("workstream.entry", entity_refs=(f"WS:{workstream_id}",), scope="program")
    snapshot = store.snapshot(as_of=event.recorded_at)
    for fact in snapshot.facts:
        if fact.fact_type == "workstream.entry" and fact.natural_key == natural_key:
            return fact
    return None


def _load_current_commitment_fact(store: ProgramFactStore, event: EventEnvelope) -> ProgramFactRevision | None:
    if event.event_type == "commitment.made.v1":
        return None
    commitment_id = _required_str(event.payload, "commitment_id")
    natural_key = build_commitment_natural_key(commitment_id)
    snapshot = store.snapshot(as_of=event.recorded_at)
    for fact in snapshot.facts:
        if fact.fact_type == "commitment.entry" and fact.natural_key == natural_key:
            return fact
    return None


def _load_current_corroboration_fact(
    store: ProgramFactStore,
    *,
    entity_id: str,
    family: str,
    as_of,
) -> ProgramFactRevision | None:
    natural_key = build_natural_key("fact.corroboration", entity_refs=(entity_id, f"FAMILY:{family}"), scope="program")
    snapshot = store.snapshot(as_of=as_of)
    for fact in snapshot.facts:
        if fact.fact_type == "fact.corroboration" and fact.natural_key == natural_key:
            return fact
    return None


def _corroborating_source_document_keys(event: EventEnvelope, *, programs_root: Path) -> tuple[str, ...]:
    effective_events = apply_supersession(read_events(event.program_id, programs_root=programs_root))
    matching_keys = {
        source_document_key(candidate.source_ref)
        for candidate in effective_events
        if candidate.confidence == ConfidenceTier.AI_EXTRACTED and candidate.dedupe_core_hash == event.dedupe_core_hash
    }
    return tuple(sorted(matching_keys))


def run_cross_source_conflict_detection(
    program_id: str,
    *,
    programs_root: Path,
    correlation_id: str = "",
) -> dict[str, int]:
    """AG-9 / §6.14.5: run cross-source conflict detection on the production path.

    Invokes ``detect_corroboration_and_conflicts`` over the current fact-store
    snapshot (the "last-known ProgramReality cache" per §6.14.4) so Vertex acts
    as a *reconciler*, not an email summarizer: when an EML-derived milestone
    observation materially contradicts the authoritative milestone state already
    in the store (different provenance classes — human_comms vs ado/PM-judgment),
    a ``fact.conflict`` is written with the counter-source ``as_of`` so the
    operator sees the disagreement at triage, never a silent parrot.

    Honest by design: when EML-derived and store-authored entity keys do not yet
    align, the detector groups them separately and correctly finds *no conflict*.
    It never fabricates a disagreement.

    W2-12: this — not ``src/core/rev/pipeline.py`` — is the sanctioned place for
    this fact-store write; REV modules must never import ``ProgramFactStore``/
    ``append_fact`` directly (see ``tests/contracts/test_rev_bridge_decoupling.py``).
    The REV pipeline calls this function instead of writing facts itself.

    Best-effort — never raises (mirrors ``_compute_family_divergence`` in the
    REV pipeline). Returns ``{"conflicts": n, "corroborations": n, "observations": n}``.
    """
    import logging
    from datetime import datetime, timezone

    log = logging.getLogger(__name__)
    try:
        from src.core.truth_model import build_truth_context, detect_corroboration_and_conflicts

        now = datetime.now(timezone.utc)
        store = ProgramFactStore(program_id, home_root=None, db_root=programs_root.parent)
        snapshot = store.snapshot(as_of=now)
        observations = list(snapshot.facts)
        if not observations:
            return {"conflicts": 0, "corroborations": 0, "observations": 0}

        authority = load_source_authority_policy()
        ctx = build_truth_context(program_id, fact_snapshot=snapshot)
        result = detect_corroboration_and_conflicts(
            observations,
            authority,
            trust_ledger=None,
            registry=None,
            ctx=ctx,
            now=now,
            program_fact_store=store,
        )

        for conflict in result.conflicts:
            _append_conflict_fact(store, conflict, now)
        for corroboration in result.corroborations:
            _append_corroboration_fact(store, corroboration, now)
        return {
            "conflicts": len(result.conflicts),
            "corroborations": len(result.corroborations),
            "observations": len(observations),
        }
    except Exception:  # noqa: BLE001 — conflict check must never break the cycle
        log.warning(
            "cross-source conflict check failed for %s (correlation_id=%s)",
            program_id, correlation_id, exc_info=True,
        )
        return {"conflicts": 0, "corroborations": 0, "observations": 0, "error": True}


def _append_conflict_fact(store: ProgramFactStore, conflict: dict, now: Any) -> None:
    """Write one ``fact.conflict`` (reshaped to satisfy fact_schema_registry).

    The detector emits ``target_natural_key/observed_value/material/...``; the
    registered schema requires ``day_bucket/conflicting_signal_ids/
    conflict_description``. We reshape + carry the §6.14.5 ``counter_source_as_of``
    so the disputed flag is staleness-judgeable. Best-effort — a single failed
    append is logged, never raised.
    """
    import logging
    log = logging.getLogger(__name__)
    try:
        entity_id = str(conflict.get("entity_id", ""))
        family = str(conflict.get("family", ""))
        if not entity_id or not family:
            return
        entity_refs = (entity_id, f"FAMILY:{family}")
        payload = {
            **conflict,
            # Schema-required fields (fact_schema_registry).
            "day_bucket": now.date().isoformat(),
            "conflicting_signal_ids": [
                str(conflict.get("losing_source", "")),
                str(conflict.get("winning_source", "")),
            ],
            "conflict_description": (
                f"{family}: {conflict.get('expected_value','?')} → "
                f"{conflict.get('observed_value','?')}"
            ),
            "severity": "material" if conflict.get("material") else "minor",
            # §6.14.5: carry the counter-source staleness timestamp so the
            # operator can judge whether the dispute is a stale-source artifact.
            "counter_source_as_of": now.isoformat(),
        }
        fact = ProgramFactInput(
            fact_type="fact.conflict",
            entity_refs=entity_refs,
            payload=payload,
            scope="program",
            precedence=FactPrecedence.VERIFIED_SYSTEM_SIGNAL,
            review_state=FactReviewState.ACCEPTED,
            lifecycle_state=FactLifecycleState.ACTIVE,
            created_by="vertex.rev_conflict_check",
        )
        store.append_fact(fact, recorded_at=now)
    except Exception:  # noqa: BLE001
        log.debug("conflict fact append skipped", exc_info=True)


def _append_corroboration_fact(store: ProgramFactStore, corroboration: dict, now: Any) -> None:
    """Write one ``fact.corroboration`` (reshaped to satisfy fact_schema_registry)."""
    import logging
    log = logging.getLogger(__name__)
    try:
        entity_id = str(corroboration.get("entity_id", ""))
        family = str(corroboration.get("family", ""))
        if not entity_id or not family:
            return
        payload = {
            **corroboration,
            "day_bucket": now.date().isoformat(),
            "corroborating_signal_ids": [
                str(corroboration.get("source_a", "")),
                str(corroboration.get("source_b", "")),
            ],
            "corroboration_count": 2,
        }
        fact = ProgramFactInput(
            fact_type="fact.corroboration",
            entity_refs=(entity_id, f"FAMILY:{family}"),
            payload=payload,
            scope="program",
            precedence=FactPrecedence.VERIFIED_SYSTEM_SIGNAL,
            review_state=FactReviewState.ACCEPTED,
            lifecycle_state=FactLifecycleState.ACTIVE,
            created_by="vertex.rev_conflict_check",
        )
        store.append_fact(fact, recorded_at=now)
    except Exception:  # noqa: BLE001
        log.debug("corroboration fact append skipped", exc_info=True)