"""Unit tests for ADF-W4.6: qualitative slip bands + calibrated rendering."""

from __future__ import annotations

from datetime import date

import pytest

from src.core.forecast_bands import (
    SlipBand,
    SlipBandAssessment,
    assess_forecasts,
    assess_slip_band,
    classify_slip_band,
)
from src.core.forecast_engine import ETAForecast, ForecastMethod
from src.core.models import Confidence


def _forecast(
    *,
    slip_probability: float = 0.5,
    prior_slips: int = 0,
    confidence: Confidence = Confidence.MEDIUM,
    method: ForecastMethod = ForecastMethod.HEURISTIC,
    p50: date | None = None,
    p95: date | None = None,
) -> ETAForecast:
    return ETAForecast(
        work_item_id=123,
        ado_target_date=date(2026, 8, 1),
        predicted_target_date=date(2026, 8, 10),
        confidence=confidence,
        slip_probability=slip_probability,
        reasoning="test",
        prior_slips=prior_slips,
        p50_date=p50,
        p80_date=None,
        p95_date=p95,
        forecast_method=method,
    )


def test_classify_slip_band_thresholds() -> None:
    assert classify_slip_band(0.1) is SlipBand.LOW
    assert classify_slip_band(0.32) is SlipBand.LOW
    assert classify_slip_band(0.33) is SlipBand.ELEVATED
    assert classify_slip_band(0.5) is SlipBand.ELEVATED
    assert classify_slip_band(0.66) is SlipBand.ELEVATED
    assert classify_slip_band(0.67) is SlipBand.HIGH
    assert classify_slip_band(0.9) is SlipBand.HIGH


def test_assess_uncalibrated_hides_precise_probability() -> None:
    forecast = _forecast(slip_probability=0.7, prior_slips=3)
    assessment = assess_slip_band(forecast, calibrated=False)
    assert assessment.band is SlipBand.HIGH
    assert assessment.calibrated is False
    # The raw value is retained for diagnostics but the render omits it.
    assert assessment.slip_probability == 0.7
    assert "miss probability" not in assessment.render_summary
    assert "High slip risk" in assessment.render_summary


def test_assess_calibrated_shows_precise_probability_and_ci() -> None:
    forecast = _forecast(
        slip_probability=0.7,
        prior_slips=3,
        p50=date(2026, 8, 5),
        p95=date(2026, 8, 20),
        method=ForecastMethod.MONTE_CARLO,
    )
    assessment = assess_slip_band(forecast, calibrated=True)
    assert assessment.calibrated is True
    summary = assessment.render_summary
    assert "miss probability" in summary
    assert "calibrated" in summary
    assert "p50" in summary and "p95" in summary


def test_drivers_named_from_forecast_signals() -> None:
    forecast = _forecast(slip_probability=0.5, prior_slips=2, confidence=Confidence.LOW)
    assessment = assess_slip_band(forecast, calibrated=False)
    drivers = assessment.drivers
    assert "2 prior slips" in drivers
    assert "low-confidence trajectory" in drivers


def test_heuristic_driver_called_out() -> None:
    forecast = _forecast(method=ForecastMethod.HEURISTIC)
    assessment = assess_slip_band(forecast, calibrated=False)
    assert any("heuristic" in d for d in assessment.drivers)


def test_trajectory_only_calibration_quality() -> None:
    forecast = _forecast(method=ForecastMethod.TRAJECTORY_HISTORY)
    assessment = assess_slip_band(forecast, calibrated=False)
    assert assessment.calibration_quality == "trajectory_only"


def test_calibrated_quality_label() -> None:
    forecast = _forecast(method=ForecastMethod.MONTE_CARLO)
    assessment = assess_slip_band(forecast, calibrated=True)
    assert assessment.calibration_quality == "calibrated"


def test_assess_forecasts_batch_respects_calibrated_set() -> None:
    forecasts = {
        1: _forecast(slip_probability=0.2),
        2: _forecast(slip_probability=0.8),
    }
    assessments = assess_forecasts(forecasts, calibrated_ids=frozenset({2}))
    assert assessments[1].calibrated is False
    assert assessments[2].calibrated is True
    assert assessments[1].band is SlipBand.LOW
    assert assessments[2].band is SlipBand.HIGH


def test_uncalibrated_never_publishes_precise_probability_in_render() -> None:
    """INV: 'Do not publish an uncalibrated precise probability' (Section 8.10.3)."""
    for prob in (0.1, 0.3, 0.5, 0.7, 0.9):
        forecast = _forecast(slip_probability=prob)
        assessment = assess_slip_band(forecast, calibrated=False)
        assert "%" not in assessment.render_summary, (
            f"uncalibrated render leaked precise probability at {prob}: {assessment.render_summary}"
        )


def test_evidence_window_carried_through() -> None:
    forecast = _forecast()
    assessment = assess_slip_band(forecast, calibrated=False, evidence_window_days=90)
    assert assessment.evidence_window_days == 90
