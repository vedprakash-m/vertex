"""SQLite-backed candidate + decision store (W7-1 / PS-27).

Replaces JSONL files as the operational source-of-truth for candidate and
triage-decision queries.  JSONL files (``pending.jsonl`` / ``triaged.jsonl``)
are retained as an append-only audit trail but are no longer the read path.

Root cause of PS-27: ``append_jsonl_line`` rotates ``pending.jsonl`` to
``rotated/pending.<ts>.<n>.jsonl`` when the file exceeds 10 MB; the old
``load_pending_candidates`` only read from the current ``pending.jsonl``, so
every rotated file was invisible to operational queries.  SQLite retains all
rows across any number of rotations.

Schema (``candidates.db``):
  ``candidates``          – one row per candidate; UNIQUE on candidate_id only
  ``candidate_decisions`` – append-only history (one row per decision event)
  ``_meta``               – schema_version sentinel

Migration: on the first open of a program's DB (i.e. ``candidates.db`` is
absent), all existing JSONL records (current + rotated files) are imported.
The migration is idempotent: if the DB already has rows it returns immediately.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

log = logging.getLogger(__name__)

DB_FILENAME = "candidates.db"
_SCHEMA_VERSION = "1"

# S-1 outbox state machine values (acceptance-time durability, §5.7 / PS-F)
OUTBOX_STATUS_PENDING    = "pending"     # written before projection starts
OUTBOX_STATUS_PROJECTED  = "projected"   # projection completed successfully
OUTBOX_STATUS_DEAD_LETTER = "dead_letter" # max retries exceeded; surfaced in doctor
OUTBOX_MAX_ATTEMPTS = 3  # max projection attempts before dead-letter (§5.7)

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS _meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
    candidate_id        TEXT PRIMARY KEY,
    program_id          TEXT NOT NULL,
    batch_id            TEXT NOT NULL,
    source_document_key TEXT NOT NULL,
    dedupe_key          TEXT NOT NULL,
    staged_at           TEXT NOT NULL,
    payload_json        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_c_batch    ON candidates (batch_id);
CREATE INDEX IF NOT EXISTS idx_c_program  ON candidates (program_id);
CREATE INDEX IF NOT EXISTS idx_c_dedupe   ON candidates (dedupe_key);
CREATE TABLE IF NOT EXISTS candidate_decisions (
    decision_rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id   TEXT NOT NULL,
    kind           TEXT NOT NULL,
    decided_at     TEXT NOT NULL,
    triage_actor   TEXT NOT NULL,
    payload_json   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_d_candidate  ON candidate_decisions (candidate_id);
CREATE INDEX IF NOT EXISTS idx_d_decided_at ON candidate_decisions (decided_at);
CREATE TABLE IF NOT EXISTS projection_outbox (
    outbox_id         TEXT PRIMARY KEY,
    candidate_id      TEXT NOT NULL,
    program_id        TEXT NOT NULL,
    source_event_id   TEXT NOT NULL,
    enqueued_at       TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending',
    attempt_count     INTEGER NOT NULL DEFAULT 0,
    last_attempted_at TEXT,
    projected_at      TEXT,
    failure_reason    TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_status     ON projection_outbox (status);
CREATE INDEX IF NOT EXISTS idx_outbox_program    ON projection_outbox (program_id);
CREATE INDEX IF NOT EXISTS idx_outbox_candidate  ON projection_outbox (candidate_id);
"""

# Migration SQL: add outbox table to existing DBs (idempotent via IF NOT EXISTS)
_OUTBOX_MIGRATION_SQL = """\
CREATE TABLE IF NOT EXISTS projection_outbox (
    outbox_id         TEXT PRIMARY KEY,
    candidate_id      TEXT NOT NULL,
    program_id        TEXT NOT NULL,
    source_event_id   TEXT NOT NULL,
    enqueued_at       TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending',
    attempt_count     INTEGER NOT NULL DEFAULT 0,
    last_attempted_at TEXT,
    projected_at      TEXT,
    failure_reason    TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_status     ON projection_outbox (status);
CREATE INDEX IF NOT EXISTS idx_outbox_program    ON projection_outbox (program_id);
CREATE INDEX IF NOT EXISTS idx_outbox_candidate  ON projection_outbox (candidate_id);
"""


def candidate_db_path(db_dir: Path) -> Path:
    return db_dir / DB_FILENAME


@contextmanager
def _open_db(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_candidate_db(db_dir: Path) -> None:
    """Create tables and indexes if absent; safe to call repeatedly (CREATE IF NOT EXISTS)."""
    db_dir.mkdir(parents=True, exist_ok=True)
    with _open_db(candidate_db_path(db_dir)) as conn:
        conn.executescript(_SCHEMA_SQL)
        # S-1: idempotent migration for existing DBs that pre-date the outbox table
        conn.executescript(_OUTBOX_MIGRATION_SQL)
        conn.execute(
            "INSERT OR IGNORE INTO _meta (key, value) VALUES ('schema_version', ?)",
            (_SCHEMA_VERSION,),
        )


def sqlite_insert_candidate(
    db_dir: Path,
    *,
    candidate_id: str,
    program_id: str,
    batch_id: str,
    source_document_key: str,
    dedupe_key: str,
    staged_at: str,
    payload_json: str,
) -> bool:
    """Insert a candidate row.

    Returns True if inserted, False if a duplicate exists (``candidate_id``
    PRIMARY KEY violation — same as the original JSONL idempotency check).
    """
    try:
        with _open_db(candidate_db_path(db_dir)) as conn:
            conn.execute(
                """INSERT INTO candidates
                   (candidate_id, program_id, batch_id, source_document_key,
                    dedupe_key, staged_at, payload_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (candidate_id, program_id, batch_id, source_document_key,
                 dedupe_key, staged_at, payload_json),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def sqlite_load_candidates(db_dir: Path) -> list[dict]:
    """Return all candidate rows as dicts ordered by staged_at then candidate_id."""
    db_path = candidate_db_path(db_dir)
    if not db_path.exists():
        return []
    with _open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT payload_json FROM candidates ORDER BY staged_at, candidate_id"
        ).fetchall()
    return [json.loads(row["payload_json"]) for row in rows]


def sqlite_insert_decision(
    db_dir: Path,
    *,
    candidate_id: str,
    kind: str,
    decided_at: str,
    triage_actor: str,
    payload_json: str,
) -> None:
    """Append a decision row (one row per decision event, like JSONL).

    ``load_triage_decisions`` returns rows in ``decided_at`` order so callers
    that want "latest per candidate" iterate and keep the last (same semantics
    as the old JSONL append-only log).
    """
    with _open_db(candidate_db_path(db_dir)) as conn:
        conn.execute(
            """INSERT INTO candidate_decisions
               (candidate_id, kind, decided_at, triage_actor, payload_json)
               VALUES (?, ?, ?, ?, ?)""",
            (candidate_id, kind, decided_at, triage_actor, payload_json),
        )


# Keep old name as alias so any callers that weren't updated yet still work.
sqlite_upsert_decision = sqlite_insert_decision


def sqlite_load_decisions(db_dir: Path) -> list[dict]:
    """Return all decision rows (full history, multiple per candidate) ordered by decided_at."""
    db_path = candidate_db_path(db_dir)
    if not db_path.exists():
        return []
    with _open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT payload_json FROM candidate_decisions ORDER BY decided_at, candidate_id"
        ).fetchall()
    return [json.loads(row["payload_json"]) for row in rows]


def migrate_jsonl_to_sqlite(
    db_dir: Path,
    *,
    pending_path: Path,
    triaged_path: Path,
) -> tuple[int, int]:
    """Import all existing JSONL records (current + rotated files) into SQLite.

    Safe to call repeatedly — skips immediately if the DB already has rows.
    Returns ``(candidates_imported, decisions_imported)``.
    """
    db_path = candidate_db_path(db_dir)
    if db_path.exists():
        with _open_db(db_path) as conn:
            if conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] > 0:
                return 0, 0

    from src.core.jsonl_utils import list_rotated_jsonl_paths, read_jsonl_records

    rotated_dir = pending_path.parent / "rotated"
    pending_paths: list[Path] = list(list_rotated_jsonl_paths(rotated_dir, stem="pending"))
    if pending_path.exists():
        pending_paths.append(pending_path)

    triaged_paths: list[Path] = list(list_rotated_jsonl_paths(rotated_dir, stem="triaged"))
    if triaged_path.exists():
        triaged_paths.append(triaged_path)

    cand_count = 0
    for p in pending_paths:
        for row in read_jsonl_records(p):
            cid = str(row.get("candidate_id", ""))
            if not cid:
                continue
            inserted = sqlite_insert_candidate(
                db_dir,
                candidate_id=cid,
                program_id=str(row.get("program_id", "")),
                batch_id=str(row.get("batch_id", "")),
                source_document_key=str(row.get("source_document_key", "")),
                dedupe_key=str(row.get("dedupe_key", "")),
                staged_at=str(row.get("staged_at") or row.get("proposed_occurred_at", "")),
                payload_json=json.dumps(row, sort_keys=True),
            )
            if inserted:
                cand_count += 1

    dec_count = 0
    for p in triaged_paths:
        for row in read_jsonl_records(p):
            cid = str(row.get("candidate_id", ""))
            if not cid:
                continue
            sqlite_upsert_decision(
                db_dir,
                candidate_id=cid,
                kind=str(row.get("kind", "")),
                decided_at=str(row.get("decided_at", "")),
                triage_actor=str(row.get("triage_actor", "")),
                payload_json=json.dumps(row, sort_keys=True),
            )
            dec_count += 1

    if cand_count or dec_count:
        log.info(
            "candidate_sqlite_store: migrated %d candidates + %d decisions from JSONL",
            cand_count,
            dec_count,
        )
    return cand_count, dec_count


# ---------------------------------------------------------------------------
# S-1: projection_outbox CRUD (acceptance-time durability, §5.7 / PS-F)
# ---------------------------------------------------------------------------

def outbox_enqueue(
    db_dir: Path,
    *,
    outbox_id: str,
    candidate_id: str,
    program_id: str,
    source_event_id: str,
    enqueued_at: str,
) -> None:
    """Write a pending outbox row BEFORE calling project_program_events."""
    with _open_db(candidate_db_path(db_dir)) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO projection_outbox
                (outbox_id, candidate_id, program_id, source_event_id, enqueued_at,
                 status, attempt_count)
            VALUES (?, ?, ?, ?, ?, ?, 0)
            """,
            (outbox_id, candidate_id, program_id, source_event_id, enqueued_at,
             OUTBOX_STATUS_PENDING),
        )


def outbox_mark_projected(db_dir: Path, *, outbox_id: str, projected_at: str) -> None:
    """Mark a pending outbox entry as successfully projected."""
    with _open_db(candidate_db_path(db_dir)) as conn:
        conn.execute(
            """
            UPDATE projection_outbox
            SET status='projected', projected_at=?, last_attempted_at=?,
                attempt_count=attempt_count+1
            WHERE outbox_id=?
            """,
            (projected_at, projected_at, outbox_id),
        )


def outbox_mark_failed(
    db_dir: Path,
    *,
    outbox_id: str,
    attempted_at: str,
    failure_reason: str,
) -> str:
    """Increment attempt_count; return new status (pending or dead_letter)."""
    with _open_db(candidate_db_path(db_dir)) as conn:
        conn.execute(
            """
            UPDATE projection_outbox
            SET attempt_count=attempt_count+1, last_attempted_at=?, failure_reason=?
            WHERE outbox_id=?
            """,
            (attempted_at, failure_reason, outbox_id),
        )
        row = conn.execute(
            "SELECT attempt_count FROM projection_outbox WHERE outbox_id=?",
            (outbox_id,),
        ).fetchone()
        count = row["attempt_count"] if row else OUTBOX_MAX_ATTEMPTS
        if count >= OUTBOX_MAX_ATTEMPTS:
            conn.execute(
                "UPDATE projection_outbox SET status=? WHERE outbox_id=?",
                (OUTBOX_STATUS_DEAD_LETTER, outbox_id),
            )
            return OUTBOX_STATUS_DEAD_LETTER
        return OUTBOX_STATUS_PENDING


def outbox_list_pending(db_dir: Path, *, program_id: str | None = None) -> list[dict]:
    """Return all pending outbox rows, optionally filtered by program_id."""
    db_path = candidate_db_path(db_dir)
    if not db_path.exists():
        return []
    with _open_db(db_path) as conn:
        if program_id:
            rows = conn.execute(
                "SELECT * FROM projection_outbox WHERE status=? AND program_id=? ORDER BY enqueued_at",
                (OUTBOX_STATUS_PENDING, program_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM projection_outbox WHERE status=? ORDER BY enqueued_at",
                (OUTBOX_STATUS_PENDING,),
            ).fetchall()
    return [dict(r) for r in rows]


def outbox_list_dead_letters(db_dir: Path, *, program_id: str | None = None) -> list[dict]:
    """Return dead-letter outbox rows for operator review (`vertex doctor`)."""
    db_path = candidate_db_path(db_dir)
    if not db_path.exists():
        return []
    with _open_db(db_path) as conn:
        if program_id:
            rows = conn.execute(
                "SELECT * FROM projection_outbox WHERE status=? AND program_id=? ORDER BY enqueued_at",
                (OUTBOX_STATUS_DEAD_LETTER, program_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM projection_outbox WHERE status=? ORDER BY enqueued_at",
                (OUTBOX_STATUS_DEAD_LETTER,),
            ).fetchall()
    return [dict(r) for r in rows]

