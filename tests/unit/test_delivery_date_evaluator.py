from __future__ import annotations

from datetime import date, datetime, timezone

from src.core.delivery_date_evaluator import DeliveryDateSnapshot, evaluate_delivery_date_hypothesis
from src.core.hypothesis_models import Hypothesis, HypothesisKind


def test_evaluate_delivery_date_hypothesis_marks_overdue_open_item_alert() -> None:
    hypothesis = Hypothesis(
        id="hyp-dd-001",
        short_id="H-101",
        program_id="acme",
        kind=HypothesisKind.DELIVERY_DATE,
        statement="Pilot completes by June 1.",
        expected_value="2026-06-01",
        as_of_date=date(2026, 5, 20),
        telemetry_assertion_id=None,
        linked_ado_item_id=12345,
        proposed_by="pm",
        proposed_at=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
    )

    result = evaluate_delivery_date_hypothesis(
        hypothesis,
        DeliveryDateSnapshot(work_item_id=12345, state="Active", target_date=date(2026, 6, 1)),
        datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc),
    )

    assert result.violated is True
    assert result.days_past_due == 9
    assert result.severity is not None
    assert result.severity.value == "alert"


def test_evaluate_delivery_date_hypothesis_marks_closed_item_as_non_violating() -> None:
    hypothesis = Hypothesis(
        id="hyp-dd-001",
        short_id="H-101",
        program_id="acme",
        kind=HypothesisKind.DELIVERY_DATE,
        statement="Pilot completes by June 1.",
        expected_value="2026-06-01",
        as_of_date=date(2026, 5, 20),
        telemetry_assertion_id=None,
        linked_ado_item_id=12345,
        proposed_by="pm",
        proposed_at=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
    )

    result = evaluate_delivery_date_hypothesis(
        hypothesis,
        DeliveryDateSnapshot(
            work_item_id=12345,
            state="Closed",
            target_date=date(2026, 6, 1),
            closed_at=datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc),
        ),
        datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc),
    )

    assert result.violated is False
    assert result.item_closed is True
    assert result.closed_late is True
    assert result.days_past_due == 2