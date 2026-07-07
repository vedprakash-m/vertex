from __future__ import annotations

from datetime import date
from pathlib import Path

from src.core.models import RiskLevel
from src.core.models_v2 import TrajectoryPoint
from src.core.trajectory import (
    append_trajectory_point,
    get_trajectory_checksum_path,
    get_trajectory_path,
    list_trajectory_quarantine_paths,
    read_trajectory,
    trajectory_checksum_matches,
)


def test_append_trajectory_point_skips_timestamp_only_duplicates(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    initial = TrajectoryPoint(
        date=date(2026, 5, 8),
        state="Active",
        assigned_to="Operator",
        target_date=date(2026, 5, 30),
        risk_level=RiskLevel.MEDIUM,
        area_path="One\\Adventure\\Acme",
        tags=("acme", "release"),
    )
    duplicate_snapshot = TrajectoryPoint(
        date=date(2026, 5, 9),
        state="Active",
        assigned_to="Operator",
        target_date=date(2026, 5, 30),
        risk_level=RiskLevel.MEDIUM,
        area_path="One\\Adventure\\Acme",
        tags=("acme", "release"),
    )
    changed_snapshot = TrajectoryPoint(
        date=date(2026, 5, 10),
        state="At Risk",
        assigned_to="Operator",
        target_date=date(2026, 6, 6),
        risk_level=RiskLevel.HIGH,
        area_path="One\\Adventure\\Acme",
        tags=("acme", "release"),
    )

    wrote_initial = append_trajectory_point("acme", 1234, initial, programs_root=programs_root)
    wrote_duplicate = append_trajectory_point("acme", 1234, duplicate_snapshot, programs_root=programs_root)
    wrote_changed = append_trajectory_point("acme", 1234, changed_snapshot, programs_root=programs_root)
    points = read_trajectory("acme", 1234, programs_root=programs_root)

    assert wrote_initial is True
    assert wrote_duplicate is False
    assert wrote_changed is True
    assert points == (initial, changed_snapshot)
    assert get_trajectory_checksum_path("acme", 1234, programs_root).exists()
    assert trajectory_checksum_matches("acme", 1234, programs_root) is True


def test_read_trajectory_quarantines_invalid_jsonl_and_preserves_valid_points(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = get_trajectory_path("acme", 1234, programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            (
                '{"date":"2026-05-08","state":"Active","assigned_to":"Operator","target_date":"2026-05-30","risk_level":"medium","risk_assessment":null,"risk_assessment_comment":null,"area_path":"One\\\\Adventure\\\\Acme","tags":["acme","release"]}',
                'not-json',
                '{"date":"2026-05-10","state":"At Risk","assigned_to":"Operator","target_date":"2026-06-06","risk_level":"high","risk_assessment":null,"risk_assessment_comment":null,"area_path":"One\\\\Adventure\\\\Acme","tags":["acme","release"]}',
            )
        )
        + "\n",
        encoding="utf-8",
    )

    points = read_trajectory("acme", 1234, programs_root=programs_root)
    quarantines = list_trajectory_quarantine_paths("acme", programs_root=programs_root)

    assert len(points) == 2
    assert len(quarantines) == 1
    assert trajectory_checksum_matches("acme", 1234, programs_root) is True