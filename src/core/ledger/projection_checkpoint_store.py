"""Projection checkpoint store (arch-fix.md Phase 1, CPK).

Every projection built off the event log (fact-store bridge, REV
candidate projection, future AF-6 read models) needs a durable watermark
so a restart or replay can resume from the right point rather than
reprocessing the whole log — and so staleness/drift can be *detected*
(a `ProjectionBarrier` blocking publish when the index lags the log is
AF-6 scope; this store is the substrate that makes that check possible).

One row per ``(program_id, projection_name)``: the last event processed
(id + recorded_at, not just a raw offset — so a rebuilt/rotated log can
still locate the watermark), the projector/policy code version that wrote
it (so a code change invalidates stale checkpoints instead of silently
reusing them), and a checksum (caller-defined — e.g. a hash of the
resulting projection state) for drift detection.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core._db import open_program_db

PROGRAMS_ROOT = Path(__file__).resolve().parents[3] / "programs"
_DB_FILENAME = "projection_checkpoints.sqlite3"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projection_checkpoint (
    projection_name       TEXT PRIMARY KEY,
    watermark_event_id    TEXT NOT NULL,
    watermark_recorded_at TEXT NOT NULL,
    projector_version     TEXT NOT NULL,
    policy_version        TEXT NOT NULL,
    checksum              TEXT,
    updated_at            TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class ProjectionCheckpoint:
    projection_name: str
    watermark_event_id: str
    watermark_recorded_at: datetime
    projector_version: str
    policy_version: str
    checksum: str | None
    updated_at: datetime


def get_projection_checkpoint_db_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "ledger" / _DB_FILENAME


def _wire(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def record_checkpoint(
    program_id: str,
    projection_name: str,
    *,
    watermark_event_id: str,
    watermark_recorded_at: datetime,
    projector_version: str,
    policy_version: str,
    checksum: str | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> ProjectionCheckpoint:
    path = get_projection_checkpoint_db_path(program_id, programs_root=programs_root)
    now = datetime.now(timezone.utc)
    with open_program_db(path, durability="strict") as connection:
        connection.executescript(_SCHEMA_SQL)
        connection.execute(
            """
            INSERT INTO projection_checkpoint (
                projection_name, watermark_event_id, watermark_recorded_at,
                projector_version, policy_version, checksum, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(projection_name) DO UPDATE SET
                watermark_event_id = excluded.watermark_event_id,
                watermark_recorded_at = excluded.watermark_recorded_at,
                projector_version = excluded.projector_version,
                policy_version = excluded.policy_version,
                checksum = excluded.checksum,
                updated_at = excluded.updated_at
            """,
            (
                projection_name,
                watermark_event_id,
                _wire(watermark_recorded_at),
                projector_version,
                policy_version,
                checksum,
                _wire(now),
            ),
        )
    return ProjectionCheckpoint(
        projection_name=projection_name,
        watermark_event_id=watermark_event_id,
        watermark_recorded_at=watermark_recorded_at,
        projector_version=projector_version,
        policy_version=policy_version,
        checksum=checksum,
        updated_at=now,
    )


def load_checkpoint(
    program_id: str,
    projection_name: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> ProjectionCheckpoint | None:
    path = get_projection_checkpoint_db_path(program_id, programs_root=programs_root)
    if not path.exists():
        return None
    with open_program_db(path, read_only=True) as connection:
        row = connection.execute(
            """
            SELECT projection_name, watermark_event_id, watermark_recorded_at,
                   projector_version, policy_version, checksum, updated_at
            FROM projection_checkpoint WHERE projection_name = ?
            """,
            (projection_name,),
        ).fetchone()
    if row is None:
        return None
    return _checkpoint_from_row(row)


def list_checkpoints(
    program_id: str, *, programs_root: Path = PROGRAMS_ROOT
) -> tuple[ProjectionCheckpoint, ...]:
    path = get_projection_checkpoint_db_path(program_id, programs_root=programs_root)
    if not path.exists():
        return ()
    with open_program_db(path, read_only=True) as connection:
        rows = connection.execute(
            """
            SELECT projection_name, watermark_event_id, watermark_recorded_at,
                   projector_version, policy_version, checksum, updated_at
            FROM projection_checkpoint ORDER BY projection_name
            """
        ).fetchall()
    return tuple(_checkpoint_from_row(row) for row in rows)


def _checkpoint_from_row(row: tuple[Any, ...]) -> ProjectionCheckpoint:
    (
        projection_name,
        watermark_event_id,
        watermark_recorded_at,
        projector_version,
        policy_version,
        checksum,
        updated_at,
    ) = row
    return ProjectionCheckpoint(
        projection_name=str(projection_name),
        watermark_event_id=str(watermark_event_id),
        watermark_recorded_at=_parse(str(watermark_recorded_at)),
        projector_version=str(projector_version),
        policy_version=str(policy_version),
        checksum=str(checksum) if checksum is not None else None,
        updated_at=_parse(str(updated_at)),
    )
