from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from src.core.object_lifecycle import (
    LifecycleEvent,
    LifecycleObjectType,
    LifecycleRecord,
    LifecycleState,
    open_record,
    transition,
)

_T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)


def _open(object_type: LifecycleObjectType = LifecycleObjectType.RISK) -> LifecycleRecord:
    return open_record(object_type, "R-1", ("ado:101",), opened_at=_T0)


def _step(record: LifecycleRecord, state: LifecycleState, hours: int, **kwargs: object) -> LifecycleRecord:
    result = transition(record, state, ("ado:102",), occurred_at=_T0 + timedelta(hours=hours), **kwargs)
    assert result.finding is None
    assert result.record is not None
    return result.record


def test_object_types_match_closed_loop_table() -> None:
    assert {t.value for t in LifecycleObjectType} == {
        "risk",
        "dependency",
        "decision",
        "meeting_action",
        "claim_commitment",
        "stakeholder_intervention",
    }


def test_full_loop_records_ordered_evidence_linked_events() -> None:
    record = _open()
    record = _step(record, LifecycleState.BREACHED, 1)
    record = _step(record, LifecycleState.ESCALATED, 2)
    record = _step(record, LifecycleState.RESOLVED, 3)
    record = _step(
        record, LifecycleState.OUTCOME_MEASURED, 4, mitigation_effectiveness="effective", residual_risk="low"
    )

    assert record.state is LifecycleState.OUTCOME_MEASURED
    assert [(e.from_state, e.to_state) for e in record.events] == [
        (None, LifecycleState.OPEN),
        (LifecycleState.OPEN, LifecycleState.BREACHED),
        (LifecycleState.BREACHED, LifecycleState.ESCALATED),
        (LifecycleState.ESCALATED, LifecycleState.RESOLVED),
        (LifecycleState.RESOLVED, LifecycleState.OUTCOME_MEASURED),
    ]
    assert all(e.evidence_refs for e in record.events)
    assert record.mitigation_effectiveness == "effective"
    assert record.residual_risk == "low"
    assert record.events[-1].residual_risk == "low"


def test_open_can_resolve_without_breach() -> None:
    record = _step(_open(LifecycleObjectType.DEPENDENCY), LifecycleState.RESOLVED, 1)
    assert record.state is LifecycleState.RESOLVED


@pytest.mark.parametrize(
    ("path", "target"),
    [
        ((), LifecycleState.ESCALATED),
        ((), LifecycleState.OUTCOME_MEASURED),
        ((), LifecycleState.OPEN),
        ((LifecycleState.RESOLVED,), LifecycleState.BREACHED),
        ((LifecycleState.RESOLVED, LifecycleState.OUTCOME_MEASURED), LifecycleState.RESOLVED),
    ],
)
def test_illegal_transition_returns_rejection_finding(
    path: tuple[LifecycleState, ...], target: LifecycleState
) -> None:
    record = _open()
    for hours, state in enumerate(path, start=1):
        record = _step(record, state, hours)

    result = transition(record, target, ("ado:200",), occurred_at=_T0 + timedelta(hours=10))

    assert result.record is None
    assert result.finding is not None
    assert result.finding.code == "illegal_transition"
    assert result.finding.from_state is record.state
    assert result.finding.requested_state is target


def test_transition_without_evidence_is_rejected() -> None:
    result = transition(_open(), LifecycleState.BREACHED, (), occurred_at=_T0 + timedelta(hours=1))
    assert result.record is None
    assert result.finding is not None
    assert result.finding.code == "missing_evidence"


def test_open_record_requires_evidence() -> None:
    with pytest.raises(ValueError):
        open_record(LifecycleObjectType.DECISION, "D-1", (), opened_at=_T0)


def test_outcome_fields_rejected_outside_outcome_measurement() -> None:
    result = transition(
        _open(), LifecycleState.RESOLVED, ("ado:1",), occurred_at=_T0 + timedelta(hours=1), residual_risk="low"
    )
    assert result.record is None
    assert result.finding is not None
    assert result.finding.code == "outcome_fields_not_allowed"


def test_outcome_fields_are_optional() -> None:
    record = _step(_step(_open(), LifecycleState.RESOLVED, 1), LifecycleState.OUTCOME_MEASURED, 2)
    assert record.mitigation_effectiveness is None
    assert record.residual_risk is None


def test_time_regression_is_rejected() -> None:
    record = _step(_open(), LifecycleState.BREACHED, 2)
    result = transition(record, LifecycleState.ESCALATED, ("ado:3",), occurred_at=_T0 + timedelta(hours=1))
    assert result.record is None
    assert result.finding is not None
    assert result.finding.code == "non_monotonic_time"


def test_naive_timestamp_is_programming_error() -> None:
    with pytest.raises(ValueError):
        transition(_open(), LifecycleState.BREACHED, ("ado:1",), occurred_at=datetime(2026, 11, 2, 9, 0))


def test_default_timestamp_is_utc_now() -> None:
    before = datetime.now(timezone.utc)
    result = transition(open_record(LifecycleObjectType.CLAIM_COMMITMENT, "C-1", ("ado:1",)), LifecycleState.BREACHED, ("ado:2",))
    assert result.record is not None
    occurred = result.record.events[-1].occurred_at
    assert occurred.tzinfo is not None
    assert occurred >= before


def test_transition_does_not_mutate_input_and_records_are_frozen() -> None:
    record = _open()
    _step(record, LifecycleState.BREACHED, 1)
    assert record.state is LifecycleState.OPEN
    assert len(record.events) == 1
    with pytest.raises(FrozenInstanceError):
        record.state = LifecycleState.RESOLVED  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        record.events[0].evidence_refs = ()  # type: ignore[misc]
    assert isinstance(record.events[0], LifecycleEvent)
