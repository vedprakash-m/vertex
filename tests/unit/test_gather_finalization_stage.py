from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.commands.gather_pipeline.finalization_stage import compute_and_persist_plane1_changes


def test_compute_and_persist_plane1_changes_passes_db_root_to_snapshot_writers(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    gathered_at = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    sentinel_snapshot = object()
    current_snapshot = object()
    captured: dict[str, object] = {}

    compute_and_persist_plane1_changes(
        "demo",
        programs_root,
        gathered_at,
        load_program_facts=lambda *args, **kwargs: sentinel_snapshot,
        project_milestones=lambda snapshot: (),
        project_risk_entries=lambda snapshot: (),
        project_decision_entries=lambda snapshot: (),
        project_assumptions=lambda snapshot: (),
        project_workstreams=lambda snapshot: (),
        load_plane1_last_seen=lambda *args, **kwargs: object(),
        compute_plane1_changes=lambda *args, **kwargs: (),
        append_plane1_changes=lambda *args, **kwargs: None,
        build_plane1_snapshot=lambda *args, **kwargs: current_snapshot,
        shadow_write_plane1_snapshot=lambda program_id, snapshot, *, recorded_at, db_root=None, **_kwargs: captured.setdefault(
            "shadow_args",
            {
                "program_id": program_id,
                "snapshot": snapshot,
                "recorded_at": recorded_at,
                "db_root": db_root,
            },
        ),
        persist_program_fact_snapshot=lambda snapshot, *, recorded_at, db_root=None: captured.setdefault(
            "persist_args",
            {
                "snapshot": snapshot,
                "recorded_at": recorded_at,
                "db_root": db_root,
            },
        ),
        write_plane1_last_seen=lambda *args, **kwargs: None,
    )

    assert captured == {
        "shadow_args": {
            "program_id": "demo",
            "snapshot": current_snapshot,
            "recorded_at": gathered_at,
            "db_root": programs_root.parent / "vertex-db",
        },
        "persist_args": {
            "snapshot": sentinel_snapshot,
            "recorded_at": gathered_at,
            "db_root": programs_root.parent / "vertex-db",
        },
    }
