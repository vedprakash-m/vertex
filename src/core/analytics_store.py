from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import logging
from src.core.jsonl_utils import append_jsonl_line, parse_jsonl_line, quarantine_and_rewrite_jsonl
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterator
import uuid

from src.core._db import open_program_db
from src.core.context_snapshot_store import ContextSnapshot, load_context_snapshot
from src.core.action_tracker import read_action_log
from src.core.claim_tracker import read_claim_log
from src.core.decision_register import load_decisions
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.journal import get_program_journal_archive_dir
from src.core.models import Confidence, Snapshot
from src.core.models_v2 import ActionItem, ActionStatusUpdate, ClaimEntry, ClaimStatusUpdate, Contradiction, ContradictionPacket, DataSourceType, DecisionStatus, ResolvedContradiction, VitalityArchiveEntry
from src.core.program_fact_store import load_program_facts, project_decision_entries
from src.core.program_paths import get_program_analytics_store_path
from src.core.snapshot_store import read_snapshot
from src.core.vitality_reporting import parse_vitality_archive_entry


_log = logging.getLogger(__name__)

_DIRTY_MARKER = ".analytics_dirty"
_DEFAULT_LOAD_DECISIONS = load_decisions

# High-risk append-only audit file — grows with every autonomy event.
# Rotated at 10 MB (spec §11.3 Phase 5 / D-23) to bound on-disk footprint.
_AUTONOMY_AUDIT_MAX_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class AutonomyAuditRecord:
    program_id: str
    action_id: str
    level: str
    author_alias: str
    subject_alias: str | None
    evidence_refs: tuple[str, ...]
    policy_rule: str | None
    accepted: bool
    applied_at: datetime
    action_type: str | None = None
    blast_radius: str | None = None
    rollback_mechanism: str | None = None
    prior_acceptance_rate: float | None = None


@dataclass(frozen=True, slots=True)
class AnalyticsRebuildArtifacts:
    program_id: str
    database_path: Path
    confirmed_risks: int
    confirmed_claims: int
    confirmed_decisions: int
    program_fact_decisions: int
    context_snapshot_decisions: int
    raw_decision_fallbacks: int
    confirmed_vitality: int
    confirmed_actions: int
    dri_response_log: int
    autonomy_audit: int

    def __post_init__(self) -> None:
        if self.confirmed_decisions != (
            self.program_fact_decisions + self.context_snapshot_decisions + self.raw_decision_fallbacks
        ):
            raise ValueError("confirmed_decisions must equal the sum of decision source tiers")

    @property
    def low_fidelity_decisions(self) -> int:
        return self.context_snapshot_decisions + self.raw_decision_fallbacks


@dataclass(frozen=True, slots=True)
class ConfirmedDecisionProjection:
    decision_id: str
    issue_number: int
    text: str
    owner: str
    status: str
    resolved_at: str | None
    source_tier: str

    def as_db_row(self, *, edition_id: str, confirmed_at: datetime) -> tuple[str, int, str, str, str, str, str | None, str, str]:
        return (
            self.decision_id,
            self.issue_number,
            edition_id,
            self.text,
            self.owner,
            self.status,
            self.resolved_at,
            _ensure_utc(confirmed_at).isoformat(),
            self.source_tier,
        )


@dataclass(frozen=True, slots=True)
class AutonomyAuditArchiveArtifacts:
    program_id: str
    before_date: date
    archived_count: int
    remaining_count: int
    archive_paths: tuple[Path, ...]


def get_program_autonomy_audit_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "journal" / "autonomy_audit.jsonl"


def get_program_autonomy_audit_archive_path(
    program_id: str,
    year: int,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    return get_program_journal_archive_dir(program_id, programs_root) / f"autonomy_audit_archive_{year}.jsonl"


def get_program_analytics_dirty_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "_feedback" / _DIRTY_MARKER


def replace_contradiction_state(
    program_id: str,
    packets: tuple[ContradictionPacket, ...],
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    with _connect_analytics_db(program_id, programs_root=programs_root) as connection:
        connection.execute("DELETE FROM contradiction_state")
        connection.executemany(
            """
            INSERT OR REPLACE INTO contradiction_state (
                work_item_id,
                workstream_id,
                field,
                source_a,
                source_b,
                summary,
                contradiction_confidence,
                evidence_refs_json,
                packet_confidence,
                recommended_source,
                recommended_confidence,
                recommended_rationale,
                recommended_evidence_refs_json,
                generated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    packet.work_item_id,
                    packet.workstream_id,
                    contradiction.field,
                    contradiction.source_a,
                    contradiction.source_b,
                    contradiction.summary,
                    contradiction.confidence.value,
                    json.dumps(list(contradiction.evidence_refs), sort_keys=True),
                    packet.confidence.value,
                    None if packet.recommended_resolution is None else packet.recommended_resolution.winning_source.value,
                    None if packet.recommended_resolution is None else packet.recommended_resolution.confidence.value,
                    None if packet.recommended_resolution is None else packet.recommended_resolution.rationale,
                    json.dumps(
                        [] if packet.recommended_resolution is None else list(packet.recommended_resolution.evidence_refs),
                        sort_keys=True,
                    ),
                    _ensure_utc(packet.generated_at).isoformat(),
                )
                for packet in packets
                for contradiction in packet.contradictions
            ],
        )


def load_contradiction_state(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[ContradictionPacket, ...]:
    path = get_program_analytics_store_path(program_id, programs_root=programs_root)
    if not path.exists():
        return ()
    with _connect_analytics_db(program_id, programs_root=programs_root) as connection:
        rows = connection.execute(
            """
            SELECT
                work_item_id,
                workstream_id,
                field,
                source_a,
                source_b,
                summary,
                contradiction_confidence,
                evidence_refs_json,
                packet_confidence,
                recommended_source,
                recommended_confidence,
                recommended_rationale,
                recommended_evidence_refs_json,
                generated_at
            FROM contradiction_state
            ORDER BY COALESCE(workstream_id, ''), work_item_id, field, source_a, source_b
            """
        ).fetchall()

    grouped: dict[tuple[int, str | None, str], list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(
            (int(row["work_item_id"]), row["workstream_id"], str(row["generated_at"])),
            [],
        ).append(row)

    packets: list[ContradictionPacket] = []
    for (work_item_id, workstream_id, generated_at), packet_rows in grouped.items():
        contradictions = tuple(
            Contradiction(
                field=str(row["field"]),
                source_a=str(row["source_a"]),
                source_b=str(row["source_b"]),
                summary=str(row["summary"]),
                confidence=_parse_confidence(row["contradiction_confidence"]),
                evidence_refs=tuple(str(value) for value in json.loads(str(row["evidence_refs_json"]))),
            )
            for row in packet_rows
        )
        first = packet_rows[0]
        recommended_source = first["recommended_source"]
        recommended_resolution = None
        if recommended_source is not None:
            recommended_resolution = ResolvedContradiction(
                winning_source=DataSourceType.from_string(str(recommended_source)),
                confidence=_parse_confidence(first["recommended_confidence"]),
                rationale=str(first["recommended_rationale"] or ""),
                evidence_refs=tuple(str(value) for value in json.loads(str(first["recommended_evidence_refs_json"] or "[]"))),
            )
        packets.append(
            ContradictionPacket(
                work_item_id=work_item_id,
                workstream_id=workstream_id,
                contradictions=contradictions,
                confidence=_parse_confidence(first["packet_confidence"]),
                recommended_resolution=recommended_resolution,
                generated_at=_parse_datetime(generated_at),
            )
        )
    return tuple(packets)


def mark_analytics_dirty(program_id: str, *, reason: str, programs_root: Path = PROGRAMS_ROOT) -> Path:
    path = get_program_analytics_dirty_path(program_id, programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "program_id": program_id,
        "dirty_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
    }
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)
    return path


def clear_analytics_dirty(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> None:
    path = get_program_analytics_dirty_path(program_id, programs_root=programs_root)
    if path.exists():
        path.unlink()


def append_autonomy_audit_record(
    record: AutonomyAuditRecord,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    path = get_program_autonomy_audit_path(record.program_id, programs_root=programs_root)
    append_jsonl_line(
        path,
        json.dumps(_autonomy_record_to_payload(record), sort_keys=True) + "\n",
        max_bytes=_AUTONOMY_AUDIT_MAX_BYTES,
    )
    try:
        with _connect_analytics_db(record.program_id, programs_root=programs_root) as connection:
            _insert_autonomy_audit_records(connection, (record,))
    except Exception:
        mark_analytics_dirty(record.program_id, reason="autonomy audit projection failed", programs_root=programs_root)
        raise
    clear_analytics_dirty(record.program_id, programs_root=programs_root)
    return path


def compute_prior_acceptance_rate(
    program_id: str,
    *,
    action_type: str,
    programs_root: Path = PROGRAMS_ROOT,
) -> float | None:
    normalized_action_type = action_type.strip()
    if not normalized_action_type:
        return None
    matching = [
        record
        for record in _load_autonomy_audit_records(program_id, programs_root=programs_root)
        if record.action_type == normalized_action_type
    ]
    if not matching:
        return None
    accepted = sum(1 for record in matching if record.accepted)
    return accepted / len(matching)


def load_autonomy_audit_records(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[AutonomyAuditRecord, ...]:
    return _load_autonomy_audit_records(program_id, programs_root=programs_root)


def archive_autonomy_audit_records(
    program_id: str,
    *,
    before: date,
    programs_root: Path = PROGRAMS_ROOT,
) -> AutonomyAuditArchiveArtifacts:
    records = _load_autonomy_audit_records(program_id, programs_root=programs_root)
    archived_records = tuple(record for record in records if _ensure_utc(record.applied_at).date() < before)
    remaining_records = tuple(record for record in records if _ensure_utc(record.applied_at).date() >= before)
    if not archived_records:
        return AutonomyAuditArchiveArtifacts(
            program_id=program_id,
            before_date=before,
            archived_count=0,
            remaining_count=len(remaining_records),
            archive_paths=(),
        )

    archive_paths: list[Path] = []
    grouped_records: dict[int, list[AutonomyAuditRecord]] = {}
    for record in archived_records:
        grouped_records.setdefault(_ensure_utc(record.applied_at).year, []).append(record)

    for year, year_records in sorted(grouped_records.items()):
        archive_path = get_program_autonomy_audit_archive_path(program_id, year, programs_root=programs_root)
        existing_records = _load_autonomy_audit_records_from_path(archive_path, program_id=program_id)
        _replace_jsonl_payloads(
            archive_path,
            [_autonomy_record_to_payload(record) for record in (*existing_records, *tuple(year_records))],
        )
        archive_paths.append(archive_path)

    _replace_jsonl_payloads(
        get_program_autonomy_audit_path(program_id, programs_root=programs_root),
        [_autonomy_record_to_payload(record) for record in remaining_records],
    )
    rebuild_program_analytics(program_id=program_id, programs_root=programs_root)
    return AutonomyAuditArchiveArtifacts(
        program_id=program_id,
        before_date=before,
        archived_count=len(archived_records),
        remaining_count=len(remaining_records),
        archive_paths=tuple(archive_paths),
    )


def project_confirmed_issue(
    *,
    program_id: str,
    edition_id: str,
    snapshot: Snapshot,
    confirmed_at: datetime,
    vitality_entry: VitalityArchiveEntry | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    confirmed_at_utc = _ensure_utc(confirmed_at)
    claim_rows = _claim_rows_as_of(
        program_id=program_id,
        edition_id=edition_id,
        issue_number=snapshot.issue_number,
        as_of=confirmed_at_utc,
        programs_root=programs_root,
    )
    decision_rows = _decision_rows_as_of(
        program_id=program_id,
        edition_id=edition_id,
        issue_number=snapshot.issue_number,
        as_of=confirmed_at_utc,
        programs_root=programs_root,
    )
    action_rows = _action_rows_as_of(
        program_id=program_id,
        issue_number=snapshot.issue_number,
        as_of=confirmed_at_utc,
        programs_root=programs_root,
    )
    vitality_rows = _vitality_rows(
        issue_number=snapshot.issue_number,
        confirmed_at=confirmed_at_utc,
        vitality_entry=vitality_entry,
    )

    with _connect_analytics_db(program_id, programs_root=programs_root) as connection:
        connection.execute("DELETE FROM confirmed_risks WHERE edition = ? AND issue_number = ?", (edition_id, snapshot.issue_number))
        connection.execute("DELETE FROM confirmed_claims WHERE issue_number = ?", (snapshot.issue_number,))
        connection.execute("DELETE FROM confirmed_decisions WHERE issue_number = ?", (snapshot.issue_number,))
        connection.execute("DELETE FROM confirmed_vitality WHERE issue_number = ?", (snapshot.issue_number,))
        connection.execute("DELETE FROM confirmed_actions WHERE issue_number = ?", (snapshot.issue_number,))

        connection.executemany(
            """
            INSERT OR REPLACE INTO confirmed_risks (
                edition, issue_number, dimension, risk, confirmed_at, prev_risk
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    edition_id,
                    snapshot.issue_number,
                    dimension.name,
                    dimension.risk.value,
                    confirmed_at_utc.isoformat(),
                    dimension.prior_risk.value if dimension.prior_risk is not None else None,
                )
                for dimension in snapshot.scorecards
            ],
        )
        connection.executemany(
            """
            INSERT OR REPLACE INTO confirmed_claims (
                claim_id, issue_number, workstream_id, due_date, status, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            claim_rows,
        )
        connection.executemany(
            """
            INSERT OR REPLACE INTO confirmed_decisions (
                decision_id, issue_number, edition, text, owner, status, resolved_at, confirmed_at, source_tier
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                row.as_db_row(edition_id=edition_id, confirmed_at=confirmed_at_utc)
                for row in decision_rows
            ],
        )
        connection.executemany(
            """
            INSERT OR REPLACE INTO confirmed_vitality (
                issue_number, workstream_id, composite_score, freshness_grade, confirmed_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            vitality_rows,
        )
        connection.executemany(
            """
            INSERT OR REPLACE INTO confirmed_actions (
                action_id, issue_number, owner, due_date, status
            ) VALUES (?, ?, ?, ?, ?)
            """,
            action_rows,
        )

    clear_analytics_dirty(program_id, programs_root=programs_root)


def rebuild_program_analytics(
    *,
    program_id: str,
    programs_root: Path = PROGRAMS_ROOT,
) -> AnalyticsRebuildArtifacts:
    confirmed_states = _load_confirmed_issue_states(program_id, programs_root=programs_root)
    database_path = get_program_analytics_store_path(program_id, programs_root=programs_root)

    confirmed_risks = 0
    confirmed_claims = 0
    confirmed_decisions = 0
    program_fact_decisions = 0
    context_snapshot_decisions = 0
    raw_decision_fallbacks = 0
    confirmed_vitality = 0
    confirmed_actions = 0
    autonomy_records = _load_autonomy_audit_records(program_id, programs_root=programs_root)

    with _connect_analytics_db(program_id, programs_root=programs_root) as connection:
        for table_name in (
            "confirmed_risks",
            "confirmed_claims",
            "confirmed_decisions",
            "confirmed_vitality",
            "confirmed_actions",
            "dri_response_log",
            "autonomy_audit",
            "contradiction_state",
        ):
            connection.execute(f"DELETE FROM {table_name}")

        for state in confirmed_states:
            connection.executemany(
                """
                INSERT OR REPLACE INTO confirmed_risks (
                    edition, issue_number, dimension, risk, confirmed_at, prev_risk
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        state["edition_id"],
                        state["issue_number"],
                        dimension.name,
                        dimension.risk.value,
                        state["confirmed_at"].isoformat(),
                        dimension.prior_risk.value if dimension.prior_risk is not None else None,
                    )
                    for dimension in state["snapshot"].scorecards
                ],
            )
            confirmed_risks += len(state["snapshot"].scorecards)

            claim_rows = _claim_rows_as_of(
                program_id=program_id,
                edition_id=str(state["edition_id"]),
                issue_number=int(state["issue_number"]),
                as_of=state["confirmed_at"],
                programs_root=programs_root,
            )
            connection.executemany(
                """
                INSERT OR REPLACE INTO confirmed_claims (
                    claim_id, issue_number, workstream_id, due_date, status, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                claim_rows,
            )
            confirmed_claims += len(claim_rows)

            decision_rows = _decision_rows_as_of(
                program_id=program_id,
                edition_id=str(state["edition_id"]),
                issue_number=int(state["issue_number"]),
                as_of=state["confirmed_at"],
                programs_root=programs_root,
            )
            connection.executemany(
                """
                INSERT OR REPLACE INTO confirmed_decisions (
                    decision_id, issue_number, edition, text, owner, status, resolved_at, confirmed_at, source_tier
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    row.as_db_row(
                        edition_id=str(state["edition_id"]),
                        confirmed_at=state["confirmed_at"],
                    )
                    for row in decision_rows
                ],
            )
            confirmed_decisions += len(decision_rows)

            vitality_rows = _vitality_rows(
                issue_number=int(state["issue_number"]),
                confirmed_at=state["confirmed_at"],
                vitality_entry=state["vitality_entry"],
            )
            connection.executemany(
                """
                INSERT OR REPLACE INTO confirmed_vitality (
                    issue_number, workstream_id, composite_score, freshness_grade, confirmed_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                vitality_rows,
            )
            confirmed_vitality += len(vitality_rows)

            action_rows = _action_rows_as_of(
                program_id=program_id,
                issue_number=int(state["issue_number"]),
                as_of=state["confirmed_at"],
                programs_root=programs_root,
            )
            connection.executemany(
                """
                INSERT OR REPLACE INTO confirmed_actions (
                    action_id, issue_number, owner, due_date, status
                ) VALUES (?, ?, ?, ?, ?)
                """,
                action_rows,
            )
            confirmed_actions += len(action_rows)

        _insert_autonomy_audit_records(connection, autonomy_records)
        (
            program_fact_decisions,
            context_snapshot_decisions,
            raw_decision_fallbacks,
        ) = _load_confirmed_decision_source_counts(connection)

    clear_analytics_dirty(program_id, programs_root=programs_root)
    return AnalyticsRebuildArtifacts(
        program_id=program_id,
        database_path=database_path,
        confirmed_risks=confirmed_risks,
        confirmed_claims=confirmed_claims,
        confirmed_decisions=confirmed_decisions,
        program_fact_decisions=program_fact_decisions,
        context_snapshot_decisions=context_snapshot_decisions,
        raw_decision_fallbacks=raw_decision_fallbacks,
        confirmed_vitality=confirmed_vitality,
        confirmed_actions=confirmed_actions,
        dri_response_log=0,
        autonomy_audit=len(autonomy_records),
    )


@contextmanager
def _connect_analytics_db(program_id: str, *, programs_root: Path) -> Iterator[sqlite3.Connection]:
    path = get_program_analytics_store_path(program_id, programs_root=programs_root)
    with open_program_db(path) as connection:
        _ensure_schema(connection)
        yield connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS confirmed_risks (
            edition TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            dimension TEXT NOT NULL,
            risk TEXT NOT NULL,
            confirmed_at TEXT NOT NULL,
            prev_risk TEXT,
            PRIMARY KEY (edition, issue_number, dimension)
        );

        CREATE TABLE IF NOT EXISTS confirmed_claims (
            claim_id TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            workstream_id TEXT,
            due_date TEXT,
            status TEXT NOT NULL,
            resolved_at TEXT,
            PRIMARY KEY (claim_id, issue_number)
        );

        CREATE TABLE IF NOT EXISTS confirmed_decisions (
            decision_id TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            edition TEXT,
            text TEXT NOT NULL,
            owner TEXT NOT NULL,
            status TEXT NOT NULL,
            resolved_at TEXT,
            confirmed_at TEXT,
            source_tier TEXT,
            PRIMARY KEY (decision_id, issue_number)
        );

        CREATE TABLE IF NOT EXISTS confirmed_vitality (
            issue_number INTEGER NOT NULL,
            workstream_id TEXT NOT NULL,
            composite_score INTEGER NOT NULL,
            freshness_grade TEXT,
            confirmed_at TEXT NOT NULL,
            PRIMARY KEY (issue_number, workstream_id)
        );

        CREATE TABLE IF NOT EXISTS confirmed_actions (
            action_id TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            owner TEXT NOT NULL,
            due_date TEXT,
            status TEXT NOT NULL,
            PRIMARY KEY (action_id, issue_number)
        );

        CREATE TABLE IF NOT EXISTS dri_response_log (
            event_id TEXT NOT NULL,
            dri_alias TEXT NOT NULL,
            notification_type TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            responded_at TEXT,
            latency_days REAL,
            PRIMARY KEY (dri_alias, notification_type, sent_at)
        );

        CREATE TABLE IF NOT EXISTS gate_failure_log (
            gate_id TEXT NOT NULL,
            cause TEXT NOT NULL,
            edition_id TEXT NOT NULL,
            program_id TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            occurrence_count INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (gate_id, cause, edition_id, program_id)
        );

        CREATE TABLE IF NOT EXISTS override_streak_log (
            program_id TEXT NOT NULL,
            dimension TEXT NOT NULL,
            edition_id TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            original_value TEXT,
            override_value TEXT NOT NULL,
            overridden_at TEXT NOT NULL,
            streak_count INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (program_id, dimension, edition_id, issue_number)
        );

        CREATE TABLE IF NOT EXISTS autonomy_audit (
            action_id TEXT NOT NULL PRIMARY KEY,
            level TEXT NOT NULL,
            author_alias TEXT NOT NULL,
            subject_alias TEXT,
            action_type TEXT,
            evidence_refs_json TEXT NOT NULL,
            policy_rule TEXT,
            accepted INTEGER NOT NULL,
            applied_at TEXT NOT NULL,
            blast_radius TEXT,
            rollback_mechanism TEXT,
            prior_acceptance_rate REAL
        );

        CREATE TABLE IF NOT EXISTS contradiction_state (
            work_item_id INTEGER NOT NULL,
            workstream_id TEXT,
            field TEXT NOT NULL,
            source_a TEXT NOT NULL,
            source_b TEXT NOT NULL,
            summary TEXT NOT NULL,
            contradiction_confidence TEXT NOT NULL,
            evidence_refs_json TEXT NOT NULL,
            packet_confidence TEXT NOT NULL,
            recommended_source TEXT,
            recommended_confidence TEXT,
            recommended_rationale TEXT,
            recommended_evidence_refs_json TEXT,
            generated_at TEXT NOT NULL,
            PRIMARY KEY (work_item_id, source_a, source_b, field)
        );
        """
    )
    _ensure_autonomy_audit_columns(connection)
    _ensure_confirmed_decisions_columns(connection)
    _ensure_dri_response_log_columns(connection)


def _ensure_autonomy_audit_columns(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(autonomy_audit)").fetchall()
    }
    if "subject_alias" not in columns:
        connection.execute("ALTER TABLE autonomy_audit ADD COLUMN subject_alias TEXT")
    if "action_type" not in columns:
        connection.execute("ALTER TABLE autonomy_audit ADD COLUMN action_type TEXT")
    if "blast_radius" not in columns:
        connection.execute("ALTER TABLE autonomy_audit ADD COLUMN blast_radius TEXT")
    if "rollback_mechanism" not in columns:
        connection.execute("ALTER TABLE autonomy_audit ADD COLUMN rollback_mechanism TEXT")
    if "prior_acceptance_rate" not in columns:
        connection.execute("ALTER TABLE autonomy_audit ADD COLUMN prior_acceptance_rate REAL")


def _ensure_confirmed_decisions_columns(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(confirmed_decisions)").fetchall()
    }
    if "edition" not in columns:
        connection.execute("ALTER TABLE confirmed_decisions ADD COLUMN edition TEXT")
    if "confirmed_at" not in columns:
        connection.execute("ALTER TABLE confirmed_decisions ADD COLUMN confirmed_at TEXT")
    if "source_tier" not in columns:
        connection.execute("ALTER TABLE confirmed_decisions ADD COLUMN source_tier TEXT")


def _ensure_dri_response_log_columns(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(dri_response_log)").fetchall()
    }
    if "event_id" not in columns:
        connection.execute("ALTER TABLE dri_response_log ADD COLUMN event_id TEXT")


# ── FR-SG-41: dri_response_log writers ──────────────────────────────────────


def record_nudge_sent(
    program_id: str,
    *,
    dri_alias: str,
    notification_type: str,
    sent_at: datetime,
    programs_root: Path = PROGRAMS_ROOT,
) -> str:
    """Insert a sent-nudge row and return the stable event_id UUID."""
    event_id = str(uuid.uuid4())
    sent_iso = _ensure_utc(sent_at).isoformat()
    with _connect_analytics_db(program_id, programs_root=programs_root) as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO dri_response_log
                (event_id, dri_alias, notification_type, sent_at)
            VALUES (?, ?, ?, ?)
            """,
            (event_id, dri_alias.strip().lower(), notification_type, sent_iso),
        )
    return event_id


def record_nudge_response(
    program_id: str,
    *,
    event_id: str,
    responded_at: datetime,
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    """Record a DRI response against a previously sent nudge identified by event_id."""
    responded_iso = _ensure_utc(responded_at).isoformat()
    with _connect_analytics_db(program_id, programs_root=programs_root) as connection:
        row = connection.execute(
            "SELECT sent_at FROM dri_response_log WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None:
            return
        sent_at = _parse_datetime(str(row["sent_at"]))
        latency = (responded_at - sent_at).total_seconds() / 86400.0
        connection.execute(
            """
            UPDATE dri_response_log
               SET responded_at = ?, latency_days = ?
             WHERE event_id = ? AND responded_at IS NULL
            """,
            (responded_iso, round(latency, 3), event_id),
        )


# ── FR-SG-40: gate_failure_log writers ──────────────────────────────────────


@dataclass(frozen=True, slots=True)
class GateFailureRecord:
    gate_id: str
    cause: str
    edition_id: str
    program_id: str
    recorded_at: datetime
    occurrence_count: int


def record_gate_failure(
    program_id: str,
    *,
    gate_id: str,
    cause: str,
    edition_id: str,
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    """Upsert a gate failure keyed by (gate_id, cause, edition_id, program_id).

    Uses INSERT OR IGNORE + UPDATE so each distinct (gate+cause+edition) pair
    is counted once even if the gate is evaluated on multiple runs.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    with _connect_analytics_db(program_id, programs_root=programs_root) as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO gate_failure_log
                (gate_id, cause, edition_id, program_id, recorded_at, occurrence_count)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (gate_id, cause, edition_id, program_id, now_iso),
        )
        connection.execute(
            """
            UPDATE gate_failure_log
               SET occurrence_count = occurrence_count + 1,
                   recorded_at      = ?
             WHERE gate_id = ? AND cause = ? AND edition_id = ? AND program_id = ?
               AND recorded_at != ?
            """,
            (now_iso, gate_id, cause, edition_id, program_id, now_iso),
        )


def get_recurring_gate_failures(
    program_id: str,
    *,
    min_occurrences: int = 3,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[GateFailureRecord, ...]:
    """Return gate failures that have recurred at least *min_occurrences* times."""
    with _connect_analytics_db(program_id, programs_root=programs_root) as connection:
        rows = connection.execute(
            """
            SELECT gate_id, cause, edition_id, program_id, recorded_at,
                   SUM(occurrence_count) AS total
              FROM gate_failure_log
             WHERE program_id = ?
          GROUP BY gate_id, cause
            HAVING total >= ?
          ORDER BY total DESC
            """,
            (program_id, min_occurrences),
        ).fetchall()
    return tuple(
        GateFailureRecord(
            gate_id=str(row["gate_id"]),
            cause=str(row["cause"]),
            edition_id=str(row["edition_id"]),
            program_id=program_id,
            recorded_at=_parse_datetime(str(row["recorded_at"])),
            occurrence_count=int(row["total"]),
        )
        for row in rows
    )


def record_gate_failures_from_report(
    program_id: str,
    *,
    gate_report: Any,
    edition_id: str,
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    """Record all failing gates in *gate_report* to gate_failure_log (FR-SG-40).

    *gate_report* must expose ``failing_results`` as an iterable of objects
    with ``gate_id`` and ``message`` attributes (compatible with QualityGateReport).
    Failures are keyed by (gate_id, cause, edition_id, program_id) so multi-run
    evaluations of the same edition do not inflate counts.
    """
    if not program_id or not edition_id:
        return
    for result in gate_report.failing_results:
        cause = (result.message or "unknown")[:256]
        record_gate_failure(
            program_id,
            gate_id=str(result.gate_id),
            cause=cause,
            edition_id=edition_id,
            programs_root=programs_root,
        )


# ── FR-SG-37: override streak tracking ──────────────────────────────────────

@dataclass(frozen=True, slots=True)
class OverrideStreakEntry:
    program_id: str
    dimension: str
    edition_id: str
    issue_number: int
    original_value: str | None
    override_value: str
    streak_count: int


def record_override(
    program_id: str,
    *,
    dimension: str,
    edition_id: str,
    issue_number: int,
    original_value: str | None,
    override_value: str,
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    """Record a single dimension override and maintain streak_count (FR-SG-37)."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect_analytics_db(program_id, programs_root=programs_root) as conn:
        prev = conn.execute(
            "SELECT streak_count FROM override_streak_log WHERE program_id=? AND dimension=? ORDER BY issue_number DESC LIMIT 1",
            (program_id, dimension),
        ).fetchone()
        streak = (int(prev["streak_count"]) + 1) if prev else 1
        conn.execute(
            """
            INSERT OR REPLACE INTO override_streak_log
                (program_id, dimension, edition_id, issue_number, original_value, override_value, overridden_at, streak_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (program_id, dimension, edition_id, issue_number, original_value, override_value, now, streak),
        )
        conn.commit()


def get_override_streaks(
    program_id: str,
    *,
    min_streak: int = 3,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[OverrideStreakEntry, ...]:
    """Return dimensions overridden *min_streak* or more consecutive times (FR-SG-37)."""
    with _connect_analytics_db(program_id, programs_root=programs_root) as conn:
        rows = conn.execute(
            """
            SELECT program_id, dimension, edition_id, issue_number, original_value, override_value, streak_count
            FROM override_streak_log
            WHERE program_id=? AND streak_count >= ?
            ORDER BY streak_count DESC, dimension
            """,
            (program_id, min_streak),
        ).fetchall()
    return tuple(
        OverrideStreakEntry(
            program_id=str(row["program_id"]),
            dimension=str(row["dimension"]),
            edition_id=str(row["edition_id"]),
            issue_number=int(row["issue_number"]),
            original_value=row["original_value"],
            override_value=str(row["override_value"]),
            streak_count=int(row["streak_count"]),
        )
        for row in rows
    )


def _load_confirmed_decision_source_counts(connection: sqlite3.Connection) -> tuple[int, int, int]:
    counts = {
        str(row["source_tier"]): int(row["count"])
        for row in connection.execute(
            """
            SELECT source_tier, COUNT(*) AS count
            FROM confirmed_decisions
            GROUP BY source_tier
            """
        ).fetchall()
        if row["source_tier"] is not None
    }
    return (
        counts.get("program_facts", 0),
        sum(count for tier, count in counts.items() if tier.startswith("context_snapshot")),
        counts.get("raw_decisions", 0),
    )


def _replace_jsonl_payloads(path: Path, payloads: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not payloads:
        if path.exists():
            path.unlink()
        return
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        for payload in payloads:
            handle.write(json.dumps(payload, sort_keys=True))
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def _autonomy_record_to_payload(record: AutonomyAuditRecord) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "action_id": record.action_id,
        "level": record.level,
        "author_alias": record.author_alias,
        "evidence_refs": list(record.evidence_refs),
        "policy_rule": record.policy_rule,
        "accepted": record.accepted,
        "applied_at": _ensure_utc(record.applied_at).isoformat(),
    }
    if record.subject_alias is not None:
        payload["subject_alias"] = record.subject_alias
    if record.action_type is not None:
        payload["action_type"] = record.action_type
    if record.blast_radius is not None:
        payload["blast_radius"] = record.blast_radius
    if record.rollback_mechanism is not None:
        payload["rollback_mechanism"] = record.rollback_mechanism
    if record.prior_acceptance_rate is not None:
        payload["prior_acceptance_rate"] = record.prior_acceptance_rate
    return payload


def _insert_autonomy_audit_records(
    connection: sqlite3.Connection,
    records: tuple[AutonomyAuditRecord, ...],
) -> None:
    connection.executemany(
        """
        INSERT OR REPLACE INTO autonomy_audit (
            action_id,
            level,
            author_alias,
            subject_alias,
            action_type,
            evidence_refs_json,
            policy_rule,
            accepted,
            applied_at,
            blast_radius,
            rollback_mechanism,
            prior_acceptance_rate
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                record.action_id,
                record.level,
                record.author_alias,
                record.subject_alias,
                record.action_type,
                json.dumps(list(record.evidence_refs), sort_keys=True),
                record.policy_rule,
                1 if record.accepted else 0,
                _ensure_utc(record.applied_at).isoformat(),
                record.blast_radius,
                record.rollback_mechanism,
                record.prior_acceptance_rate,
            )
            for record in records
        ],
    )


def _load_autonomy_audit_records(program_id: str, *, programs_root: Path) -> tuple[AutonomyAuditRecord, ...]:
    path = get_program_autonomy_audit_path(program_id, programs_root=programs_root)
    return _load_autonomy_audit_records_from_path(path, program_id=program_id)


def _parse_autonomy_audit_row(payload: Any, *, program_id: str) -> AutonomyAuditRecord:
    if not isinstance(payload, dict):
        raise ValueError("autonomy_audit row must be a JSON object")
    return AutonomyAuditRecord(
        program_id=program_id,
        action_id=str(payload["action_id"]),
        level=str(payload["level"]),
        author_alias=str(payload["author_alias"]),
        subject_alias=_optional_string(payload.get("subject_alias")),
        action_type=_optional_string(payload.get("action_type")),
        evidence_refs=tuple(str(value) for value in payload.get("evidence_refs") or ()),
        policy_rule=_optional_string(payload.get("policy_rule")),
        accepted=bool(payload.get("accepted")),
        applied_at=_parse_datetime(payload["applied_at"]),
        blast_radius=_optional_string(payload.get("blast_radius")),
        rollback_mechanism=_optional_string(payload.get("rollback_mechanism")),
        prior_acceptance_rate=_optional_float(payload.get("prior_acceptance_rate")),
    )


def _load_autonomy_audit_records_from_path(path: Path, *, program_id: str) -> tuple[AutonomyAuditRecord, ...]:
    """ADF-W1.8: never crash on a foreign/mixed-schema row.

    A row that isn't shaped like an ``AutonomyAuditRecord`` (wrong event
    type, missing required field, bad type) is quarantined via the shared
    ``jsonl_utils`` full-file-backup pattern rather than raising -- the
    original file (valid rows included) is preserved verbatim under
    ``journal/quarantine/``, and ``path`` is rewritten with only the rows
    that parsed. One warning is logged per read that finds bad rows, not
    per row.
    """
    if not path.exists():
        return ()
    raw_lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    records: list[AutonomyAuditRecord] = []
    valid_lines: list[str] = []
    quarantined_count = 0
    for raw_line in raw_lines:
        if not raw_line.strip():
            valid_lines.append(raw_line)
            continue
        try:
            payload = parse_jsonl_line(raw_line)
            record = _parse_autonomy_audit_row(payload, program_id=program_id)
        except Exception:
            quarantined_count += 1
            continue
        records.append(record)
        valid_lines.append(raw_line)

    if quarantined_count:
        quarantine_and_rewrite_jsonl(path, valid_lines)
        _log.warning(
            "Quarantined %d unrecognized row(s) from %s (mixed-schema autonomy_audit.jsonl, ADF-W1.8); "
            "original file preserved under journal/quarantine/",
            quarantined_count,
            path,
        )

    return tuple(records)


def _parse_confidence(value: object) -> Confidence:
    return Confidence.from_string(str(value or Confidence.NONE.value))


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _claim_rows_as_of(
    *,
    program_id: str,
    edition_id: str,
    issue_number: int,
    as_of: datetime,
    programs_root: Path,
) -> list[tuple[str, int, str | None, str | None, str, str | None]]:
    latest_statuses: dict[str, ClaimStatusUpdate] = {}
    claims: list[ClaimEntry] = []
    for entry in read_claim_log(program_id, programs_root=programs_root):
        if isinstance(entry, ClaimStatusUpdate):
            if _ensure_utc(entry.updated_at) <= as_of:
                latest_statuses[entry.claim_id] = entry
            continue
        if not isinstance(entry, ClaimEntry):
            continue
        if entry.edition_id != edition_id or entry.issue_number != issue_number:
            continue
        if entry.claim_date > as_of.date():
            continue
        claims.append(entry)

    rows: list[tuple[str, int, str | None, str | None, str, str | None]] = []
    for claim in claims:
        update = latest_statuses.get(claim.id)
        status = update.new_status if update is not None else claim.status
        rows.append(
            (
                claim.id,
                issue_number,
                claim.workstream_id,
                claim.due_date.isoformat() if claim.due_date is not None else None,
                status,
                update.updated_at.isoformat() if update is not None and status != "open" else None,
            )
        )
    return rows


def _decision_rows_as_of(
    *,
    program_id: str,
    edition_id: str,
    issue_number: int,
    as_of: datetime,
    programs_root: Path,
) -> list[ConfirmedDecisionProjection]:
    rows: list[ConfirmedDecisionProjection] = []
    for decision in _load_decisions_as_of(program_id, as_of=as_of, programs_root=programs_root):
        if decision.decision_date > as_of.date():
            continue
        rows.append(
            ConfirmedDecisionProjection(
                decision_id=decision.id,
                issue_number=issue_number,
                text=decision.title,
                owner=decision.decided_by,
                status=decision.status.value,
                resolved_at=decision.decision_date.isoformat() if decision.status is not DecisionStatus.PROPOSED else None,
                source_tier="program_facts",
            )
        )
    if rows:
        return rows

    snapshot = load_context_snapshot(
        program_id,
        edition_id,
        issue_number,
        archive_root=programs_root,
    )
    if snapshot is not None:
        context_rows = _decision_rows_from_context_snapshot(snapshot, issue_number=issue_number, as_of=as_of)
        if context_rows:
            return context_rows

    for decision in load_decisions(program_id, programs_root=programs_root):
        if decision.decision_date > as_of.date():  # type: ignore[operator]
            continue
        rows.append(
            ConfirmedDecisionProjection(
                decision_id=decision.id,
                issue_number=issue_number,
                text=decision.title,
                owner=decision.decided_by,  # type: ignore[arg-type]
                status=decision.status.value,
                resolved_at=decision.decision_date.isoformat() if decision.status is not DecisionStatus.PROPOSED else None,  # type: ignore[union-attr]
                source_tier="raw_decisions",
            )
        )
    return rows


def _load_decisions_as_of(
    program_id: str,
    *,
    as_of: datetime,
    programs_root: Path,
):
    if load_decisions is not _DEFAULT_LOAD_DECISIONS:
        return load_decisions(program_id, programs_root=programs_root)
    decisions = project_decision_entries(
        load_program_facts(
            program_id,
            as_of=as_of,
            programs_root=programs_root,
        )
    )
    return decisions


def _decision_rows_from_context_snapshot(
    snapshot: ContextSnapshot,
    *,
    issue_number: int,
    as_of: datetime,
) -> list[ConfirmedDecisionProjection]:
    if not _context_snapshot_supports_decision_history(snapshot):
        return []

    rows: list[ConfirmedDecisionProjection] = []
    for decision in snapshot.decisions:
        if not isinstance(decision, dict):
            raise ValueError("Context snapshot decision payload must be a mapping.")
        decision_id = str(decision.get("id") or "").strip()
        title = str(decision.get("title") or "").strip()
        decided_by = str(decision.get("decided_by") or "").strip()
        status_raw = str(decision.get("status") or "").strip()
        decision_date_raw = str(decision.get("decision_date") or "").strip()
        if not decision_id or not title or not decided_by or not status_raw or not decision_date_raw:
            raise ValueError("Context snapshot decision payload is missing required historical fields.")
        status = DecisionStatus.from_string(status_raw)
        decision_date = date.fromisoformat(decision_date_raw)
        if decision_date > as_of.date():
            continue
        rows.append(
            ConfirmedDecisionProjection(
                decision_id=decision_id,
                issue_number=issue_number,
                text=title,
                owner=decided_by,
                status=status.value,
                resolved_at=decision_date.isoformat() if status is not DecisionStatus.PROPOSED else None,
                source_tier="context_snapshot_1_1",
            )
        )
    return rows


def _context_snapshot_supports_decision_history(snapshot: ContextSnapshot) -> bool:
    try:
        major_raw, minor_raw = str(snapshot.schema_version).split(".", 1)
        return (int(major_raw), int(minor_raw)) >= (1, 1)
    except (TypeError, ValueError):
        return False


def _action_rows_as_of(
    *,
    program_id: str,
    issue_number: int,
    as_of: datetime,
    programs_root: Path,
) -> list[tuple[str, int, str, str | None, str]]:
    base_entries: dict[str, ActionItem] = {}
    latest_updates: dict[str, ActionStatusUpdate] = {}
    for entry in read_action_log(program_id, programs_root=programs_root):
        if isinstance(entry, ActionStatusUpdate):
            if _ensure_utc(entry.updated_at) <= as_of:
                latest_updates[entry.action_id] = entry
            continue
        if _ensure_utc(entry.created_at) <= as_of and entry.id not in base_entries:
            base_entries[entry.id] = entry

    rows: list[tuple[str, int, str, str | None, str]] = []
    for action in base_entries.values():
        update = latest_updates.get(action.id)
        status = update.new_status.value if update is not None else action.status.value
        rows.append(
            (
                action.id,
                issue_number,
                action.owner_alias,
                action.due_date.isoformat() if action.due_date is not None else None,
                status,
            )
        )
    return rows


def _vitality_rows(
    *,
    issue_number: int,
    confirmed_at: datetime,
    vitality_entry: VitalityArchiveEntry | None,
) -> list[tuple[int, str, int, str | None, str]]:
    if vitality_entry is None:
        return []
    return [
        (
            issue_number,
            workstream_id,
            workstream.score,
            None,
            confirmed_at.isoformat(),
        )
        for workstream_id, workstream in sorted(vitality_entry.per_workstream.items())
    ]


def _load_confirmed_issue_states(program_id: str, *, programs_root: Path) -> list[dict[str, Any]]:
    archive_root = programs_root / program_id / "archive"
    if not archive_root.exists():
        return []

    vitality_by_issue: dict[tuple[str, int], VitalityArchiveEntry] = {}
    for edition_dir in archive_root.iterdir():
        if not edition_dir.is_dir():
            continue
        vitality_path = edition_dir / "vitality.json"
        if not vitality_path.exists():
            continue
        payload = json.loads(vitality_path.read_text(encoding="utf-8"))
        raw_entries = payload.get("entries", []) if isinstance(payload, dict) else payload
        if not isinstance(raw_entries, list):
            continue
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, dict):
                continue
            entry = parse_vitality_archive_entry(raw_entry)
            if entry is None:
                continue
            vitality_by_issue[(edition_dir.name, entry.issue_number)] = entry

    states: list[dict[str, Any]] = []
    for edition_dir in sorted(child for child in archive_root.iterdir() if child.is_dir()):
        index_path = edition_dir / "index.json"
        if not index_path.exists():
            continue
        payload = json.loads(index_path.read_text(encoding="utf-8"))
        raw_issues = payload.get("issues", []) if isinstance(payload, dict) else []
        if not isinstance(raw_issues, list):
            continue
        for raw_issue in raw_issues:
            if not isinstance(raw_issue, dict):
                continue
            if str(raw_issue.get("kind", "confirmed")) != "confirmed":
                continue
            try:
                issue_number = int(raw_issue["issue_number"])
            except (KeyError, TypeError, ValueError):
                continue
            snapshot_path_value = raw_issue.get("snapshot_path")
            snapshot_path = Path(snapshot_path_value) if isinstance(snapshot_path_value, str) and snapshot_path_value.strip() else edition_dir / "snapshots" / f"issue_{issue_number:03d}.snapshot.json"
            if not snapshot_path.exists():
                continue
            snapshot = read_snapshot(snapshot_path)
            manifest_path_value = raw_issue.get("manifest_path")
            manifest_path = Path(manifest_path_value) if isinstance(manifest_path_value, str) and manifest_path_value.strip() else None
            confirmed_at = _confirmed_at_from_manifest(manifest_path, fallback=snapshot.generated_at)
            states.append(
                {
                    "edition_id": edition_dir.name,
                    "issue_number": issue_number,
                    "snapshot": snapshot,
                    "confirmed_at": confirmed_at,
                    "vitality_entry": vitality_by_issue.get((edition_dir.name, issue_number)),
                }
            )
    states.sort(key=lambda entry: (entry["confirmed_at"], entry["edition_id"], entry["issue_number"]))
    return states


def _confirmed_at_from_manifest(manifest_path: Path | None, *, fallback: datetime) -> datetime:
    if manifest_path is None or not manifest_path.exists():
        return _ensure_utc(fallback)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _ensure_utc(fallback)
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    if not isinstance(metadata, dict):
        return _ensure_utc(fallback)
    confirmed_at = metadata.get("confirmed_at")
    if not isinstance(confirmed_at, str) or not confirmed_at.strip():
        return _ensure_utc(fallback)
    try:
        return _parse_datetime(confirmed_at)
    except ValueError:
        return _ensure_utc(fallback)


def _parse_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Expected datetime string, found {type(value).__name__}.")
    return _ensure_utc(datetime.fromisoformat(value))


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None