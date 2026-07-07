from __future__ import annotations

from datetime import date, datetime, timezone

from src.core.date_inference import DateInference, infer_target_date
from src.core.models import Confidence


def test_infer_date_prefers_explicit_target_date() -> None:
    result = DateInference.infer_date(
        target_date=datetime(2026, 5, 6, 10, 30, tzinfo=timezone.utc),
        iteration_path="One\\FY26\\Q4",
        current_semester="H2 FY26",
    )

    assert result.inferred_date == date(2026, 5, 6)
    assert result.is_inferred is False
    assert result.confidence == Confidence.HIGH
    assert result.source is None


def test_infer_date_from_iteration_quarter() -> None:
    result = DateInference.infer_date(
        target_date=None,
        iteration_path="One\\FY26\\Q3",
    )

    assert result.inferred_date == date(2026, 3, 31)
    assert result.is_inferred is True
    assert result.confidence == Confidence.MEDIUM
    assert result.source == "IterationPath: One\\FY26\\Q3"


def test_infer_date_from_iteration_month_handles_fiscal_year_boundaries() -> None:
    result = DateInference.infer_date(
        target_date=None,
        iteration_path="One\\FY26\\Q1\\August",
    )

    assert result.inferred_date == date(2025, 8, 31)
    assert result.confidence == Confidence.MEDIUM


def test_infer_date_from_semester_boundary() -> None:
    result = DateInference.infer_date(
        target_date=None,
        iteration_path=None,
        current_semester="H1 FY26",
    )

    assert result.inferred_date == date(2025, 12, 31)
    assert result.is_inferred is True
    assert result.confidence == Confidence.LOW
    assert result.source == "Semester boundary: H1 FY26"


def test_infer_target_date_returns_tuple_shape() -> None:
    inferred_date, is_inferred, source = infer_target_date(
        target_date=None,
        iteration_path="One\\FY26\\Q4",
        current_semester=None,
    )

    assert inferred_date == date(2026, 6, 30)
    assert is_inferred is True
    assert source == "IterationPath: One\\FY26\\Q4"
