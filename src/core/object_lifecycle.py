"""Observe-only closed-loop lifecycle state machine for governed TPM objects (RELO-W4.9, relo §8.4.12).

Pure and deterministic: no I/O, never mutates inputs, and is not wired into any
existing staleness or escalation engine. Every transition must carry evidence
references; illegal transitions return a rejection finding instead of raising.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum


class LifecycleObjectType(str, Enum):
    RISK = "risk"
    DEPENDENCY = "dependency"
    DECISION = "decision"
    MEETING_ACTION = "meeting_action"
    CLAIM_COMMITMENT = "claim_commitment"
    STAKEHOLDER_INTERVENTION = "stakeholder_intervention"


class LifecycleState(str, Enum):
    OPEN = "open"
    BREACHED = "breached"
    ESCALATED = "escalated"
    RESOLVED = "resolved"
    OUTCOME_MEASURED = "outcome_measured"


ALLOWED_TRANSITIONS: dict[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.OPEN: frozenset({LifecycleState.BREACHED, LifecycleState.RESOLVED}),
    LifecycleState.BREACHED: frozenset({LifecycleState.ESCALATED, LifecycleState.RESOLVED}),
    LifecycleState.ESCALATED: frozenset({LifecycleState.RESOLVED}),
    LifecycleState.RESOLVED: frozenset({LifecycleState.OUTCOME_MEASURED}),
    LifecycleState.OUTCOME_MEASURED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    object_type: LifecycleObjectType
    object_id: str
    from_state: LifecycleState | None
    to_state: LifecycleState
    occurred_at: datetime
    evidence_refs: tuple[str, ...]
    reason: str | None = None
    mitigation_effectiveness: str | None = None
    residual_risk: str | None = None


@dataclass(frozen=True, slots=True)
class LifecycleRecord:
    object_type: LifecycleObjectType
    object_id: str
    state: LifecycleState
    events: tuple[LifecycleEvent, ...]
    mitigation_effectiveness: str | None = None
    residual_risk: str | None = None


@dataclass(frozen=True, slots=True)
class LifecycleFinding:
    code: str
    object_type: LifecycleObjectType
    object_id: str
    from_state: LifecycleState
    requested_state: LifecycleState


@dataclass(frozen=True, slots=True)
class TransitionResult:
    record: LifecycleRecord | None
    finding: LifecycleFinding | None


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("lifecycle timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def open_record(
    object_type: LifecycleObjectType,
    object_id: str,
    evidence_refs: tuple[str, ...],
    *,
    opened_at: datetime | None = None,
) -> LifecycleRecord:
    if not evidence_refs:
        raise ValueError("a lifecycle record must open with evidence")
    event = LifecycleEvent(
        object_type, object_id, None, LifecycleState.OPEN, _utc(opened_at), tuple(evidence_refs)
    )
    return LifecycleRecord(object_type, object_id, LifecycleState.OPEN, (event,))


def transition(
    record: LifecycleRecord,
    to_state: LifecycleState,
    evidence_refs: tuple[str, ...],
    *,
    occurred_at: datetime | None = None,
    reason: str | None = None,
    mitigation_effectiveness: str | None = None,
    residual_risk: str | None = None,
) -> TransitionResult:
    when = _utc(occurred_at)

    def reject(code: str) -> TransitionResult:
        return TransitionResult(
            None, LifecycleFinding(code, record.object_type, record.object_id, record.state, to_state)
        )

    if to_state not in ALLOWED_TRANSITIONS[record.state]:
        return reject("illegal_transition")
    if not evidence_refs:
        return reject("missing_evidence")
    has_outcome = mitigation_effectiveness is not None or residual_risk is not None
    if has_outcome and to_state is not LifecycleState.OUTCOME_MEASURED:
        return reject("outcome_fields_not_allowed")
    if when < record.events[-1].occurred_at:
        return reject("non_monotonic_time")

    event = LifecycleEvent(
        record.object_type,
        record.object_id,
        record.state,
        to_state,
        when,
        tuple(evidence_refs),
        reason,
        mitigation_effectiveness,
        residual_risk,
    )
    updated = replace(record, state=to_state, events=record.events + (event,))
    if to_state is LifecycleState.OUTCOME_MEASURED:
        updated = replace(
            updated, mitigation_effectiveness=mitigation_effectiveness, residual_risk=residual_risk
        )
    return TransitionResult(updated, None)
