from __future__ import annotations

from datetime import date, timedelta

from src.core.forecast_engine import ForecastMethod, forecast_etas
from src.core.models import Confidence, RiskLevel
from src.core.models_v2 import TrajectoryPoint


def test_forecast_etas_keeps_zero_slip_items_high_confidence() -> None:
    forecasts = forecast_etas(
        {
            101: (
                _point(date(2026, 4, 1), date(2026, 5, 20)),
                _point(date(2026, 4, 20), date(2026, 5, 20)),
            )
        },
        (),
        as_of=date(2026, 5, 1),
    )

    assert forecasts[101].confidence == Confidence.HIGH
    assert forecasts[101].prior_slips == 0
    assert forecasts[101].forecast_method == ForecastMethod.HEURISTIC
    assert forecasts[101].p50_date == date(2026, 5, 20)
    assert forecasts[101].p80_date == date(2026, 5, 27)
    assert forecasts[101].p95_date == date(2026, 6, 3)


def test_forecast_etas_marks_two_slips_low_confidence() -> None:
    forecasts = forecast_etas(
        {
            202: (
                _point(date(2026, 3, 20), date(2026, 5, 1)),
                _point(date(2026, 4, 1), date(2026, 5, 8)),
                _point(date(2026, 4, 15), date(2026, 5, 15)),
            )
        },
        (),
        as_of=date(2026, 5, 1),
    )

    assert forecasts[202].confidence == Confidence.LOW
    assert forecasts[202].prior_slips == 2
    assert forecasts[202].slip_probability >= 0.4


def test_forecast_etas_applies_calibration_adjustment_when_present() -> None:
    baseline = forecast_etas(
        {
            212: (
                _point(date(2026, 3, 20), date(2026, 5, 1)),
                _point(date(2026, 4, 1), date(2026, 5, 8)),
                _point(date(2026, 4, 15), date(2026, 5, 15)),
            )
        },
        (),
        as_of=date(2026, 5, 1),
    )

    calibrated = forecast_etas(
        {
            212: (
                _point(date(2026, 3, 20), date(2026, 5, 1)),
                _point(date(2026, 4, 1), date(2026, 5, 8)),
                _point(date(2026, 4, 15), date(2026, 5, 15)),
            )
        },
        (),
        calibration_adjustments={212: 0.15},
        as_of=date(2026, 5, 1),
    )

    assert calibrated[212].slip_probability == baseline[212].slip_probability + 0.15
    assert "calibrated +0.15" in calibrated[212].reasoning


def test_forecast_etas_caps_three_slips_at_high_probability() -> None:
    forecasts = forecast_etas(
        {
            303: (
                _point(date(2026, 3, 10), date(2026, 5, 1)),
                _point(date(2026, 3, 20), date(2026, 5, 8)),
                _point(date(2026, 4, 1), date(2026, 5, 15)),
                _point(date(2026, 4, 20), date(2026, 5, 22)),
            )
        },
        (),
        as_of=date(2026, 5, 1),
    )

    assert forecasts[303].confidence == Confidence.LOW
    assert forecasts[303].prior_slips == 3
    assert forecasts[303].slip_probability >= 0.8


def test_forecast_etas_uses_trajectory_history_percentiles_for_three_points() -> None:
    forecasts = forecast_etas(
        {
            404: (
                _point(date(2026, 3, 10), date(2026, 5, 1)),
                _point(date(2026, 3, 24), date(2026, 5, 6)),
                _point(date(2026, 4, 7), date(2026, 5, 10)),
            )
        },
        (),
        as_of=date(2026, 4, 10),
    )

    forecast = forecasts[404]

    assert forecast.forecast_method == ForecastMethod.TRAJECTORY_HISTORY
    assert forecast.p50_date is not None
    assert forecast.p80_date is not None
    assert forecast.p95_date is not None
    assert forecast.p50_date <= forecast.p80_date <= forecast.p95_date
    assert forecast.p50_date > forecast.ado_target_date


def test_forecast_etas_uses_deterministic_monte_carlo_percentiles_for_dense_history() -> None:
    first_run = forecast_etas(
        {
            505: (
                _point(date(2026, 3, 1), date(2026, 5, 1)),
                _point(date(2026, 3, 15), date(2026, 5, 5)),
                _point(date(2026, 3, 29), date(2026, 5, 10)),
                _point(date(2026, 4, 12), date(2026, 5, 18)),
            )
        },
        (),
        as_of=date(2026, 4, 15),
    )
    second_run = forecast_etas(
        {
            505: (
                _point(date(2026, 3, 1), date(2026, 5, 1)),
                _point(date(2026, 3, 15), date(2026, 5, 5)),
                _point(date(2026, 3, 29), date(2026, 5, 10)),
                _point(date(2026, 4, 12), date(2026, 5, 18)),
            )
        },
        (),
        as_of=date(2026, 4, 15),
    )

    forecast = first_run[505]

    assert forecast.forecast_method == ForecastMethod.MONTE_CARLO
    assert forecast.p50_date is not None
    assert forecast.p80_date is not None
    assert forecast.p95_date is not None
    assert forecast.p50_date <= forecast.p80_date <= forecast.p95_date
    assert forecast.p80_date >= forecast.predicted_target_date
    assert forecast.p50_date == second_run[505].p50_date
    assert forecast.p80_date == second_run[505].p80_date
    assert forecast.p95_date == second_run[505].p95_date


def test_forecast_etas_preserves_monte_carlo_percentile_invariants_across_dense_histories() -> None:
    scenarios = (
        (601, (0, 0, 0)),
        (602, (1, 2, 3)),
        (603, (0, 3, 0)),
        (604, (4, 1, 6)),
        (605, (7, 0, 7)),
    )

    for work_item_id, target_shifts in scenarios:
        points = _dense_history_points(
            start_date=date(2026, 3, 1),
            base_target_date=date(2026, 5, 1),
            target_shifts=target_shifts,
        )

        first_run = forecast_etas({work_item_id: points}, (), as_of=date(2026, 4, 15))
        second_run = forecast_etas({work_item_id: points}, (), as_of=date(2026, 4, 15))
        forecast = first_run[work_item_id]

        assert forecast.forecast_method == ForecastMethod.MONTE_CARLO
        assert forecast.ado_target_date is not None
        assert forecast.predicted_target_date is not None
        assert forecast.p50_date is not None
        assert forecast.p80_date is not None
        assert forecast.p95_date is not None
        assert forecast.ado_target_date <= forecast.p50_date <= forecast.p80_date <= forecast.p95_date
        assert forecast.p80_date >= forecast.predicted_target_date
        assert forecast.p50_date == second_run[work_item_id].p50_date
        assert forecast.p80_date == second_run[work_item_id].p80_date
        assert forecast.p95_date == second_run[work_item_id].p95_date


def test_forecast_etas_preserves_monte_carlo_percentile_invariants_across_generated_dense_histories() -> None:
    work_item_id = 700
    for first_shift in (-3, 0, 4):
        for second_shift in (-3, 0, 4):
            for third_shift in (-3, 0, 4):
                scenario = (first_shift, second_shift, third_shift)
                points = _dense_history_points(
                    start_date=date(2026, 3, 1),
                    base_target_date=date(2026, 5, 1),
                    target_shifts=scenario,
                )

                first_run = forecast_etas({work_item_id: points}, (), as_of=date(2026, 4, 15))
                second_run = forecast_etas({work_item_id: points}, (), as_of=date(2026, 4, 15))
                forecast = first_run[work_item_id]

                assert forecast.forecast_method == ForecastMethod.MONTE_CARLO, scenario
                assert forecast.ado_target_date is not None, scenario
                assert forecast.predicted_target_date is not None, scenario
                assert forecast.p50_date is not None, scenario
                assert forecast.p80_date is not None, scenario
                assert forecast.p95_date is not None, scenario
                assert forecast.ado_target_date <= forecast.p50_date <= forecast.p80_date <= forecast.p95_date, scenario
                assert forecast.p80_date >= forecast.predicted_target_date, scenario
                assert forecast.p50_date == second_run[work_item_id].p50_date, scenario
                assert forecast.p80_date == second_run[work_item_id].p80_date, scenario
                assert forecast.p95_date == second_run[work_item_id].p95_date, scenario

                work_item_id += 1


def test_forecast_etas_keeps_dense_histories_without_positive_shifts_at_current_target() -> None:
    forecasts = forecast_etas(
        {
            606: _dense_history_points(
                start_date=date(2026, 3, 1),
                base_target_date=date(2026, 5, 1),
                target_shifts=(0, 0, 0),
            )
        },
        (),
        as_of=date(2026, 4, 15),
    )

    forecast = forecasts[606]

    assert forecast.forecast_method == ForecastMethod.MONTE_CARLO
    assert forecast.ado_target_date is not None
    assert forecast.p50_date == forecast.ado_target_date
    assert forecast.p80_date == forecast.ado_target_date
    assert forecast.p95_date == forecast.ado_target_date


def _point(point_date: date, target_date: date) -> TrajectoryPoint:
    return TrajectoryPoint(
        date=point_date,
        state="Active",
        assigned_to="owner@example.com",
        target_date=target_date,
        risk_level=RiskLevel.LOW,
        area_path="One\\Adventure\\Acme",
        tags=("acme",),
    )


def _dense_history_points(
    *,
    start_date: date,
    base_target_date: date,
    target_shifts: tuple[int, int, int],
) -> tuple[TrajectoryPoint, ...]:
    points: list[TrajectoryPoint] = []
    current_target_date = base_target_date
    for offset_weeks, shift_days in enumerate((0, *target_shifts)):
        current_target_date += timedelta(days=shift_days)
        points.append(_point(start_date + timedelta(days=offset_weeks * 14), current_target_date))
    return tuple(points)