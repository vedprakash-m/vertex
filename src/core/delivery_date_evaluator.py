from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from src.core.hypothesis_models import ChallengeSeverity, Hypothesis
from src.core.work_item_states import TERMINAL_WORK_ITEM_STATES


_CLOSED_WORK_ITEM_STATES = TERMINAL_WORK_ITEM_STATES


@dataclass(frozen=True, slots=True)
class DeliveryDateSnapshot:
    work_item_id: int
    state: str
    target_date: date | None = None
    closed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DeliveryDateEvaluationResult:
    violated: bool
    item_closed: bool
    closed_late: bool
    days_past_due: int
    delta_magnitude: float | None
    severity: ChallengeSeverity | None
    note: str
    ado_current_target: str | None


def evaluate_delivery_date_hypothesis(
    hypothesis: Hypothesis,
    snapshot: DeliveryDateSnapshot,
    as_of: datetime,
) -> DeliveryDateEvaluationResult:
    expected_date = _parse_expected_date(hypothesis.expected_value)
    ado_current_target = snapshot.target_date.isoformat() if snapshot.target_date is not None else None
    if expected_date is None:
        return DeliveryDateEvaluationResult(
            violated=False,
            item_closed=False,
            closed_late=False,
            days_past_due=0,
            delta_magnitude=None,
            severity=None,
            note="delivery_date_missing_expected_value",
            ado_current_target=ado_current_target,
        )

    item_closed = snapshot.state.strip().lower() in _CLOSED_WORK_ITEM_STATES
    comparison_date = snapshot.closed_at.date() if snapshot.closed_at is not None else as_of.date()
    days_past_due = max(0, (comparison_date - expected_date).days)
    if item_closed:
        closed_late = days_past_due > 0
        note = (
            f"ADO item {snapshot.work_item_id} closed {days_past_due} day(s) after target date"
            if closed_late
            else f"ADO item {snapshot.work_item_id} closed on or before target date"
        )
        return DeliveryDateEvaluationResult(
            violated=False,
            item_closed=True,
            closed_late=closed_late,
            days_past_due=days_past_due,
            delta_magnitude=None,
            severity=None,
            note=note,
            ado_current_target=ado_current_target,
        )

    if days_past_due <= 0:
        return DeliveryDateEvaluationResult(
            violated=False,
            item_closed=False,
            closed_late=False,
            days_past_due=0,
            delta_magnitude=0.0,
            severity=None,
            note=f"ADO item {snapshot.work_item_id} is not past target date",
            ado_current_target=ado_current_target,
        )

    timeline_start = hypothesis.proposed_at.date() if hypothesis.proposed_at is not None else hypothesis.as_of_date
    timeline_days = max((expected_date - timeline_start).days, 1)
    delta_magnitude = days_past_due / float(timeline_days)
    severity = _derive_delivery_date_severity(delta_magnitude)
    return DeliveryDateEvaluationResult(
        violated=True,
        item_closed=False,
        closed_late=False,
        days_past_due=days_past_due,
        delta_magnitude=delta_magnitude,
        severity=severity,
        note=f"ADO item {snapshot.work_item_id} is {days_past_due} day(s) past target date",
        ado_current_target=ado_current_target,
    )


def _derive_delivery_date_severity(delta_magnitude: float) -> ChallengeSeverity:
    if delta_magnitude > 0.2:
        return ChallengeSeverity.ALERT
    if delta_magnitude >= 0.05:
        return ChallengeSeverity.WARN
    return ChallengeSeverity.INFO


def _parse_expected_date(value: float | str | None) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None