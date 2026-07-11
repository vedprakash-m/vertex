from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from src.core._db import open_program_db
from src.core.ledger.event_types import get_event_schema
from src.core.ledger.source_refs import SourceRef, source_document_key


PROGRAMS_ROOT = Path(__file__).resolve().parents[3] / "programs"
SCHEMA_VERSION = "1"


@dataclass(frozen=True, slots=True)
class IndexedEventRecord:
    event_id: str
    event_type: str
    occurred_at: str
    recorded_at: str
    confidence: str
    temporal_confidence: str
    actor: str
    source_ref_type: str
    source_document_key: str
    vault_hash: str | None
    dedupe_core_hash: str | None
    is_control: bool
    file: str
    line_no: int
    content_hash: str
    superseded_by: str | None


def get_event_index_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "ledger" / f"{program_id}-index.sqlite3"


@contextmanager
def connect_event_index(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Iterator[sqlite3.Connection]:
    """Yield a connection to this program's event index.

    arch-fix.md Phase 1 (INV-AF-13): routed through ``open_program_db()``
    instead of a hand-rolled ``sqlite3.connect`` + hardcoded
    ``PRAGMA journal_mode=WAL``, so the event index picks WAL/DELETE by
    filesystem like every other store (network drives were previously
    forced into WAL, which is unsafe there). ``durability="strict"``
    preserves this store's prior always-``synchronous=FULL`` behavior
    (unlike the "balanced" default, which relaxes to NORMAL off network
    paths).
    """
    path = get_event_index_path(program_id, programs_root=programs_root)
    with open_program_db(path, durability="strict") as connection:
        _ensure_schema(connection)
        yield connection


def index_event(
    envelope: Any,
    *,
    file_path: Path,
    line_no: int,
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    with connect_event_index(envelope.program_id, programs_root=programs_root) as connection:
        _index_event_in_connection(connection, envelope, file_path=file_path, line_no=line_no)


def index_events(
    entries: tuple[tuple[Any, Path, int], ...],
    *,
    program_id: str,
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    if not entries:
        return
    with connect_event_index(program_id, programs_root=programs_root) as connection:
        for envelope, file_path, line_no in entries:
            _index_event_in_connection(connection, envelope, file_path=file_path, line_no=line_no)


def rebuild_event_index(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> int:
    from src.core.ledger.event_log import EventEnvelope, iter_event_records

    indexed = 0
    with connect_event_index(program_id, programs_root=programs_root) as connection:
        connection.execute("DELETE FROM event_entity_refs")
        connection.execute("DELETE FROM vault_refs")
        connection.execute("DELETE FROM event_index")
        connection.execute("DELETE FROM index_meta")

        for file_path, line_no, payload in iter_event_records(program_id, programs_root=programs_root):
            envelope = EventEnvelope.from_dict(payload)
            schema = get_event_schema(envelope.event_type)
            connection.execute(
                """
                INSERT OR REPLACE INTO event_index (
                    event_id, event_type, occurred_at, recorded_at, confidence,
                    temporal_confidence, actor, source_ref_type, source_document_key,
                    vault_hash, dedupe_core_hash, is_control, file, line_no,
                    content_hash, superseded_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    envelope.event_id,
                    envelope.event_type,
                    envelope.occurred_at.isoformat(),
                    envelope.recorded_at.isoformat(),
                    envelope.confidence.value,
                    envelope.temporal_confidence.value,
                    envelope.actor,
                    envelope.source_ref.ref_type,
                    source_document_key(envelope.source_ref),
                    getattr(envelope.source_ref, "vault_hash", None),
                    envelope.dedupe_core_hash,
                    1 if schema.is_control else 0,
                    file_path.name,
                    line_no,
                    envelope.content_hash,
                    None,
                ),
            )
            for entity_id in sorted(_extract_entity_refs(envelope.payload, schema.entity_ref_fields)):
                connection.execute(
                    "INSERT OR REPLACE INTO event_entity_refs (event_id, entity_id) VALUES (?, ?)",
                    (envelope.event_id, entity_id),
                )
            _insert_vault_ref(connection, envelope.source_ref, envelope.event_id, "source_ref")
            for ref in envelope.corroborating_refs:
                _insert_vault_ref(connection, ref, envelope.event_id, "corroborating_ref")
            connection.execute("DELETE FROM index_meta")
            connection.execute(
                "INSERT INTO index_meta (schema_version, last_indexed_event_id, last_indexed_at) VALUES (?, ?, ?)",
                (SCHEMA_VERSION, envelope.event_id, envelope.recorded_at.isoformat()),
            )
            indexed += 1

    return indexed


def _index_event_in_connection(connection: sqlite3.Connection, envelope: Any, *, file_path: Path, line_no: int) -> None:
    schema = get_event_schema(envelope.event_type)
    connection.execute(
        """
        INSERT OR REPLACE INTO event_index (
            event_id, event_type, occurred_at, recorded_at, confidence,
            temporal_confidence, actor, source_ref_type, source_document_key,
            vault_hash, dedupe_core_hash, is_control, file, line_no,
            content_hash, superseded_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            envelope.event_id,
            envelope.event_type,
            envelope.occurred_at.isoformat(),
            envelope.recorded_at.isoformat(),
            envelope.confidence.value,
            envelope.temporal_confidence.value,
            envelope.actor,
            envelope.source_ref.ref_type,
            source_document_key(envelope.source_ref),
            getattr(envelope.source_ref, "vault_hash", None),
            envelope.dedupe_core_hash,
            1 if schema.is_control else 0,
            file_path.name,
            line_no,
            envelope.content_hash,
            None,
        ),
    )
    connection.execute("DELETE FROM event_entity_refs WHERE event_id = ?", (envelope.event_id,))
    for entity_id in sorted(_extract_entity_refs(envelope.payload, schema.entity_ref_fields)):
        connection.execute(
            "INSERT OR REPLACE INTO event_entity_refs (event_id, entity_id) VALUES (?, ?)",
            (envelope.event_id, entity_id),
        )
    connection.execute("DELETE FROM vault_refs WHERE ref_owner_id = ? AND ref_owner_type = 'event'", (envelope.event_id,))
    _insert_vault_ref(connection, envelope.source_ref, envelope.event_id, "source_ref")
    for ref in envelope.corroborating_refs:
        _insert_vault_ref(connection, ref, envelope.event_id, "corroborating_ref")
    connection.execute("DELETE FROM index_meta")
    connection.execute(
        "INSERT INTO index_meta (schema_version, last_indexed_event_id, last_indexed_at) VALUES (?, ?, ?)",
        (SCHEMA_VERSION, envelope.event_id, envelope.recorded_at.isoformat()),
    )


def load_indexed_events(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> tuple[IndexedEventRecord, ...]:
    with connect_event_index(program_id, programs_root=programs_root) as connection:
        rows = connection.execute(
            """
            SELECT event_id, event_type, occurred_at, recorded_at, confidence,
                   temporal_confidence, actor, source_ref_type, source_document_key,
                   vault_hash, dedupe_core_hash, is_control, file, line_no,
                   content_hash, superseded_by
            FROM event_index
            ORDER BY recorded_at, event_id
            """
        ).fetchall()
    return tuple(IndexedEventRecord(*row) for row in rows)


def load_event_entity_refs(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> dict[str, tuple[str, ...]]:
    with connect_event_index(program_id, programs_root=programs_root) as connection:
        rows = connection.execute(
            "SELECT event_id, entity_id FROM event_entity_refs ORDER BY event_id, entity_id"
        ).fetchall()
    results: dict[str, list[str]] = {}
    for event_id, entity_id in rows:
        results.setdefault(event_id, []).append(entity_id)
    return {event_id: tuple(values) for event_id, values in results.items()}


def load_entity_event_ids(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> dict[str, tuple[str, ...]]:
    with connect_event_index(program_id, programs_root=programs_root) as connection:
        rows = connection.execute(
            "SELECT entity_id, event_id FROM event_entity_refs ORDER BY entity_id, event_id"
        ).fetchall()
    results: dict[str, list[str]] = {}
    for entity_id, event_id in rows:
        results.setdefault(entity_id, []).append(event_id)
    return {entity_id: tuple(values) for entity_id, values in results.items()}


def load_vault_refs(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> tuple[tuple[str, str, str, str], ...]:
    with connect_event_index(program_id, programs_root=programs_root) as connection:
        rows = connection.execute(
            "SELECT vault_hash, ref_owner_id, ref_owner_type, ref_role FROM vault_refs ORDER BY vault_hash, ref_owner_id, ref_role"
        ).fetchall()
    # open_program_db() sets row_factory=sqlite3.Row, which does NOT compare
    # equal to a plain tuple (`Row(...) == (...)` is False even with identical
    # values) — materialize plain tuples so callers' tuple-equality checks
    # keep working exactly as before this store was routed through it.
    return tuple(tuple(row) for row in rows)


def _extract_entity_refs(payload: dict[str, Any], entity_ref_fields: frozenset[str]) -> set[str]:
    refs: set[str] = set()
    for field_name in entity_ref_fields:
        value = payload.get(field_name)
        if isinstance(value, str):
            refs.add(value)
        elif isinstance(value, list):
            refs.update(item for item in value if isinstance(item, str))
        elif isinstance(value, dict) and field_name == "milestone_dates":
            refs.update(key for key in value.keys() if isinstance(key, str))
    grounded_in = payload.get("grounded_in")
    if isinstance(grounded_in, list):
        refs.update(item for item in grounded_in if isinstance(item, str))
    return refs


def _insert_vault_ref(connection: sqlite3.Connection, source_ref: SourceRef, owner_id: str, role: str) -> None:
    vault_hash = getattr(source_ref, "vault_hash", None)
    if not isinstance(vault_hash, str) or not vault_hash:
        return
    connection.execute(
        "INSERT OR REPLACE INTO vault_refs (vault_hash, ref_owner_id, ref_owner_type, ref_role) VALUES (?, ?, 'event', ?)",
        (vault_hash, owner_id, role),
    )


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS event_index (
          event_id TEXT PRIMARY KEY,
          event_type TEXT NOT NULL,
          occurred_at TEXT NOT NULL,
          recorded_at TEXT NOT NULL,
          confidence TEXT NOT NULL,
          temporal_confidence TEXT NOT NULL,
          actor TEXT NOT NULL,
          source_ref_type TEXT NOT NULL,
          source_document_key TEXT,
          vault_hash TEXT,
          dedupe_core_hash TEXT,
          is_control INTEGER NOT NULL DEFAULT 0,
          file TEXT NOT NULL,
          line_no INTEGER NOT NULL,
          content_hash TEXT NOT NULL,
          superseded_by TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_event_occurred ON event_index(occurred_at);
        CREATE INDEX IF NOT EXISTS ix_event_type ON event_index(event_type, occurred_at);
        CREATE INDEX IF NOT EXISTS ix_event_recorded ON event_index(recorded_at);
        CREATE INDEX IF NOT EXISTS ix_dedupe_core ON event_index(dedupe_core_hash);
        CREATE INDEX IF NOT EXISTS ix_vault_hash_ev ON event_index(vault_hash);

        CREATE TABLE IF NOT EXISTS event_entity_refs (
          event_id TEXT NOT NULL,
          entity_id TEXT NOT NULL,
          PRIMARY KEY (event_id, entity_id)
        );
        CREATE INDEX IF NOT EXISTS ix_eer_entity ON event_entity_refs(entity_id);

        CREATE TABLE IF NOT EXISTS vault_refs (
          vault_hash TEXT NOT NULL,
          ref_owner_id TEXT NOT NULL,
          ref_owner_type TEXT NOT NULL,
          ref_role TEXT NOT NULL,
          PRIMARY KEY (vault_hash, ref_owner_id, ref_role)
        );
        CREATE INDEX IF NOT EXISTS ix_vr_hash ON vault_refs(vault_hash);

        CREATE TABLE IF NOT EXISTS index_meta (
          schema_version TEXT NOT NULL,
          last_indexed_event_id TEXT,
          last_indexed_at TEXT
        );
        """
    )