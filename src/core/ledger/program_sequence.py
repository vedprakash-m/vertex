"""Monotonic per-program sequence allocator (arch-fix.md Phase 1, CPK).

Provides a strictly-increasing integer per program, independent of
wall-clock ties — two events recorded in the same millisecond (or with a
clamped/regressed clock) still get a stable, total order. Backed by a
single-row SQLite table via ``open_program_db()`` so the allocation is
atomic under SQLite's own transaction locking (and, once workspace leasing
lands, serialized across hosts by the lease as well).

This is infrastructure, not itself an authority: it does not replace
``recorded_at``/hash-chain ordering in ``event_log.py`` — it exists for
CPK consumers (``UnitOfWork``, ``ProjectionCheckpointStore``,
``DurableOutboxStore``) that need a comparable, gap-tolerant ordinal
independent of parsing timestamps.
"""
from __future__ import annotations

from pathlib import Path

from src.core._db import open_program_db_with_retry as open_program_db

PROGRAMS_ROOT = Path(__file__).resolve().parents[3] / "programs"
_DB_FILENAME = "program_sequence.sqlite3"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS program_sequence (
    id    INTEGER PRIMARY KEY CHECK (id = 1),
    value INTEGER NOT NULL
);
"""


def get_program_sequence_db_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "ledger" / _DB_FILENAME


def next_sequence(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> int:
    """Atomically allocate and return the next sequence value for ``program_id``.

    The first call for a program returns 1. Every subsequent call returns a
    strictly greater integer, even across process restarts.
    """
    path = get_program_sequence_db_path(program_id, programs_root=programs_root)
    with open_program_db(path, durability="strict") as connection:
        connection.executescript(_SCHEMA_SQL)
        connection.execute("INSERT OR IGNORE INTO program_sequence (id, value) VALUES (1, 0)")
        row = connection.execute(
            "UPDATE program_sequence SET value = value + 1 WHERE id = 1 RETURNING value"
        ).fetchone()
        assert row is not None
        return int(row[0])


def current_sequence(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> int:
    """Return the current (last-allocated) sequence value without advancing it.

    Returns 0 if no sequence has ever been allocated for this program.
    """
    path = get_program_sequence_db_path(program_id, programs_root=programs_root)
    if not path.exists():
        return 0
    # read_only=True: the schema is guaranteed already present by whichever
    # next_sequence() call created this file, so no DDL is needed here (and
    # a read-only connection cannot execute writes, including idempotent DDL).
    with open_program_db(path, read_only=True) as connection:
        row = connection.execute("SELECT value FROM program_sequence WHERE id = 1").fetchone()
        return int(row[0]) if row is not None else 0
