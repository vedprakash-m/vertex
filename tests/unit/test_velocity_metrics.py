from __future__ import annotations

from datetime import date

from src.core.models import RiskLevel
from src.core.models_v2 import TrajectoryPoint
from src.core.velocity_metrics import build_velocity_metrics


def test_build_velocity_metrics_computes_cycle_time_and_throughput() -> None:
    metrics = build_velocity_metrics(
        {
            1: (
                TrajectoryPoint(date=date(2026, 4, 24), state="Active", assigned_to="owner", target_date=date(2026, 5, 10), risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme"),
                TrajectoryPoint(date=date(2026, 5, 8), state="Resolved", assigned_to="owner", target_date=date(2026, 5, 10), risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme"),
            ),
            2: (
                TrajectoryPoint(date=date(2026, 4, 29), state="Active", assigned_to="owner", target_date=date(2026, 5, 12), risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme"),
                TrajectoryPoint(date=date(2026, 5, 6), state="Resolved", assigned_to="owner", target_date=date(2026, 5, 12), risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme"),
            ),
            3: (
                TrajectoryPoint(date=date(2026, 4, 19), state="Active", assigned_to="owner", target_date=date(2026, 5, 14), risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme"),
                TrajectoryPoint(date=date(2026, 5, 10), state="Resolved", assigned_to="owner", target_date=date(2026, 5, 14), risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme"),
            ),
        },
        as_of=date(2026, 5, 10),
        window_days=7,
    )

    assert metrics is not None
    assert metrics.resolved_count == 3
    assert metrics.throughput_per_week == 3
    assert metrics.median_cycle_time_days == 14
    assert metrics.p90_cycle_time_days == 21


def test_build_velocity_metrics_returns_zero_throughput_when_no_items_resolve_in_window() -> None:
    metrics = build_velocity_metrics(
        {
            1: (
                TrajectoryPoint(date=date(2026, 5, 1), state="Active", assigned_to="owner", target_date=date(2026, 5, 20), risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme"),
            ),
            2: (
                TrajectoryPoint(date=date(2026, 4, 28), state="Active", assigned_to="owner", target_date=date(2026, 5, 22), risk_level=RiskLevel.HIGH, area_path="One\\Adventure\\Acme"),
            ),
        },
        as_of=date(2026, 5, 10),
        window_days=7,
    )

    assert metrics is not None
    assert metrics.resolved_count == 0
    assert metrics.throughput_per_week == 0
    assert metrics.median_cycle_time_days is None
    assert metrics.p90_cycle_time_days is None