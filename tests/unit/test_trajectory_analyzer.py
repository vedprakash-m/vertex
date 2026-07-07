from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from src.core.models import RiskLevel
from src.core.models_v2 import TrajectoryPoint
from src.core.trajectory_analyzer import analyze_trajectories


def test_analyze_trajectories_detects_high_severity_eta_drift() -> None:
    fixture_path = Path(__file__).resolve().parents[1] / "fixtures" / "sample_trajectory.jsonl"
    if not fixture_path.exists():
        import pytest
        pytest.skip("Requires local fixture data")
    points = _load_points(fixture_path)

    patterns = analyze_trajectories({36830830: points}, as_of=date(2026, 5, 31))

    assert len(patterns) == 1
    assert patterns[0].pattern == "eta_drift"
    assert patterns[0].severity == "high"
    assert patterns[0].occurrences == 3


def test_analyze_trajectories_requires_three_reassignments_for_pattern() -> None:
    two_reassignments = (
        TrajectoryPoint(date=date(2026, 5, 1), state="Active", assigned_to="a", target_date=None, risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme"),
        TrajectoryPoint(date=date(2026, 5, 2), state="Active", assigned_to="b", target_date=None, risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme"),
        TrajectoryPoint(date=date(2026, 5, 3), state="Active", assigned_to="c", target_date=None, risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme"),
    )
    three_reassignments = two_reassignments + (
        TrajectoryPoint(date=date(2026, 5, 4), state="Active", assigned_to="d", target_date=None, risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme"),
    )

    below_threshold = analyze_trajectories({1001: two_reassignments}, as_of=date(2026, 5, 10))
    at_threshold = analyze_trajectories({1002: three_reassignments}, as_of=date(2026, 5, 10))

    assert below_threshold == ()
    assert len(at_threshold) == 1
    assert at_threshold[0].pattern == "chronic_reassign"
    assert at_threshold[0].severity == "medium"
    assert at_threshold[0].occurrences == 3


def _load_points(path: Path) -> tuple[TrajectoryPoint, ...]:
    loaded: list[TrajectoryPoint] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        payload = json.loads(raw_line)
        loaded.append(
            TrajectoryPoint(
                date=date.fromisoformat(payload["date"]),
                state=payload["state"],
                assigned_to=payload.get("assigned_to"),
                target_date=date.fromisoformat(payload["target_date"]) if payload.get("target_date") else None,
                risk_level=RiskLevel.from_string(payload["risk_level"]) if payload.get("risk_level") else None,
                area_path=payload["area_path"],
                tags=tuple(payload.get("tags") or ()),
            )
        )
    return tuple(loaded)