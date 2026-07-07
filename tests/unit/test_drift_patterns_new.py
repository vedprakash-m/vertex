from __future__ import annotations

from datetime import date

from src.core.models import RiskLevel
from src.core.models_v2 import TrajectoryPoint
from src.core.trajectory_analyzer import analyze_trajectories


def test_analyze_trajectories_detects_scope_creep_from_area_migration() -> None:
    trajectories = {
        4101: (
            _point(date(2026, 5, 1), area_path="One\\Adventure\\Acme\\Deployment"),
            _point(date(2026, 5, 8), area_path="One\\Adventure\\Contoso\\Networking"),
        ),
    }

    patterns = analyze_trajectories(trajectories, as_of=date(2026, 5, 10))

    assert len(patterns) == 1
    assert patterns[0].pattern == "scope_creep"
    assert patterns[0].severity == "medium"
    assert patterns[0].occurrences == 1


def test_analyze_trajectories_detects_scope_creep_from_new_item_burst() -> None:
    trajectories = {
        work_item_id: (_point(date(2026, 5, 8), area_path="One\\Adventure\\Acme\\Deployment"),)
        for work_item_id in (5101, 5102, 5103, 5104)
    }

    patterns = analyze_trajectories(trajectories, as_of=date(2026, 5, 10))

    scope_patterns = [pattern for pattern in patterns if pattern.pattern == "scope_creep"]

    assert len(scope_patterns) == 4
    assert all(pattern.severity == "medium" for pattern in scope_patterns)
    assert all(pattern.occurrences == 4 for pattern in scope_patterns)
    assert all(pattern.window_days == 7 for pattern in scope_patterns)


def test_analyze_trajectories_detects_priority_flip() -> None:
    trajectories = {
        6101: (
            _point(date(2026, 4, 20), risk_level=RiskLevel.HIGH),
            _point(date(2026, 4, 28), risk_level=RiskLevel.LOW),
            _point(date(2026, 5, 8), risk_level=RiskLevel.HIGH),
        ),
    }

    patterns = analyze_trajectories(trajectories, as_of=date(2026, 5, 10))

    assert len(patterns) == 1
    assert patterns[0].pattern == "priority_flip"
    assert patterns[0].severity == "medium"
    assert patterns[0].occurrences == 2


def test_analyze_trajectories_detects_blocked_long() -> None:
    trajectories = {
        7101: (
            _point(date(2026, 4, 20), state="Blocked", assigned_to="owner@example.com"),
            _point(date(2026, 4, 30), state="Active", assigned_to="owner@example.com", tags=("blocked",)),
            _point(date(2026, 5, 8), state="Active", assigned_to="owner@example.com", tags=("blocked",)),
        ),
    }

    patterns = analyze_trajectories(trajectories, as_of=date(2026, 5, 10))

    assert len(patterns) == 1
    assert patterns[0].pattern == "blocked_long"
    assert patterns[0].severity == "high"
    assert patterns[0].window_days == 14


def test_analyze_trajectories_detects_eta_compression() -> None:
    trajectories = {
        8101: (
            _point(date(2026, 4, 25), target_date=date(2026, 5, 10)),
            _point(date(2026, 5, 1), target_date=date(2026, 5, 15)),
            _point(date(2026, 5, 5), target_date=date(2026, 5, 20)),
            _point(date(2026, 5, 8), target_date=date(2026, 5, 12)),
        ),
    }

    patterns = analyze_trajectories(trajectories, as_of=date(2026, 5, 10))

    compression = next((p for p in patterns if p.pattern == "eta_compression"), None)
    assert compression is not None
    assert compression.severity == "medium"
    assert compression.occurrences == 3


def _point(
    point_date: date,
    *,
    state: str = "Active",
    assigned_to: str | None = "owner@example.com",
    target_date: date | None = None,
    risk_level: RiskLevel | None = RiskLevel.MEDIUM,
    area_path: str = "One\\Adventure\\Acme\\Deployment",
    tags: tuple[str, ...] = (),
) -> TrajectoryPoint:
    return TrajectoryPoint(
        date=point_date,
        state=state,
        assigned_to=assigned_to,
        target_date=target_date,
        risk_level=risk_level,
        area_path=area_path,
        tags=tags,
    )