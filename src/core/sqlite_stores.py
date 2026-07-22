from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Literal, cast

from src.core._db import open_program_db
from src.core.journal import PROGRAMS_ROOT
from src.core.models import Confidence, RiskLevel
from src.core.models_v2 import ReviewPolicy, Signal, SignalReviewDecision, SignalThreadLink, SignalUsageMarker, TrajectoryPoint


_DB_FILENAME = "vertex_store.sqlite3"


def get_program_sqlite_store_path(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / _DB_FILENAME


class SQLiteSignalStore:
    def __init__(self, programs_root: Path = PROGRAMS_ROOT) -> None:
        self._programs_root = programs_root

    def append(self, signal: Signal) -> None:
        with _connect_program_db(signal.program_id, programs_root=self._programs_root) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO signals (
                    program_id,
                    signal_id,
                    timestamp,
                    source,
                    workstream_id,
                    entity_refs_json,
                    text,
                    raw_ref,
                    confidence,
                    metadata_json,
                    thread_id,
                    review_policy
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal.program_id,
                    signal.id,
                    _encode_datetime(signal.timestamp),
                    signal.source,
                    signal.workstream_id,
                    json.dumps(list(signal.entity_refs)),
                    signal.text,
                    signal.raw_ref,
                    signal.confidence.value,
                    json.dumps(signal.metadata, sort_keys=True) if signal.metadata is not None else None,
                    signal.thread_id,
                    signal.review_policy.value if signal.review_policy is not None else None,
                ),
            )

    def read(
        self,
        program_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
        **filters: Any,
    ) -> tuple[Signal, ...]:
        unexpected_filters = sorted(key for key in filters if key != "workstream_id")
        if unexpected_filters:
            raise TypeError(f"Unsupported signal filters: {', '.join(unexpected_filters)}")

        clauses = ["program_id = ?"]
        params: list[Any] = [program_id]
        if start is not None:
            clauses.append("timestamp >= ?")
            params.append(_encode_datetime(_normalize_datetime(start)))
        if end is not None:
            clauses.append("timestamp <= ?")
            params.append(_encode_datetime(_normalize_datetime(end)))
        workstream_id = filters.get("workstream_id")
        if workstream_id is not None:
            clauses.append("workstream_id = ?")
            params.append(workstream_id)

        query = (
            "SELECT signal_id, timestamp, source, workstream_id, entity_refs_json, text, raw_ref, confidence, metadata_json, thread_id, review_policy "
            "FROM signals WHERE " + " AND ".join(clauses) + " ORDER BY timestamp ASC, signal_id ASC"
        )
        with _connect_program_db(program_id, programs_root=self._programs_root) as connection:
            rows = connection.execute(query, params).fetchall()
            thread_links = self.read_threads(program_id)

        signals: list[Signal] = []
        for row in rows:
            signal = Signal(
                id=str(row["signal_id"]),
                timestamp=_parse_datetime(row["timestamp"]),
                source=str(row["source"]),
                program_id=program_id,
                workstream_id=_optional_string(row["workstream_id"]),
                entity_refs=tuple(str(value) for value in json.loads(str(row["entity_refs_json"]))),
                text=str(row["text"]),
                raw_ref=_optional_string(row["raw_ref"]),
                confidence=Confidence.from_string(str(row["confidence"])),
                metadata=_parse_json_object(row["metadata_json"]),
                thread_id=_optional_string(row["thread_id"]),
                review_policy=(
                    ReviewPolicy.from_string(str(row["review_policy"]))
                    if row["review_policy"] is not None
                    else None
                ),
            )
            thread_link = thread_links.get(signal.id)
            if thread_link is not None and signal.thread_id != thread_link.thread_id:
                signal = replace(signal, thread_id=thread_link.thread_id)
            signals.append(signal)
        return tuple(signals)

    def read_reviews(self, program_id: str) -> dict[str, SignalReviewDecision]:
        with _connect_program_db(program_id, programs_root=self._programs_root) as connection:
            rows = connection.execute(
                """
                SELECT signal_id, decision, reviewed_at, reviewed_by, note
                FROM signal_reviews
                WHERE program_id = ?
                ORDER BY reviewed_at ASC, rowid ASC
                """,
                (program_id,),
            ).fetchall()

        reviews: dict[str, SignalReviewDecision] = {}
        for row in rows:
            review = SignalReviewDecision(
                signal_id=str(row["signal_id"]),
                decision=cast(Literal["approved", "dismissed", "deferred"], str(row["decision"])),
                reviewed_at=_parse_datetime(row["reviewed_at"]),
                reviewed_by=str(row["reviewed_by"]),
                note=_optional_string(row["note"]),
            )
            reviews[review.signal_id] = review
        return reviews

    def append_review(self, program_id: str, decision: SignalReviewDecision) -> None:
        with _connect_program_db(program_id, programs_root=self._programs_root) as connection:
            connection.execute(
                """
                INSERT INTO signal_reviews (program_id, signal_id, decision, reviewed_at, reviewed_by, note)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    program_id,
                    decision.signal_id,
                    decision.decision,
                    _encode_datetime(decision.reviewed_at),
                    decision.reviewed_by,
                    decision.note,
                ),
            )

    def read_usage_markers(self, program_id: str) -> tuple[SignalUsageMarker, ...]:
        with _connect_program_db(program_id, programs_root=self._programs_root) as connection:
            rows = connection.execute(
                """
                SELECT signal_id, issue_number, edition_id, manifest_id, used_at
                FROM signal_usage_markers
                WHERE program_id = ?
                ORDER BY used_at ASC, rowid ASC
                """,
                (program_id,),
            ).fetchall()

        return tuple(
            SignalUsageMarker(
                signal_id=str(row["signal_id"]),
                issue_number=int(row["issue_number"]),
                edition_id=str(row["edition_id"]),
                manifest_id=str(row["manifest_id"]),
                used_at=_parse_datetime(row["used_at"]),
            )
            for row in rows
        )

    def append_usage_marker(self, program_id: str, marker: SignalUsageMarker) -> None:
        with _connect_program_db(program_id, programs_root=self._programs_root) as connection:
            connection.execute(
                """
                INSERT INTO signal_usage_markers (program_id, signal_id, issue_number, edition_id, manifest_id, used_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    program_id,
                    marker.signal_id,
                    marker.issue_number,
                    marker.edition_id,
                    marker.manifest_id,
                    _encode_datetime(marker.used_at),
                ),
            )

    def read_threads(self, program_id: str) -> dict[str, SignalThreadLink]:
        with _connect_program_db(program_id, programs_root=self._programs_root) as connection:
            rows = connection.execute(
                """
                SELECT signal_id, thread_id, linked_at, linked_by
                FROM signal_threads
                WHERE program_id = ?
                ORDER BY linked_at ASC, rowid ASC
                """,
                (program_id,),
            ).fetchall()

        threads: dict[str, SignalThreadLink] = {}
        for row in rows:
            link = SignalThreadLink(
                signal_id=str(row["signal_id"]),
                thread_id=str(row["thread_id"]),
                linked_at=_parse_datetime(row["linked_at"]),
                linked_by=str(row["linked_by"]),
            )
            threads[link.signal_id] = link
        return threads

    def append_thread(self, program_id: str, link: SignalThreadLink) -> None:
        with _connect_program_db(program_id, programs_root=self._programs_root) as connection:
            connection.execute(
                """
                INSERT INTO signal_threads (program_id, signal_id, thread_id, linked_at, linked_by)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    program_id,
                    link.signal_id,
                    link.thread_id,
                    _encode_datetime(link.linked_at),
                    link.linked_by,
                ),
            )


class SQLiteTrajectoryStore:
    def __init__(self, programs_root: Path = PROGRAMS_ROOT) -> None:
        self._programs_root = programs_root

    def append(self, program_id: str, work_item_id: int, point: TrajectoryPoint) -> bool:
        with _connect_program_db(program_id, programs_root=self._programs_root) as connection:
            row = connection.execute(
                """
                SELECT point_date, state, assigned_to, target_date, risk_level, area_path, tags_json,
                       risk_assessment, risk_assessment_comment
                FROM trajectory_points
                WHERE program_id = ? AND work_item_id = ?
                ORDER BY point_date DESC, inserted_at DESC, rowid DESC
                LIMIT 1
                """,
                (program_id, work_item_id),
            ).fetchone()
            if row is not None:
                previous = _trajectory_point_from_row(row)
                if not _trajectory_has_material_change(previous, point):
                    return False

            connection.execute(
                """
                INSERT INTO trajectory_points (
                    program_id,
                    work_item_id,
                    point_date,
                    state,
                    assigned_to,
                    target_date,
                    risk_level,
                    area_path,
                    tags_json,
                    risk_assessment,
                    risk_assessment_comment,
                    inserted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    program_id,
                    work_item_id,
                    point.date.isoformat(),
                    point.state,
                    point.assigned_to,
                    point.target_date.isoformat() if point.target_date is not None else None,
                    point.risk_level.value if point.risk_level is not None else None,
                    point.area_path,
                    json.dumps(list(point.tags)),
                    point.risk_assessment,
                    point.risk_assessment_comment,
                    _encode_datetime(datetime.now(timezone.utc)),
                ),
            )
        return True

    def list_work_item_ids(self, program_id: str) -> tuple[int, ...]:
        with _connect_program_db(program_id, programs_root=self._programs_root) as connection:
            rows = connection.execute(
                "SELECT DISTINCT work_item_id FROM trajectory_points WHERE program_id = ? ORDER BY work_item_id ASC",
                (program_id,),
            ).fetchall()
        return tuple(int(row[0]) for row in rows)

    def read(
        self,
        program_id: str,
        work_item_id: int,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[TrajectoryPoint, ...]:
        clauses = ["program_id = ?", "work_item_id = ?"]
        params: list[Any] = [program_id, work_item_id]
        if start is not None:
            clauses.append("point_date >= ?")
            params.append(start.isoformat())
        if end is not None:
            clauses.append("point_date <= ?")
            params.append(end.isoformat())

        query = (
            "SELECT point_date, state, assigned_to, target_date, risk_level, area_path, tags_json, "
            "risk_assessment, risk_assessment_comment "
            "FROM trajectory_points WHERE " + " AND ".join(clauses) + " ORDER BY point_date ASC, inserted_at ASC, rowid ASC"
        )
        with _connect_program_db(program_id, programs_root=self._programs_root) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(_trajectory_point_from_row(row) for row in rows)


def build_sqlite_signal_store(*, programs_root: Path = PROGRAMS_ROOT) -> SQLiteSignalStore:
    return SQLiteSignalStore(programs_root=programs_root)


def build_sqlite_trajectory_store(*, programs_root: Path = PROGRAMS_ROOT) -> SQLiteTrajectoryStore:
    return SQLiteTrajectoryStore(programs_root=programs_root)


def read_sqlite_signal_review_log(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[SignalReviewDecision, ...]:
    with _connect_program_db(program_id, programs_root=programs_root) as connection:
        rows = connection.execute(
            """
            SELECT signal_id, decision, reviewed_at, reviewed_by, note
            FROM signal_reviews
            WHERE program_id = ?
            ORDER BY reviewed_at ASC, rowid ASC
            """,
            (program_id,),
        ).fetchall()

    return tuple(
        SignalReviewDecision(
            signal_id=str(row["signal_id"]),
            decision=cast(Literal["approved", "dismissed", "deferred"], str(row["decision"])),
            reviewed_at=_parse_datetime(row["reviewed_at"]),
            reviewed_by=str(row["reviewed_by"]),
            note=_optional_string(row["note"]),
        )
        for row in rows
    )


@contextmanager
def _connect_program_db(program_id: str, *, programs_root: Path) -> Iterator[sqlite3.Connection]:
    """INV-AF-13 (WO-2 item 2): routed through ``open_program_db()`` instead of
    a hand-rolled ``sqlite3.connect`` + hardcoded ``PRAGMA journal_mode=WAL``,
    so this store picks WAL/DELETE by filesystem like every other store
    (network drives were previously forced into WAL, which is unsafe there).
    ``durability="strict"`` preserves this store's prior always-``synchronous
    =FULL`` behavior (the old code never set ``PRAGMA synchronous``, leaving
    SQLite's library default of FULL in effect).
    """
    path = get_program_sqlite_store_path(program_id, programs_root=programs_root)
    with open_program_db(path, durability="strict") as connection:
        _ensure_schema(connection)
        yield connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS signals (
            program_id TEXT NOT NULL,
            signal_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            source TEXT NOT NULL,
            workstream_id TEXT,
            entity_refs_json TEXT NOT NULL,
            text TEXT NOT NULL,
            raw_ref TEXT,
            confidence TEXT NOT NULL,
            metadata_json TEXT,
            thread_id TEXT,
            review_policy TEXT,
            PRIMARY KEY (program_id, signal_id)
        );
        CREATE INDEX IF NOT EXISTS idx_signals_program_timestamp ON signals(program_id, timestamp, signal_id);

        CREATE TABLE IF NOT EXISTS signal_reviews (
            program_id TEXT NOT NULL,
            signal_id TEXT NOT NULL,
            decision TEXT NOT NULL,
            reviewed_at TEXT NOT NULL,
            reviewed_by TEXT NOT NULL,
            note TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_signal_reviews_program_signal ON signal_reviews(program_id, signal_id, reviewed_at);

        CREATE TABLE IF NOT EXISTS signal_usage_markers (
            program_id TEXT NOT NULL,
            signal_id TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            edition_id TEXT NOT NULL,
            manifest_id TEXT NOT NULL,
            used_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_signal_usage_markers_program_signal
            ON signal_usage_markers(program_id, signal_id, used_at);

        CREATE TABLE IF NOT EXISTS signal_threads (
            program_id TEXT NOT NULL,
            signal_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            linked_at TEXT NOT NULL,
            linked_by TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_signal_threads_program_signal ON signal_threads(program_id, signal_id, linked_at);

        CREATE TABLE IF NOT EXISTS trajectory_points (
            program_id TEXT NOT NULL,
            work_item_id INTEGER NOT NULL,
            point_date TEXT NOT NULL,
            state TEXT NOT NULL,
            assigned_to TEXT,
            target_date TEXT,
            risk_level TEXT,
            area_path TEXT NOT NULL,
            tags_json TEXT NOT NULL,
            risk_assessment TEXT,
            risk_assessment_comment TEXT,
            inserted_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_trajectory_points_program_item_date
            ON trajectory_points(program_id, work_item_id, point_date, inserted_at);
        """
    )
    signal_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(signals)").fetchall()}
    if "review_policy" not in signal_columns:
        connection.execute("ALTER TABLE signals ADD COLUMN review_policy TEXT")


def _trajectory_point_from_row(row: sqlite3.Row) -> TrajectoryPoint:
    raw_risk_level = row["risk_level"]
    return TrajectoryPoint(
        date=date.fromisoformat(str(row["point_date"])),
        state=str(row["state"]),
        assigned_to=_optional_string(row["assigned_to"]),
        target_date=date.fromisoformat(str(row["target_date"])) if row["target_date"] is not None else None,
        risk_level=RiskLevel.from_string(str(raw_risk_level)) if raw_risk_level is not None else None,
        area_path=str(row["area_path"]),
        tags=tuple(str(value) for value in json.loads(str(row["tags_json"]))),
        risk_assessment=_optional_string(row["risk_assessment"]),
        risk_assessment_comment=_optional_string(row["risk_assessment_comment"]),
    )


def _trajectory_has_material_change(previous: TrajectoryPoint, current: TrajectoryPoint) -> bool:
    return (
        previous.state != current.state
        or previous.assigned_to != current.assigned_to
        or previous.target_date != current.target_date
        or previous.risk_level != current.risk_level
        or previous.risk_assessment != current.risk_assessment
        or previous.risk_assessment_comment != current.risk_assessment_comment
        or previous.area_path != current.area_path
        or previous.tags != current.tags
    )


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _encode_datetime(value: datetime) -> str:
    return _normalize_datetime(value).isoformat()


def _parse_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Expected ISO datetime string, found {type(value).__name__}.")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_json_object(value: Any) -> dict[str, str | int | float | bool | None] | None:
    if value is None:
        return None
    payload = json.loads(str(value))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object, found {type(payload).__name__}.")
    return {str(key): item for key, item in payload.items()}