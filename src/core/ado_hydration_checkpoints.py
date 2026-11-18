"""Versioned per-subresource ADO rich-hydration checkpoints (RELO-W2.3/W2.9).

One row per (work item, subresource). A checkpoint only advances inside the
caller's open transaction, so it commits or rolls back together with the
evidence it vouches for. A missing or older-version row reads as ``unknown``,
which forces full rehydration of that subresource.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Iterable


class HydrationSubresource(str, Enum):
    REVISIONS = "revisions"
    COMMENTS = "comments"
    RELATIONS = "relations"
    # Attachment metadata only; attachment content is never downloaded.
    ATTACHMENT_META = "attachment_meta"


CHECKPOINT_SCHEMA_VERSION = 1

CHECKPOINT_DDL = """
CREATE TABLE IF NOT EXISTS ado_hydration_checkpoints (
    work_item_id INTEGER NOT NULL,
    subresource TEXT NOT NULL CHECK (subresource IN ('revisions', 'comments', 'relations', 'attachment_meta')),
    schema_version INTEGER NOT NULL,
    cursor TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (work_item_id, subresource)
);
"""


@dataclass(frozen=True, slots=True)
class HydrationCheckpoint:
    work_item_id: int
    subresource: HydrationSubresource
    state: str  # "current" | "unknown"
    schema_version: int | None
    cursor: str | None


@dataclass(frozen=True, slots=True)
class CheckpointAdvance:
    work_item_id: int
    subresource: HydrationSubresource
    cursor: str


def ensure_checkpoint_table(conn: sqlite3.Connection) -> None:
    conn.executescript(CHECKPOINT_DDL)


def read_checkpoints(
    conn: sqlite3.Connection, work_item_ids: Iterable[int]
) -> dict[tuple[int, HydrationSubresource], HydrationCheckpoint]:
    ids = tuple(dict.fromkeys(int(work_item_id) for work_item_id in work_item_ids))
    result = {
        (wid, sub): HydrationCheckpoint(wid, sub, "unknown", None, None)
        for wid in ids
        for sub in HydrationSubresource
    }
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        rows = conn.execute(
            "SELECT work_item_id, subresource, schema_version, cursor FROM ado_hydration_checkpoints"
            f" WHERE work_item_id IN ({','.join('?' * len(chunk))})",
            chunk,
        ).fetchall()
        for wid, sub, version, cursor in rows:
            if version != CHECKPOINT_SCHEMA_VERSION:
                continue
            key = (int(wid), HydrationSubresource(sub))
            result[key] = HydrationCheckpoint(key[0], key[1], "current", version, cursor)
    return result


def advance_checkpoint(conn: sqlite3.Connection, advance: CheckpointAdvance, *, at: datetime) -> None:
    """Stage a checkpoint advance in the caller's open evidence transaction; never commits."""
    if not conn.in_transaction:
        raise RuntimeError("Checkpoint advance requires the open transaction that commits its evidence.")
    conn.execute(
        "INSERT OR REPLACE INTO ado_hydration_checkpoints"
        "(work_item_id, subresource, schema_version, cursor, updated_at) VALUES (?, ?, ?, ?, ?)",
        (
            int(advance.work_item_id),
            getattr(advance.subresource, "value", advance.subresource),
            CHECKPOINT_SCHEMA_VERSION,
            advance.cursor,
            at.isoformat(),
        ),
    )
