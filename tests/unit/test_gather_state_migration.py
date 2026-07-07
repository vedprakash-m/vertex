from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.core.gather_state_store import load_gather_state, write_gather_state


def test_load_gather_state_reads_schema_1_and_write_gather_state_rewrites_schema_2(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    legacy_path = program_dir / "gather_state.json"
    legacy_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "program_id": "acme",
                "gathered_at": "2026-05-17T14:02:11Z",
                "scanned_items": 4,
                "discovered_signals": 2,
                "new_signals": 1,
                "pending_review": 1,
                "trajectory_updates": 0,
                "auto_reviews_written": 0,
                "ado_calls": 3,
                "archived_journal_files": 0,
                "background_proposals": 0,
            }
        ),
        encoding="utf-8",
    )

    state = load_gather_state("acme", programs_root=programs_root)

    assert state is not None
    rewritten_path = write_gather_state(
        "acme",
        gathered_at=state.gathered_at,
        scanned_items=state.scanned_items,
        discovered_signals=state.discovered_signals,
        new_signals=state.new_signals,
        pending_review=state.pending_review,
        trajectory_updates=state.trajectory_updates,
        auto_reviews_written=state.auto_reviews_written,
        ado_calls=state.ado_calls,
        archived_journal_files=state.archived_journal_files,
        background_proposals=state.background_proposals,
        programs_root=programs_root,
        query_states={
            "acme-deployment-p50-p90": {
                "last_attempted_at": datetime(2026, 5, 17, 14, 2, 11, tzinfo=timezone.utc),
                "last_cycle_succeeded": True,
            }
        },
    )

    payload = json.loads(rewritten_path.read_text(encoding="utf-8"))

    assert payload["schema_version"] == "2.0"
    assert payload["scanned_items"] == 4
    assert payload["discovered_signals"] == 2
    assert payload["new_signals"] == 1
    assert payload["queries"]["acme-deployment-p50-p90"]["last_attempted_at"] == "2026-05-17T14:02:11Z"
    assert payload["queries"]["acme-deployment-p50-p90"]["last_cycle_succeeded"] is True