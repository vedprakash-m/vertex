"""FR-SG-49: Checkpoint store — snapshot mutable program stores before fact-layer promotion."""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.core.config_loader import PROGRAMS_ROOT

# Files that are captured in each checkpoint snapshot.
# Phase 1-B (declutter.md §6 1-B / R-9): T-3b runtime artifacts are now under
# ``runtime/`` — channel_registry, vertex_analytics, and m365_registry are the
# three checkpointed artifacts that hold pm_confirmed state or non-lossless
# analytics data (G-5 zero silent data loss). The read-restore logic uses
# ``program_dir / rel_path`` so the subdirectory is preserved correctly.
CHECKPOINT_FILE_PATHS: tuple[str, ...] = (
    "risk_register.yaml",
    "decisions.yaml",
    "workstreams.yaml",
    "journal/actions.jsonl",
    "journal/claims.jsonl",
    "journal/workstream_associations.jsonl",
    "dependencies.yaml",
    "milestones.yaml",
    "chronicle.jsonl",
    "runtime/channel_registry.sqlite3",
    "runtime/vertex_analytics.sqlite3",
    "runtime/m365_registry.yaml",
    # ADF-W5.9 (Section 15.2: "include AI release, tier measurements,
    # workflow/value events, cockpit history watermarks, and outbox state
    # in checkpoint/restore drills"). The append-only, hash-chained ledger
    # itself (programs/<id>/ledger/*.events.jsonl) is deliberately NOT
    # added here -- restoring an older snapshot over a newer hash-chained
    # log would break forward chain continuity, a fundamentally different
    # (and riskier) operation than restoring mutable state like
    # risk_register.yaml. The ledger's own append-only + hash-chain design
    # is its protection; this generic raw-copy checkpoint mechanism is not
    # the right tool for it.
    "runtime/tier_decisions.jsonl",
    "runtime/actuation/outbox.db",
    "journal/proposal_audit.jsonl",
    "_alerts/alerts.jsonl",
)

# Directories that are captured in each checkpoint snapshot (recursively).
CHECKPOINT_DIR_PATHS: tuple[str, ...] = (
    "overrides",
    # ADF-W5.9: cockpit history/latest.json/cockpit.html and prefetch
    # snapshots are runtime caches, not authoritative state, but restoring
    # them alongside a checkpoint keeps a restored program's cockpit/prefetch
    # view consistent with the restored point in time rather than stale.
    "runtime/cockpit",
    "runtime/prefetch",
)


def create_checkpoint_snapshot(
    program_id: str,
    issue_number: int,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    """Snapshot mutable program stores to a timestamped checkpoint directory.

    Snapshot path: programs/<prog>/checkpoints/issue_<NNN>_<iso_ts>/
        Copies: risk_register.yaml, decisions.yaml, workstreams.yaml, journal/actions.jsonl,
            journal/claims.jsonl, journal/workstream_associations.jsonl,
            dependencies.yaml, milestones.yaml, chronicle.jsonl,
            overrides/ directory.

    Returns the created checkpoint directory path.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    checkpoint_dir = programs_root / program_id / "checkpoints" / f"issue_{issue_number:03d}_{ts}"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    program_dir = programs_root / program_id
    for rel_path in CHECKPOINT_FILE_PATHS:
        src = program_dir / rel_path
        if src.exists():
            dst = checkpoint_dir / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            _copy_checkpoint_file(src, dst)

    for rel_dir in CHECKPOINT_DIR_PATHS:
        src_dir = program_dir / rel_dir
        if src_dir.exists():
            dst_dir = checkpoint_dir / rel_dir
            shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)

    return checkpoint_dir


def list_checkpoints(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[Path, ...]:
    """Return available checkpoint directories for a program, newest first."""
    checkpoint_root = programs_root / program_id / "checkpoints"
    if not checkpoint_root.exists():
        return ()
    return tuple(sorted(checkpoint_root.iterdir(), reverse=True))


def restore_checkpoint(
    program_id: str,
    checkpoint_path: Path,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    """Restore a checkpoint by copying its files back to the program directory."""
    program_dir = programs_root / program_id
    for rel_path in CHECKPOINT_FILE_PATHS:
        src = checkpoint_path / rel_path
        if src.exists():
            dst = program_dir / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            _copy_checkpoint_file(src, dst)

    for rel_dir in CHECKPOINT_DIR_PATHS:
        src_dir = checkpoint_path / rel_dir
        if src_dir.exists():
            dst_dir = program_dir / rel_dir
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)


def checkpoint_missing_relpaths(
    program_id: str,
    checkpoint_path: Path,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[str, ...]:
    """Return captured store paths that exist live but are absent from a checkpoint."""
    program_dir = programs_root / program_id
    missing: list[str] = []

    for rel_path in CHECKPOINT_FILE_PATHS:
        if (program_dir / rel_path).exists() and not (checkpoint_path / rel_path).exists():
            missing.append(rel_path)

    for rel_dir in CHECKPOINT_DIR_PATHS:
        if (program_dir / rel_dir).exists() and not (checkpoint_path / rel_dir).exists():
            missing.append(f"{rel_dir}/")

    return tuple(missing)


def _copy_checkpoint_file(src: Path, dst: Path) -> None:
    if src.suffix == ".sqlite3":
        _copy_sqlite(src, dst)
        return
    shutil.copy2(src, dst)


def _copy_sqlite(src: Path, dst: Path) -> None:
    """INV-AF-13 (WO-2 item 8): deliberately NOT routed through
    open_program_db(). This uses SQLite's native page-level online backup API
    (``Connection.backup()``), which replicates the source file's raw pages
    into the destination wholesale -- it runs no domain SQL and isn't served
    by the shared read/write connection policy (WAL/synchronous pragmas set
    on either handle are irrelevant since ``backup()`` overwrites the
    destination's pages outright). Allowlisted in
    tests/contracts/test_architecture_fitness.py's INV-AF-13 guard, same
    rationale class as unit_of_work.py's cross-database ATTACH primitive.
    """
    src_conn = sqlite3.connect(src)
    dst_conn = sqlite3.connect(dst)
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
