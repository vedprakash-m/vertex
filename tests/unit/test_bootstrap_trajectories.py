from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

bootstrap_trajectories = pytest.importorskip(
    "scripts.bootstrap_trajectories",
    reason="scripts/bootstrap_trajectories.py is a private operator script not included in the public repo",
)
from src.core.models_v2 import ADOConfig, Program
from src.core.trajectory import read_trajectory
from src.core.trajectory_analyzer import analyze_trajectories


def test_bootstrap_trajectories_seeds_history_and_is_idempotent(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    item = bootstrap_trajectories.BootstrapWorkItem(
        id=36830830,
        state="At Risk",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        target_date=date(2026, 6, 10),
        area_path="One\\Adventure\\Acme",
        tags=("acme",),
        changed_date=datetime(2026, 5, 4, 10, 0, tzinfo=timezone.utc),
        revision_rows=(
            _revision_row(1, datetime(2026, 4, 10, 9, 0, tzinfo=timezone.utc), "Active", "priya@example.com", "2026-05-20"),
            _revision_row(2, datetime(2026, 4, 20, 9, 0, tzinfo=timezone.utc), "Active", "priya@example.com", "2026-05-27"),
            _revision_row(3, datetime(2026, 4, 27, 9, 0, tzinfo=timezone.utc), "Active", "priya@example.com", "2026-06-03"),
            _revision_row(4, datetime(2026, 5, 4, 9, 0, tzinfo=timezone.utc), "At Risk", "priya@example.com", "2026-06-10"),
        ),
    )

    monkeypatch.setattr(bootstrap_trajectories, "_load_program", lambda program_id, programs_root: program)

    first = bootstrap_trajectories.bootstrap_trajectories(
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, as_of: ((item,), 3),
    )
    second = bootstrap_trajectories.bootstrap_trajectories(
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, as_of: ((item,), 3),
    )

    points = read_trajectory("acme", 36830830, programs_root=programs_root)
    patterns = analyze_trajectories({36830830: points}, as_of=date(2026, 5, 10))

    assert first.appended_points == 4
    assert first.seeded_items == 1
    assert second.appended_points == 0
    assert second.skipped_items == 1
    assert tuple(point.target_date for point in points) == (
        date(2026, 5, 20),
        date(2026, 5, 27),
        date(2026, 6, 3),
        date(2026, 6, 10),
    )
    assert len(patterns) == 1
    assert patterns[0].pattern == "eta_drift"
    assert patterns[0].severity == "high"
    assert patterns[0].occurrences == 3


def _revision_row(rev: int, changed_at: datetime, state: str, assigned_to: str, target_date: str) -> dict[str, object]:
    return {
        "rev": rev,
        "fields": {
            "System.ChangedDate": changed_at.isoformat(),
            "System.State": state,
            "System.AssignedTo": {"displayName": assigned_to.split("@", 1)[0], "uniqueName": assigned_to},
            "System.AreaPath": "One\\Adventure\\Acme",
            "System.Tags": "acme",
            "Microsoft.VSTS.Scheduling.TargetDate": target_date,
        },
    }