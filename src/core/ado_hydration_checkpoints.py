"""Versioned provider-scoped ADO hydration checkpoints (RELO-W2.3/W2.9)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Iterable


class HydrationSubresource(str, Enum):
    ITEM = "item"
    REVISIONS = "revisions"
    COMMENTS = "comments"
    RELATIONS = "relations"
    ATTACHMENT_META = "attachment_meta"


CHECKPOINT_SCHEMA_VERSION = 2
LEGACY_CHECKPOINT_TABLE = "ado_hydration_checkpoints_legacy_v1"

_CHECKPOINT_DDL = """
CREATE TABLE IF NOT EXISTS ado_hydration_checkpoints (
    provider_instance_id TEXT NOT NULL,
    work_item_id INTEGER NOT NULL,
    subresource TEXT NOT NULL CHECK (
        subresource IN ('item', 'revisions', 'comments', 'relations', 'attachment_meta')
    ),
    schema_version INTEGER NOT NULL,
    cursor TEXT NOT NULL,
    evidence_hash TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (provider_instance_id, work_item_id, subresource)
);
CREATE TABLE IF NOT EXISTS ado_hydration_evidence (
    provider_instance_id TEXT NOT NULL,
    work_item_id INTEGER NOT NULL,
    subresource TEXT NOT NULL CHECK (
        subresource IN ('item', 'revisions', 'comments', 'relations', 'attachment_meta')
    ),
    schema_version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (provider_instance_id, work_item_id, subresource)
);
"""


@dataclass(frozen=True, slots=True)
class HydrationCheckpoint:
    work_item_id: int
    subresource: HydrationSubresource
    state: str
    schema_version: int | None
    cursor: str | None
    provider_instance_id: str = "default"
    updated_at: datetime | None = None
    reseed_reason: str | None = None
    evidence_json: str | None = None


@dataclass(frozen=True, slots=True)
class CheckpointAdvance:
    work_item_id: int
    subresource: HydrationSubresource
    cursor: str
    provider_instance_id: str = "default"
    expected_state: str = "unknown"
    expected_cursor: str | None = None
    evidence_json: str | None = None


def ensure_checkpoint_table(conn: sqlite3.Connection) -> None:
    columns = conn.execute("PRAGMA table_info(ado_hydration_checkpoints)").fetchall()
    column_names = {str(row["name"]) for row in columns}
    if columns and "provider_instance_id" not in column_names:
        legacy_name = LEGACY_CHECKPOINT_TABLE
        suffix = 2
        while conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (legacy_name,),
        ).fetchone():
            legacy_name = f"{LEGACY_CHECKPOINT_TABLE}_{suffix}"
            suffix += 1
        conn.execute(
            f"ALTER TABLE ado_hydration_checkpoints RENAME TO {legacy_name}"
        )
    elif "evidence_hash" not in column_names and columns:
        conn.execute(
            "ALTER TABLE ado_hydration_checkpoints"
            " ADD COLUMN evidence_hash TEXT NOT NULL DEFAULT ''"
        )
    conn.executescript(_CHECKPOINT_DDL)


def _validated_provider_instance_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("ADO checkpoint provider identity is required.")
    return value


def _parse_checkpoint_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _validated_checkpoint_time(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("ADO checkpoint time must be timezone-aware.")
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise ValueError("ADO checkpoint time is invalid.") from None


def read_checkpoints(
    conn: sqlite3.Connection,
    provider_instance_id: str,
    work_item_ids: Iterable[int],
    *,
    at: datetime | None = None,
) -> dict[tuple[int, HydrationSubresource], HydrationCheckpoint]:
    at = _validated_checkpoint_time(at or datetime.now(timezone.utc))
    provider_instance_id = _validated_provider_instance_id(provider_instance_id)
    ids = tuple(dict.fromkeys(int(work_item_id) for work_item_id in work_item_ids))
    result = {
        (wid, sub): HydrationCheckpoint(
            work_item_id=wid,
            subresource=sub,
            state="unknown",
            schema_version=None,
            cursor=None,
            provider_instance_id=provider_instance_id,
            updated_at=None,
            reseed_reason="checkpoint_missing",
        )
        for wid in ids
        for sub in HydrationSubresource
    }
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        rows = conn.execute(
            "SELECT c.work_item_id, c.subresource, c.schema_version, c.cursor,"
            " c.evidence_hash, c.updated_at, e.payload_json"
            " FROM ado_hydration_checkpoints AS c"
            " LEFT JOIN ado_hydration_evidence AS e"
            " ON e.provider_instance_id = c.provider_instance_id"
            " AND e.work_item_id = c.work_item_id"
            " AND e.subresource = c.subresource"
            " AND e.schema_version = c.schema_version"
            " WHERE c.provider_instance_id = ?"
            f" AND c.work_item_id IN ({','.join('?' * len(chunk))})",
            (provider_instance_id, *chunk),
        ).fetchall()
        for wid, sub, version, cursor, evidence_hash, updated_at, payload_json in rows:
            key = (int(wid), HydrationSubresource(sub))
            if (
                int(version) != CHECKPOINT_SCHEMA_VERSION
                or not isinstance(payload_json, str)
                or hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
                != evidence_hash
            ):
                continue
            try:
                _canonical_evidence_json(
                    payload_json,
                    subresource=HydrationSubresource(sub),
                    cursor=str(cursor),
                )
            except ValueError:
                continue
            parsed_at = _parse_checkpoint_time(updated_at)
            if parsed_at is None or parsed_at > at:
                continue
            result[key] = HydrationCheckpoint(
                work_item_id=key[0],
                subresource=key[1],
                state="current",
                schema_version=int(version),
                cursor=str(cursor),
                provider_instance_id=provider_instance_id,
                updated_at=parsed_at,
                evidence_json=payload_json,
            )
    return result


def plan_checkpoint_reseeds(
    checkpoints: dict[tuple[int, HydrationSubresource], HydrationCheckpoint],
    *,
    at: datetime,
    max_age_seconds: int,
    max_expired_items: int,
) -> tuple[tuple[int, str], ...]:
    unknown: set[int] = set()
    expired: dict[int, datetime] = {}
    observed_at = _validated_checkpoint_time(at)
    maximum_age = timedelta(seconds=max(max_age_seconds, 0))
    for (work_item_id, _subresource), checkpoint in checkpoints.items():
        if checkpoint.state != "current":
            unknown.add(work_item_id)
            continue
        if checkpoint.updated_at is None:
            continue
        if observed_at - checkpoint.updated_at > maximum_age:
            previous = expired.get(work_item_id)
            if previous is None or checkpoint.updated_at < previous:
                expired[work_item_id] = checkpoint.updated_at
    decisions = [(work_item_id, "checkpoint_unknown") for work_item_id in sorted(unknown)]
    decisions.extend(
        (work_item_id, "anti_entropy_expired")
        for work_item_id, _updated_at in sorted(
            expired.items(), key=lambda item: (item[1], item[0])
        )[:max(max_expired_items, 0)]
    )
    return tuple(decisions)


def _canonical_evidence_json(
    value: str | None,
    *,
    subresource: HydrationSubresource | str,
    cursor: str,
) -> str | None:
    if value is None:
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        raise ValueError("ADO checkpoint evidence must be valid JSON.") from None
    if (
        not isinstance(decoded, dict)
        or decoded.get("schema_version") != CHECKPOINT_SCHEMA_VERSION
        or decoded.get("subresource") != getattr(subresource, "value", subresource)
        or decoded.get("cursor") != cursor
        or not isinstance(decoded.get("records"), list)
    ):
        raise ValueError("ADO checkpoint evidence does not match its checkpoint.")
    return json.dumps(decoded, sort_keys=True, separators=(",", ":"))


def advance_checkpoint(
    conn: sqlite3.Connection,
    advance: CheckpointAdvance,
    *,
    provider_instance_id: str,
    at: datetime,
) -> None:
    if not conn.in_transaction:
        raise RuntimeError(
            "Checkpoint advance requires the open transaction that commits its evidence."
        )
    provider_instance_id = _validated_provider_instance_id(provider_instance_id)
    at = _validated_checkpoint_time(at)
    if (
        advance.provider_instance_id != provider_instance_id
        and advance.provider_instance_id != "default"
    ):
        raise ValueError("ADO checkpoint provider identity mismatch.")
    subresource = getattr(advance.subresource, "value", advance.subresource)
    row = conn.execute(
        "SELECT c.schema_version, c.cursor, c.evidence_hash, e.payload_json"
        " FROM ado_hydration_checkpoints AS c"
        " LEFT JOIN ado_hydration_evidence AS e"
        " ON e.provider_instance_id = c.provider_instance_id"
        " AND e.work_item_id = c.work_item_id"
        " AND e.subresource = c.subresource"
        " AND e.schema_version = c.schema_version"
        " WHERE c.provider_instance_id = ? AND c.work_item_id = ?"
        " AND c.subresource = ?",
        (provider_instance_id, int(advance.work_item_id), subresource),
    ).fetchone()
    current_state = "unknown"
    current_cursor = None
    if (
        row is not None
        and int(row["schema_version"]) == CHECKPOINT_SCHEMA_VERSION
        and isinstance(row["payload_json"], str)
        and hashlib.sha256(row["payload_json"].encode("utf-8")).hexdigest()
        == row["evidence_hash"]
    ):
        current_state = "current"
        current_cursor = str(row["cursor"])
    if (
        current_state != advance.expected_state
        or current_cursor != advance.expected_cursor
    ):
        raise ValueError("ADO checkpoint changed before evidence commit.")

    evidence_json = _canonical_evidence_json(
        advance.evidence_json
        or json.dumps(
            {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "subresource": subresource,
                "cursor": advance.cursor,
                "records": [],
                "pagination": None,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        subresource=advance.subresource,
        cursor=advance.cursor,
    )
    assert evidence_json is not None
    evidence_hash = hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO ado_hydration_evidence"
        " (provider_instance_id, work_item_id, subresource, schema_version, payload_json, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(provider_instance_id, work_item_id, subresource) DO UPDATE SET"
        " schema_version = excluded.schema_version,"
        " payload_json = excluded.payload_json,"
        " updated_at = excluded.updated_at",
        (
            provider_instance_id,
            int(advance.work_item_id),
            subresource,
            CHECKPOINT_SCHEMA_VERSION,
            evidence_json,
            at.isoformat(),
        ),
    )
    conn.execute(
        "INSERT INTO ado_hydration_checkpoints"
        " (provider_instance_id, work_item_id, subresource, schema_version,"
        " cursor, evidence_hash, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(provider_instance_id, work_item_id, subresource) DO UPDATE SET"
        " schema_version = excluded.schema_version,"
        " cursor = excluded.cursor,"
        " evidence_hash = excluded.evidence_hash,"
        " updated_at = excluded.updated_at",
        (
            provider_instance_id,
            int(advance.work_item_id),
            subresource,
            CHECKPOINT_SCHEMA_VERSION,
            advance.cursor,
            evidence_hash,
            at.isoformat(),
        ),
    )


def resolve_checkpoint_provider_instance(
    channel: str,
    provider_instance_id: str | None,
    advances: tuple[CheckpointAdvance, ...],
    checkpoint_ref_id_kind_pairs: tuple[tuple[str, str], ...],
) -> str | None:
    provider_ids = {advance.provider_instance_id for advance in advances}
    if not provider_ids:
        return provider_instance_id
    if channel != "ado":
        raise ValueError(
            "ADO checkpoint advances require matching verified work-item evidence."
        )
    verified_work_item_ids = {
        int(ref_id)
        for ref_id, ref_kind in checkpoint_ref_id_kind_pairs
        if ref_kind == "work_item" and ref_id.isdigit()
    }
    if any(
        advance.work_item_id not in verified_work_item_ids
        for advance in advances
    ):
        raise ValueError(
            "ADO checkpoint advances must belong to matching verified work-item evidence."
        )
    if provider_instance_id is None and len(provider_ids) == 1:
        provider_instance_id = next(iter(provider_ids))
    if provider_instance_id is None or provider_ids != {provider_instance_id}:
        raise ValueError(
            "ADO checkpoint advances must match one provider instance identity."
        )
    return _validated_provider_instance_id(provider_instance_id)


def commit_checkpoint_advances(
    conn: sqlite3.Connection,
    program_id: str,
    provider_instance_id: str | None,
    advances: tuple[CheckpointAdvance, ...],
    *,
    at: datetime,
) -> None:
    if not advances:
        return
    at = _validated_checkpoint_time(at)
    if provider_instance_id is None:
        raise ValueError("ADO checkpoint provider identity is required.")
    unique_advances: dict[tuple[int, HydrationSubresource], CheckpointAdvance] = {}
    for advance in advances:
        key = (int(advance.work_item_id), advance.subresource)
        previous = unique_advances.get(key)
        if previous is not None and previous != advance:
            raise ValueError("conflicting checkpoint advances for one subresource.")
        unique_advances[key] = advance
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    for work_item_id in sorted(
        {advance.work_item_id for advance in unique_advances.values()}
    ):
        exists = conn.execute(
            "SELECT 1 FROM registrations"
            " WHERE channel = 'ado' AND program_id = ?"
            " AND provider_instance_id = ? AND ref_id = ?"
            " AND ref_kind = 'work_item'",
            (program_id, provider_instance_id, str(work_item_id)),
        ).fetchone()
        if exists is None:
            raise ValueError(
                "ADO checkpoint evidence requires a matching scoped registration."
            )
    for advance in unique_advances.values():
        advance_checkpoint(
            conn,
            advance,
            provider_instance_id=provider_instance_id,
            at=at,
        )


def read_hydration_evidence(
    conn: sqlite3.Connection,
    provider_instance_id: str,
    work_item_id: int,
    subresource: HydrationSubresource,
) -> str | None:
    row = conn.execute(
        "SELECT e.payload_json, c.evidence_hash, c.cursor"
        " FROM ado_hydration_evidence AS e"
        " JOIN ado_hydration_checkpoints AS c"
        " ON c.provider_instance_id = e.provider_instance_id"
        " AND c.work_item_id = e.work_item_id"
        " AND c.subresource = e.subresource"
        " AND c.schema_version = e.schema_version"
        " WHERE e.provider_instance_id = ? AND e.work_item_id = ?"
        " AND e.subresource = ? AND e.schema_version = ?",
        (
            _validated_provider_instance_id(provider_instance_id),
            int(work_item_id),
            subresource.value,
            CHECKPOINT_SCHEMA_VERSION,
        ),
    ).fetchone()
    if row is None:
        return None
    payload = str(row["payload_json"])
    if hashlib.sha256(payload.encode("utf-8")).hexdigest() != row["evidence_hash"]:
        raise ValueError("ADO checkpoint evidence integrity mismatch.")
    try:
        _canonical_evidence_json(
            payload,
            subresource=subresource,
            cursor=str(row["cursor"]),
        )
    except ValueError:
        raise ValueError("ADO checkpoint evidence integrity mismatch.") from None
    return payload
