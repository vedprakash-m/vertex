from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.commands.doctor_checks.milestone_health_checks import (
    build_milestone_health_warning,
    snapshot_to_work_items,
)
from src.core.models import RiskLevel


def test_snapshot_to_work_items_maps_snapshot_items() -> None:
    as_of = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    snapshot = SimpleNamespace(
        ado_data_as_of=as_of,
        items=(
            SimpleNamespace(
                id=1001,
                type="Feature",
                title="Demo",
                state="Active",
                assigned_to="demo@example.com",
                area_path="One\\Demo",
                target_date=as_of.date(),
                risk_level=RiskLevel.HIGH,
                tags=("t1", "t2"),
            ),
        ),
    )

    items = snapshot_to_work_items(snapshot)

    assert len(items) == 1
    assert items[0].id == 1001
    assert items[0].custom_fields == {"changed_date": as_of.isoformat()}
    assert items[0].fetched_at == as_of


def test_build_milestone_health_warning_returns_none_without_snapshot(tmp_path) -> None:
    warning = build_milestone_health_warning(
        edition_name="demo_weekly",
        program_id="demo",
        milestones=(),
        programs_root=tmp_path / "programs",
        archive_root=tmp_path / "archive",
    )

    assert warning is None
