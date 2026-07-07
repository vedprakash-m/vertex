from __future__ import annotations

import sqlite3
from pathlib import Path

from src.core.checkpoint_store import (
    CHECKPOINT_DIR_PATHS,
    CHECKPOINT_FILE_PATHS,
    checkpoint_missing_relpaths,
    create_checkpoint_snapshot,
    list_checkpoints,
    restore_checkpoint,
)


def test_create_and_restore_checkpoint_snapshot_round_trips_mutable_stores(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    (program_dir / "journal").mkdir(parents=True, exist_ok=True)
    (program_dir / "overrides").mkdir(parents=True, exist_ok=True)

    (program_dir / "risk_register.yaml").write_text("risks: [baseline]\n", encoding="utf-8")
    (program_dir / "decisions.yaml").write_text("decisions: [baseline]\n", encoding="utf-8")
    (program_dir / "workstreams.yaml").write_text("workstreams: [baseline]\n", encoding="utf-8")
    (program_dir / "dependencies.yaml").write_text("dependencies: [baseline]\n", encoding="utf-8")
    (program_dir / "milestones.yaml").write_text("milestones: [baseline]\n", encoding="utf-8")
    (program_dir / "journal" / "actions.jsonl").write_text('{"action":"baseline"}\n', encoding="utf-8")
    (program_dir / "journal" / "claims.jsonl").write_text('{"claim":"baseline"}\n', encoding="utf-8")
    (program_dir / "journal" / "workstream_associations.jsonl").write_text('{"workstream":"baseline"}\n', encoding="utf-8")
    (program_dir / "chronicle.jsonl").write_text('{"event":"baseline"}\n', encoding="utf-8")
    (program_dir / "overrides" / "issue_001.yaml").write_text("top_3_now: []\n", encoding="utf-8")

    checkpoint_path = create_checkpoint_snapshot("demo", 7, programs_root=programs_root)
    checkpoints = list_checkpoints("demo", programs_root=programs_root)

    assert checkpoint_path.name.startswith("issue_007_")
    assert checkpoints[0] == checkpoint_path

    (program_dir / "risk_register.yaml").write_text("risks: [mutated]\n", encoding="utf-8")
    (program_dir / "decisions.yaml").write_text("decisions: [mutated]\n", encoding="utf-8")
    (program_dir / "workstreams.yaml").write_text("workstreams: [mutated]\n", encoding="utf-8")
    (program_dir / "dependencies.yaml").write_text("dependencies: [mutated]\n", encoding="utf-8")
    (program_dir / "milestones.yaml").write_text("milestones: [mutated]\n", encoding="utf-8")
    (program_dir / "journal" / "actions.jsonl").write_text('{"action":"mutated"}\n', encoding="utf-8")
    (program_dir / "journal" / "claims.jsonl").write_text('{"claim":"mutated"}\n', encoding="utf-8")
    (program_dir / "journal" / "workstream_associations.jsonl").write_text('{"workstream":"mutated"}\n', encoding="utf-8")
    (program_dir / "chronicle.jsonl").write_text('{"event":"mutated"}\n', encoding="utf-8")
    (program_dir / "overrides" / "issue_001.yaml").write_text("top_3_now: [mutated]\n", encoding="utf-8")

    restore_checkpoint("demo", checkpoint_path, programs_root=programs_root)

    assert (program_dir / "risk_register.yaml").read_text(encoding="utf-8") == "risks: [baseline]\n"
    assert (program_dir / "decisions.yaml").read_text(encoding="utf-8") == "decisions: [baseline]\n"
    assert (program_dir / "workstreams.yaml").read_text(encoding="utf-8") == "workstreams: [baseline]\n"
    assert (program_dir / "dependencies.yaml").read_text(encoding="utf-8") == "dependencies: [baseline]\n"
    assert (program_dir / "milestones.yaml").read_text(encoding="utf-8") == "milestones: [baseline]\n"
    assert (program_dir / "journal" / "actions.jsonl").read_text(encoding="utf-8") == '{"action":"baseline"}\n'
    assert (program_dir / "journal" / "claims.jsonl").read_text(encoding="utf-8") == '{"claim":"baseline"}\n'
    assert (program_dir / "journal" / "workstream_associations.jsonl").read_text(encoding="utf-8") == '{"workstream":"baseline"}\n'
    assert (program_dir / "chronicle.jsonl").read_text(encoding="utf-8") == '{"event":"baseline"}\n'
    assert (program_dir / "overrides" / "issue_001.yaml").read_text(encoding="utf-8") == "top_3_now: []\n"


def test_adf_w59_new_artifact_types_round_trip(tmp_path: Path) -> None:
    # ADF-W5.9 (Section 15.2): tier measurements, workflow/value events,
    # cockpit history, and outbox state must be included in checkpoint/
    # restore drills.
    assert "runtime/tier_decisions.jsonl" in CHECKPOINT_FILE_PATHS
    assert "runtime/actuation/outbox.db" in CHECKPOINT_FILE_PATHS
    assert "journal/proposal_audit.jsonl" in CHECKPOINT_FILE_PATHS
    assert "_alerts/alerts.jsonl" in CHECKPOINT_FILE_PATHS
    assert "runtime/cockpit" in CHECKPOINT_DIR_PATHS
    assert "runtime/prefetch" in CHECKPOINT_DIR_PATHS
    # The ledger itself is deliberately excluded (append-only/hash-chained;
    # restoring an old snapshot over it would break chain continuity).
    assert not any("ledger" in path for path in CHECKPOINT_FILE_PATHS)
    assert not any("ledger" in path for path in CHECKPOINT_DIR_PATHS)

    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    (program_dir / "runtime" / "actuation").mkdir(parents=True, exist_ok=True)
    (program_dir / "runtime" / "cockpit" / "history").mkdir(parents=True, exist_ok=True)
    (program_dir / "runtime" / "prefetch").mkdir(parents=True, exist_ok=True)
    (program_dir / "journal").mkdir(parents=True, exist_ok=True)
    (program_dir / "_alerts").mkdir(parents=True, exist_ok=True)

    (program_dir / "runtime" / "tier_decisions.jsonl").write_text('{"tier":"baseline"}\n', encoding="utf-8")
    (program_dir / "journal" / "proposal_audit.jsonl").write_text('{"event":"baseline"}\n', encoding="utf-8")
    (program_dir / "_alerts" / "alerts.jsonl").write_text('{"alert":"baseline"}\n', encoding="utf-8")
    (program_dir / "runtime" / "cockpit" / "latest.json").write_text('{"program_id":"baseline"}\n', encoding="utf-8")

    checkpoint_path = create_checkpoint_snapshot("demo", 1, programs_root=programs_root)

    (program_dir / "runtime" / "tier_decisions.jsonl").write_text('{"tier":"mutated"}\n', encoding="utf-8")
    (program_dir / "journal" / "proposal_audit.jsonl").write_text('{"event":"mutated"}\n', encoding="utf-8")
    (program_dir / "_alerts" / "alerts.jsonl").write_text('{"alert":"mutated"}\n', encoding="utf-8")
    (program_dir / "runtime" / "cockpit" / "latest.json").write_text('{"program_id":"mutated"}\n', encoding="utf-8")

    restore_checkpoint("demo", checkpoint_path, programs_root=programs_root)

    assert (program_dir / "runtime" / "tier_decisions.jsonl").read_text(encoding="utf-8") == '{"tier":"baseline"}\n'
    assert (program_dir / "journal" / "proposal_audit.jsonl").read_text(encoding="utf-8") == '{"event":"baseline"}\n'
    assert (program_dir / "_alerts" / "alerts.jsonl").read_text(encoding="utf-8") == '{"alert":"baseline"}\n'
    assert (program_dir / "runtime" / "cockpit" / "latest.json").read_text(encoding="utf-8") == '{"program_id":"baseline"}\n'


def test_checkpoint_snapshot_copies_channel_registry_sqlite_via_backup_api(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    # Phase 1-B: channel_registry lives at runtime/ (canonical path).
    runtime_dir = program_dir / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    db_path = runtime_dir / "channel_registry.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE sample(value TEXT)")
        conn.execute("INSERT INTO sample(value) VALUES ('baseline')")

    checkpoint_path = create_checkpoint_snapshot("demo", 7, programs_root=programs_root)

    with sqlite3.connect(checkpoint_path / "runtime" / "channel_registry.sqlite3") as conn:
        copied_value = conn.execute("SELECT value FROM sample").fetchone()[0]
    assert copied_value == "baseline"

    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM sample")
        conn.execute("INSERT INTO sample(value) VALUES ('mutated')")

    restore_checkpoint("demo", checkpoint_path, programs_root=programs_root)

    with sqlite3.connect(db_path) as conn:
        restored_value = conn.execute("SELECT value FROM sample").fetchone()[0]
    assert restored_value == "baseline"


def test_checkpoint_missing_relpaths_reports_live_paths_added_after_snapshot(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "risk_register.yaml").write_text("risks: []\n", encoding="utf-8")

    checkpoint_path = create_checkpoint_snapshot("demo", 7, programs_root=programs_root)
    (program_dir / "journal").mkdir(parents=True, exist_ok=True)
    (program_dir / "chronicle.jsonl").write_text('{"event":"late"}\n', encoding="utf-8")
    (program_dir / "journal" / "claims.jsonl").write_text('{"claim":"late"}\n', encoding="utf-8")
    (program_dir / "workstreams.yaml").write_text("workstreams: [late]\n", encoding="utf-8")

    assert checkpoint_missing_relpaths("demo", checkpoint_path, programs_root=programs_root) == (
        "workstreams.yaml",
        "journal/claims.jsonl",
        "chronicle.jsonl",
    )
