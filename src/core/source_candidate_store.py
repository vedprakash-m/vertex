from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.core._db import open_program_db
from src.core.channel_registry_store import ChannelRegistryStore
from src.core.discovery_intent import (
    CandidateIntentMatch,
    DiscoveryAttempt,
    DiscoveryAttemptOutcome,
    SourceCandidate,
    SourceCandidateStatus,
    SourceIntent,
    SourceIntentStatus,
    SourceRefKind,
    build_source_intent_id,
    normalize_intent_display_name,
)
from src.core.m365_registry_store import M365RegistryArtifact
from src.core.models_v2 import Workstream


CANDIDATE_REJECTION_SUPPRESSION_DAYS = 60
_DISCOVERY_TABLE_NAMES = (
    "source_intents",
    "source_candidates",
    "candidate_intent_matches",
    "discovery_attempts",
)


class SourceCandidateStore:
    def __init__(self, db_path: Path, program_id: str, *, ensure_schema: bool = True) -> None:
        self.db_path = db_path
        self.program_id = program_id
        if ensure_schema:
            ChannelRegistryStore(db_path, program_id)

    def list_intents(
        self,
        *,
        workstream_id: str | None = None,
        ref_kind: SourceRefKind | None = None,
        statuses: tuple[SourceIntentStatus, ...] | None = None,
    ) -> tuple[SourceIntent, ...]:
        where = ["program_id = ?"]
        params: list[Any] = [self.program_id]
        if workstream_id is not None:
            where.append("workstream_id = ?")
            params.append(workstream_id)
        if ref_kind is not None:
            where.append("ref_kind = ?")
            params.append(ref_kind.value)
        if statuses:
            where.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            params.extend(status.value for status in statuses)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM source_intents WHERE {' AND '.join(where)} ORDER BY workstream_id, ref_kind, normalized_name",
                tuple(params),
            ).fetchall()
        return tuple(_intent_from_row(row) for row in rows)

    def get_intent(self, intent_id: str) -> SourceIntent | None:
        with self._connect() as conn:
            return self.get_intent_with_conn(conn, intent_id)

    def get_intent_with_conn(
        self,
        conn: sqlite3.Connection,
        intent_id: str,
    ) -> SourceIntent | None:
        row = conn.execute(
            "SELECT * FROM source_intents WHERE program_id = ? AND intent_id = ?",
            (self.program_id, intent_id),
        ).fetchone()
        return _intent_from_row(row) if row is not None else None

    def get_intent_by_name(
        self,
        *,
        workstream_id: str,
        ref_kind: SourceRefKind,
        display_name: str,
    ) -> SourceIntent | None:
        normalized_name = normalize_intent_display_name(display_name)
        with self._connect() as conn:
            return self.get_intent_by_name_with_conn(
                conn,
                workstream_id=workstream_id,
                ref_kind=ref_kind,
                display_name=display_name,
            )

    def has_discovery_schema(self) -> bool:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table' AND name IN ({",".join("?" for _ in _DISCOVERY_TABLE_NAMES)})
                    """,
                    _DISCOVERY_TABLE_NAMES,
                ).fetchall()
        except sqlite3.Error:
            return False
        return {str(row["name"]) for row in rows} == set(_DISCOVERY_TABLE_NAMES)

    def get_intent_by_name_with_conn(
        self,
        conn: sqlite3.Connection,
        *,
        workstream_id: str,
        ref_kind: SourceRefKind,
        display_name: str,
    ) -> SourceIntent | None:
        normalized_name = normalize_intent_display_name(display_name)
        row = conn.execute(
            """
            SELECT * FROM source_intents
            WHERE program_id = ? AND workstream_id = ? AND ref_kind = ? AND normalized_name = ?
            """,
            (self.program_id, workstream_id, ref_kind.value, normalized_name),
        ).fetchone()
        return _intent_from_row(row) if row is not None else None

    def upsert_intent(self, intent: SourceIntent, *, preserve_lifecycle: bool = False) -> None:
        with self._connect() as conn:
            self.upsert_intent_with_conn(conn, intent, preserve_lifecycle=preserve_lifecycle)

    def upsert_intent_with_conn(
        self,
        conn: sqlite3.Connection,
        intent: SourceIntent,
        *,
        preserve_lifecycle: bool = False,
    ) -> None:
        if preserve_lifecycle:
            conn.execute(
                """
                INSERT INTO source_intents(
                    intent_id, program_id, workstream_id, ref_kind, display_name, normalized_name,
                    status, created_at, updated_at, updated_by, decision_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(intent_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    normalized_name = excluded.normalized_name,
                    updated_at = excluded.updated_at
                """,
                (
                    intent.intent_id,
                    self.program_id,
                    intent.workstream_id,
                    intent.ref_kind.value,
                    intent.display_name,
                    intent.normalized_name,
                    intent.status.value,
                    _format_datetime(intent.created_at),
                    _format_datetime(intent.updated_at),
                    intent.updated_by,
                    intent.decision_version,
                ),
            )
            return
        conn.execute(
            """
            INSERT INTO source_intents(
                intent_id, program_id, workstream_id, ref_kind, display_name, normalized_name,
                status, created_at, updated_at, updated_by, decision_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(intent_id) DO UPDATE SET
                display_name = excluded.display_name,
                normalized_name = excluded.normalized_name,
                status = excluded.status,
                updated_at = excluded.updated_at,
                updated_by = excluded.updated_by,
                decision_version = excluded.decision_version
            """,
            (
                intent.intent_id,
                self.program_id,
                intent.workstream_id,
                intent.ref_kind.value,
                intent.display_name,
                intent.normalized_name,
                intent.status.value,
                _format_datetime(intent.created_at),
                _format_datetime(intent.updated_at),
                intent.updated_by,
                intent.decision_version,
            ),
        )

    def update_intent_status(
        self,
        intent_id: str,
        *,
        status: SourceIntentStatus,
        updated_by: str,
        expected_decision_version: int | None = None,
    ) -> SourceIntent:
        with self._connect() as conn:
            return self.update_intent_status_with_conn(
                conn,
                intent_id,
                status=status,
                updated_by=updated_by,
                expected_decision_version=expected_decision_version,
            )

    def update_intent_status_with_conn(
        self,
        conn: sqlite3.Connection,
        intent_id: str,
        *,
        status: SourceIntentStatus,
        updated_by: str,
        expected_decision_version: int | None = None,
    ) -> SourceIntent:
        row = conn.execute(
            "SELECT * FROM source_intents WHERE program_id = ? AND intent_id = ?",
            (self.program_id, intent_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown source intent '{intent_id}'.")
        current = _intent_from_row(row)
        if expected_decision_version is not None and current.decision_version != expected_decision_version:
            raise ValueError(
                f"Intent '{intent_id}' has decision_version={current.decision_version}, expected {expected_decision_version}."
            )
        next_version = current.decision_version + 1
        updated_at = _current_utc()
        conn.execute(
            """
            UPDATE source_intents
            SET status = ?, updated_at = ?, updated_by = ?, decision_version = ?
            WHERE program_id = ? AND intent_id = ?
            """,
            (
                status.value,
                _format_datetime(updated_at),
                updated_by,
                next_version,
                self.program_id,
                intent_id,
            ),
        )
        return SourceIntent(
            intent_id=current.intent_id,
            program_id=current.program_id,
            workstream_id=current.workstream_id,
            ref_kind=current.ref_kind,
            display_name=current.display_name,
            normalized_name=current.normalized_name,
            status=status,
            created_at=current.created_at,
            updated_at=updated_at,
            updated_by=updated_by,
            decision_version=next_version,
        )

    def list_candidates(
        self,
        *,
        status: SourceCandidateStatus | None = None,
        workstream_id: str | None = None,
        ref_kind: SourceRefKind | None = None,
        requires_decision: bool = False,
    ) -> tuple[SourceCandidate, ...]:
        where = ["c.program_id = ?"]
        params: list[Any] = [self.program_id]
        joins = ""
        if workstream_id is not None:
            joins = " JOIN candidate_intent_matches m ON m.candidate_id = c.candidate_id JOIN source_intents i ON i.intent_id = m.intent_id "
            where.append("i.workstream_id = ?")
            params.append(workstream_id)
        if status is not None:
            where.append("c.status = ?")
            params.append(status.value)
        if ref_kind is not None:
            where.append("c.ref_kind = ?")
            params.append(ref_kind.value)
        if requires_decision:
            where.append("c.status = ?")
            params.append(SourceCandidateStatus.PENDING.value)
        query = (
            "SELECT DISTINCT c.* FROM source_candidates c"
            + joins
            + f" WHERE {' AND '.join(where)} ORDER BY c.status, c.last_seen_at DESC, c.display_name"
        )
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return tuple(_candidate_from_row(row) for row in rows)

    def get_candidate(self, candidate_id: str) -> SourceCandidate | None:
        with self._connect() as conn:
            return self.get_candidate_with_conn(conn, candidate_id)

    def get_candidate_with_conn(
        self,
        conn: sqlite3.Connection,
        candidate_id: str,
    ) -> SourceCandidate | None:
        row = conn.execute(
            "SELECT * FROM source_candidates WHERE program_id = ? AND candidate_id = ?",
            (self.program_id, candidate_id),
        ).fetchone()
        return _candidate_from_row(row) if row is not None else None

    def get_candidate_by_ref(
        self,
        *,
        ref_id: str,
        ref_kind: SourceRefKind,
        channel: str | None = None,
    ) -> SourceCandidate | None:
        where = ["program_id = ?", "ref_id = ?", "ref_kind = ?"]
        params: list[Any] = [self.program_id, ref_id, ref_kind.value]
        if channel is not None:
            where.append("channel = ?")
            params.append(channel)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM source_candidates WHERE {' AND '.join(where)} ORDER BY last_seen_at DESC LIMIT 1",
                tuple(params),
            ).fetchone()
        return _candidate_from_row(row) if row is not None else None

    def get_recent_rejected_candidate_by_ref(
        self,
        *,
        ref_id: str,
        ref_kind: SourceRefKind,
        as_of: datetime,
        channel: str | None = None,
    ) -> SourceCandidate | None:
        candidate = self.get_candidate_by_ref(ref_id=ref_id, ref_kind=ref_kind, channel=channel)
        if candidate is None or candidate.status != SourceCandidateStatus.REJECTED:
            return None
        if candidate.decided_at is None:
            return None
        if candidate.decided_at < as_of - timedelta(days=CANDIDATE_REJECTION_SUPPRESSION_DAYS):
            return None
        return candidate

    def upsert_candidate(
        self,
        candidate: SourceCandidate,
        *,
        pii_prescrubbed: bool,
    ) -> None:
        with self._connect() as conn:
            self.upsert_candidate_with_conn(conn, candidate, pii_prescrubbed=pii_prescrubbed)

    def upsert_candidate_with_conn(
        self,
        conn: sqlite3.Connection,
        candidate: SourceCandidate,
        *,
        pii_prescrubbed: bool,
    ) -> None:
        if not pii_prescrubbed:
            raise ValueError("SourceCandidateStore requires pii_prescrubbed=True before persisting candidate evidence.")
        conn.execute(
            """
            INSERT INTO source_candidates(
                candidate_id, program_id, channel, provider_instance_id, ref_id, ref_kind, display_name,
                confidence, source_provider, status, evidence_json, first_discovered_at, last_seen_at,
                decided_at, decided_by, decision_reason, old_status, decision_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(candidate_id) DO UPDATE SET
                display_name = excluded.display_name,
                confidence = excluded.confidence,
                source_provider = excluded.source_provider,
                evidence_json = excluded.evidence_json,
                last_seen_at = excluded.last_seen_at
            WHERE source_candidates.program_id = excluded.program_id
              AND source_candidates.decision_version = 0
            """,
            (
                candidate.candidate_id,
                self.program_id,
                candidate.channel,
                candidate.provider_instance_id,
                candidate.ref_id,
                candidate.ref_kind.value,
                candidate.display_name,
                candidate.confidence,
                candidate.source_provider,
                candidate.status.value,
                candidate.evidence_json,
                _format_datetime(candidate.first_discovered_at),
                _format_datetime(candidate.last_seen_at),
                _format_datetime(candidate.decided_at),
                candidate.decided_by,
                candidate.decision_reason,
                candidate.old_status,
                candidate.decision_version,
            ),
        )

    def update_candidate_status(
        self,
        candidate_id: str,
        *,
        status: SourceCandidateStatus,
        decided_by: str,
        decision_reason: str | None = None,
        expected_decision_version: int | None = None,
    ) -> SourceCandidate:
        with self._connect() as conn:
            return self.update_candidate_status_with_conn(
                conn,
                candidate_id,
                status=status,
                decided_by=decided_by,
                decision_reason=decision_reason,
                expected_decision_version=expected_decision_version,
            )

    def update_candidate_status_with_conn(
        self,
        conn: sqlite3.Connection,
        candidate_id: str,
        *,
        status: SourceCandidateStatus,
        decided_by: str,
        decision_reason: str | None = None,
        expected_decision_version: int | None = None,
    ) -> SourceCandidate:
        row = conn.execute(
            "SELECT * FROM source_candidates WHERE program_id = ? AND candidate_id = ?",
            (self.program_id, candidate_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown source candidate '{candidate_id}'.")
        current = _candidate_from_row(row)
        if expected_decision_version is not None and current.decision_version != expected_decision_version:
            raise ValueError(
                f"Candidate '{candidate_id}' has decision_version={current.decision_version}, expected {expected_decision_version}."
            )
        decided_at = _current_utc()
        next_version = current.decision_version + 1
        conn.execute(
            """
            UPDATE source_candidates
            SET status = ?, decided_at = ?, decided_by = ?, decision_reason = ?, old_status = ?, decision_version = ?
            WHERE program_id = ? AND candidate_id = ?
            """,
            (
                status.value,
                _format_datetime(decided_at),
                decided_by,
                decision_reason,
                current.status.value,
                next_version,
                self.program_id,
                candidate_id,
            ),
        )
        return SourceCandidate(
            candidate_id=current.candidate_id,
            program_id=current.program_id,
            channel=current.channel,
            provider_instance_id=current.provider_instance_id,
            ref_id=current.ref_id,
            ref_kind=current.ref_kind,
            display_name=current.display_name,
            confidence=current.confidence,
            source_provider=current.source_provider,
            status=status,
            evidence_json=current.evidence_json,
            first_discovered_at=current.first_discovered_at,
            last_seen_at=current.last_seen_at,
            decided_at=decided_at,
            decided_by=decided_by,
            decision_reason=decision_reason,
            old_status=current.status.value,
            decision_version=next_version,
        )

    def link_candidate_to_intent(self, candidate_id: str, intent_id: str, match_confidence: float) -> None:
        with self._connect() as conn:
            self.link_candidate_to_intent_with_conn(conn, candidate_id, intent_id, match_confidence)

    def link_candidate_to_intent_with_conn(
        self,
        conn: sqlite3.Connection,
        candidate_id: str,
        intent_id: str,
        match_confidence: float,
    ) -> None:
        conn.execute(
            """
            INSERT INTO candidate_intent_matches(candidate_id, intent_id, match_confidence)
            VALUES (?, ?, ?)
            ON CONFLICT(candidate_id, intent_id) DO UPDATE SET match_confidence = excluded.match_confidence
            """,
            (candidate_id, intent_id, match_confidence),
        )

    def unlink_candidate_from_intent(self, candidate_id: str, intent_id: str) -> None:
        with self._connect() as conn:
            self.unlink_candidate_from_intent_with_conn(conn, candidate_id, intent_id)

    def unlink_candidate_from_intent_with_conn(
        self,
        conn: sqlite3.Connection,
        candidate_id: str,
        intent_id: str,
    ) -> None:
        conn.execute(
            "DELETE FROM candidate_intent_matches WHERE candidate_id = ? AND intent_id = ?",
            (candidate_id, intent_id),
        )

    def get_intent_matches(self, candidate_id: str) -> tuple[CandidateIntentMatch, ...]:
        with self._connect() as conn:
            return self.get_intent_matches_with_conn(conn, candidate_id)

    def get_intent_matches_with_conn(
        self,
        conn: sqlite3.Connection,
        candidate_id: str,
    ) -> tuple[CandidateIntentMatch, ...]:
        rows = conn.execute(
            """
            SELECT candidate_id, intent_id, match_confidence
            FROM candidate_intent_matches
            WHERE candidate_id = ?
            ORDER BY match_confidence DESC, intent_id
            """,
            (candidate_id,),
        ).fetchall()
        return tuple(
            CandidateIntentMatch(
                candidate_id=str(row["candidate_id"]),
                intent_id=str(row["intent_id"]),
                match_confidence=float(row["match_confidence"]),
            )
            for row in rows
        )

    def list_candidates_for_intent(self, intent_id: str) -> tuple[SourceCandidate, ...]:
        with self._connect() as conn:
            return self.list_candidates_for_intent_with_conn(conn, intent_id)

    def list_candidates_for_intent_with_conn(
        self,
        conn: sqlite3.Connection,
        intent_id: str,
    ) -> tuple[SourceCandidate, ...]:
        rows = conn.execute(
            """
            SELECT c.*
            FROM source_candidates c
            JOIN candidate_intent_matches m ON m.candidate_id = c.candidate_id
            WHERE c.program_id = ? AND m.intent_id = ?
            ORDER BY c.status, c.confidence DESC, c.last_seen_at DESC
            """,
            (self.program_id, intent_id),
        ).fetchall()
        return tuple(_candidate_from_row(row) for row in rows)

    def record_attempt(self, attempt: DiscoveryAttempt) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO discovery_attempts(
                    attempt_id, program_id, intent_id, workstream_id, channel, provider_instance_id, ref_kind,
                    source_provider, query_hash, config_hash, autonomous_run_id, outcome, reason, result_count,
                    duration_ms, attempted_at, expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt.attempt_id,
                    self.program_id,
                    attempt.intent_id,
                    attempt.workstream_id,
                    attempt.channel,
                    attempt.provider_instance_id,
                    attempt.ref_kind.value if attempt.ref_kind is not None else None,
                    attempt.source_provider,
                    attempt.query_hash,
                    attempt.config_hash,
                    attempt.autonomous_run_id,
                    attempt.outcome.value,
                    attempt.reason,
                    attempt.result_count,
                    attempt.duration_ms,
                    _format_datetime(attempt.attempted_at),
                    _format_datetime(attempt.expires_at),
                ),
            )

    def get_attempts(
        self,
        intent_id: str,
        *,
        exclude_expired: bool = True,
    ) -> tuple[DiscoveryAttempt, ...]:
        with self._connect() as conn:
            return self.get_attempts_with_conn(conn, intent_id, exclude_expired=exclude_expired)

    def get_attempts_with_conn(
        self,
        conn: sqlite3.Connection,
        intent_id: str,
        *,
        exclude_expired: bool = True,
    ) -> tuple[DiscoveryAttempt, ...]:
        where = ["program_id = ?", "intent_id = ?"]
        params: list[Any] = [self.program_id, intent_id]
        if exclude_expired:
            where.append("(expires_at IS NULL OR expires_at >= ?)")
            params.append(_format_datetime(_current_utc()))
        rows = conn.execute(
            f"SELECT * FROM discovery_attempts WHERE {' AND '.join(where)} ORDER BY attempted_at DESC",
            tuple(params),
        ).fetchall()
        return tuple(_attempt_from_row(row) for row in rows)

    def derive_intent_state(self, intent_id: str, *, as_of: datetime) -> str:
        intent = self.get_intent(intent_id)
        if intent is None:
            raise ValueError(f"Unknown source intent '{intent_id}'.")
        if intent.status in {
            SourceIntentStatus.RETIRED,
            SourceIntentStatus.SUPPRESSED,
            SourceIntentStatus.SUPERSEDED,
            SourceIntentStatus.RESOLVED,
            SourceIntentStatus.ACTIVE,
            SourceIntentStatus.STALE,
            SourceIntentStatus.AUTH_BLOCKED,
            SourceIntentStatus.OUT_OF_IDENTITY_SCOPE,
        }:
            return intent.status.value

        candidates = self.list_candidates_for_intent(intent_id)
        attempts = self.get_attempts(intent_id, exclude_expired=False)
        candidate_statuses = {candidate.status for candidate in candidates}
        if SourceCandidateStatus.ACCEPTED in candidate_statuses:
            return SourceIntentStatus.RESOLVED.value
        pending_candidates = [candidate for candidate in candidates if candidate.status == SourceCandidateStatus.PENDING]
        high_confidence_pending = [candidate for candidate in pending_candidates if candidate.confidence >= 0.75]
        if len(high_confidence_pending) > 1:
            return SourceIntentStatus.AMBIGUOUS.value
        if pending_candidates:
            return SourceIntentStatus.CANDIDATE_FOUND.value
        if any(attempt.outcome == DiscoveryAttemptOutcome.OUT_OF_IDENTITY_SCOPE for attempt in attempts):
            return SourceIntentStatus.OUT_OF_IDENTITY_SCOPE.value
        if any(attempt.outcome == DiscoveryAttemptOutcome.AUTH_BLOCKED for attempt in attempts):
            return SourceIntentStatus.AUTH_BLOCKED.value
        if any(
            attempt.outcome in {
                DiscoveryAttemptOutcome.NO_CANDIDATES,
                DiscoveryAttemptOutcome.REJECTED_CANDIDATE_SUPPRESSED,
            }
            for attempt in attempts
        ):
            return SourceIntentStatus.NO_CANDIDATES.value
        non_expired_attempts = [
            attempt
            for attempt in attempts
            if attempt.expires_at is None or attempt.expires_at >= as_of
        ]
        if non_expired_attempts:
            return SourceIntentStatus.SEARCHING.value
        return intent.status.value

    def bootstrap_intents(
        self,
        *,
        workstreams: tuple[Workstream, ...],
        registry_artifacts: tuple[M365RegistryArtifact, ...],
        as_of: datetime,
    ) -> int:
        created_or_updated = 0
        for workstream in workstreams:
            signal_sources = workstream.signal_sources
            if signal_sources is None:
                continue
            for meeting in signal_sources.teams_meeting_series:
                created_or_updated += self._bootstrap_intent(
                    workstream_id=workstream.id,
                    ref_kind=SourceRefKind.MEETING_SERIES,
                    display_name=meeting.display_name,
                    updated_by="bootstrap_from_workstreams",
                    as_of=as_of,
                )
            for chat in signal_sources.teams_chats:
                created_or_updated += self._bootstrap_intent(
                    workstream_id=workstream.id,
                    ref_kind=SourceRefKind.TEAMS_CHAT,
                    display_name=chat.display_name,
                    updated_by="bootstrap_from_workstreams",
                    as_of=as_of,
                )
            for email_thread in signal_sources.email_threads:
                created_or_updated += self._bootstrap_intent(
                    workstream_id=workstream.id,
                    ref_kind=SourceRefKind.EMAIL_THREAD,
                    display_name=email_thread.display_name,
                    updated_by="bootstrap_from_workstreams",
                    as_of=as_of,
                )
            for subject_filter in signal_sources.email_subject_filters:
                created_or_updated += self._bootstrap_intent(
                    workstream_id=workstream.id,
                    ref_kind=SourceRefKind.EMAIL_THREAD,
                    display_name=subject_filter,
                    updated_by="bootstrap_from_workstreams",
                    as_of=as_of,
                )
        for artifact in registry_artifacts:
            missing_required_id = (
                artifact.artifact_type == SourceRefKind.MEETING_SERIES.value and artifact.series_id is None
            ) or (
                artifact.artifact_type != SourceRefKind.MEETING_SERIES.value and artifact.thread_id is None
            )
            if not artifact.pm_confirmed or not artifact.promoted_to_workstreams_yaml or not missing_required_id:
                continue
            ref_kind = _ref_kind_from_artifact_type(artifact.artifact_type)
            if ref_kind is None:
                continue
            created_or_updated += self._bootstrap_intent(
                workstream_id=artifact.inferred_workstream,
                ref_kind=ref_kind,
                display_name=artifact.display_name or artifact.artifact_id,
                updated_by="bootstrap_from_legacy",
                as_of=as_of,
            )
        return created_or_updated

    def _bootstrap_intent(
        self,
        *,
        workstream_id: str,
        ref_kind: SourceRefKind,
        display_name: str,
        updated_by: str,
        as_of: datetime,
    ) -> int:
        normalized_name = normalize_intent_display_name(display_name)
        if not normalized_name:
            return 0
        existing = self.get_intent_by_name(
            workstream_id=workstream_id,
            ref_kind=ref_kind,
            display_name=display_name,
        )
        if existing is not None:
            self.upsert_intent(
                SourceIntent(
                    intent_id=existing.intent_id,
                    program_id=self.program_id,
                    workstream_id=workstream_id,
                    ref_kind=ref_kind,
                    display_name=display_name,
                    normalized_name=normalized_name,
                    status=existing.status,
                    created_at=existing.created_at,
                    updated_at=as_of,
                    updated_by=existing.updated_by,
                    decision_version=existing.decision_version,
                ),
                preserve_lifecycle=True,
            )
            return 1
        intent = SourceIntent(
            intent_id=build_source_intent_id(
                program_id=self.program_id,
                workstream_id=workstream_id,
                ref_kind=ref_kind,
                display_name=display_name,
            ),
            program_id=self.program_id,
            workstream_id=workstream_id,
            ref_kind=ref_kind,
            display_name=display_name,
            normalized_name=normalized_name,
            status=SourceIntentStatus.DECLARED,
            created_at=as_of,
            updated_at=as_of,
            updated_by=updated_by,
            decision_version=0,
        )
        self.upsert_intent(intent)
        return 1

    def _connect(self) -> sqlite3.Connection:
        """INV-AF-13 (WO-2 item 10): same migration and atomicity caveat as
        channel_registry_store.py's ``_connect()`` (WO-2 item 9) -- routed
        through open_program_db() with its context-manager sugar bypassed,
        since callers use ``with self._connect() as conn:`` on the raw
        connection. Dropping the prior ``isolation_level=None`` autocommit
        mode means statements in one block now commit/roll back atomically
        together rather than independently; see
        test_source_candidate_store.py's
        ``test_connect_is_atomic_across_multiple_statements_in_one_block``.
        ``durability="strict"`` preserves the prior always-FULL synchronous
        default (never explicitly set before).
        """
        return open_program_db(self.db_path, durability="strict").connection


def _intent_from_row(row: sqlite3.Row) -> SourceIntent:
    return SourceIntent(
        intent_id=str(row["intent_id"]),
        program_id=str(row["program_id"]),
        workstream_id=str(row["workstream_id"]),
        ref_kind=SourceRefKind(str(row["ref_kind"])),
        display_name=str(row["display_name"]),
        normalized_name=str(row["normalized_name"]),
        status=SourceIntentStatus(str(row["status"])),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
        updated_by=str(row["updated_by"]) if row["updated_by"] is not None else None,
        decision_version=int(row["decision_version"] or 0),
    )


def _candidate_from_row(row: sqlite3.Row) -> SourceCandidate:
    return SourceCandidate(
        candidate_id=str(row["candidate_id"]),
        program_id=str(row["program_id"]),
        channel=str(row["channel"]),
        provider_instance_id=str(row["provider_instance_id"]),
        ref_id=str(row["ref_id"]),
        ref_kind=SourceRefKind(str(row["ref_kind"])),
        display_name=str(row["display_name"]) if row["display_name"] is not None else None,
        confidence=float(row["confidence"]),
        source_provider=str(row["source_provider"]),
        status=SourceCandidateStatus(str(row["status"])),
        evidence_json=str(row["evidence_json"]),
        first_discovered_at=_parse_datetime(row["first_discovered_at"]),
        last_seen_at=_parse_datetime(row["last_seen_at"]),
        decided_at=_parse_datetime_optional(row["decided_at"]),
        decided_by=str(row["decided_by"]) if row["decided_by"] is not None else None,
        decision_reason=str(row["decision_reason"]) if row["decision_reason"] is not None else None,
        old_status=str(row["old_status"]) if row["old_status"] is not None else None,
        decision_version=int(row["decision_version"] or 0),
    )


def _attempt_from_row(row: sqlite3.Row) -> DiscoveryAttempt:
    return DiscoveryAttempt(
        attempt_id=str(row["attempt_id"]),
        program_id=str(row["program_id"]),
        intent_id=str(row["intent_id"]) if row["intent_id"] is not None else None,
        workstream_id=str(row["workstream_id"]) if row["workstream_id"] is not None else None,
        channel=str(row["channel"]) if row["channel"] is not None else None,
        provider_instance_id=str(row["provider_instance_id"]),
        ref_kind=SourceRefKind(str(row["ref_kind"])) if row["ref_kind"] is not None else None,
        source_provider=str(row["source_provider"]),
        query_hash=str(row["query_hash"]),
        config_hash=str(row["config_hash"]),
        autonomous_run_id=str(row["autonomous_run_id"]) if row["autonomous_run_id"] is not None else None,
        outcome=DiscoveryAttemptOutcome(str(row["outcome"])),
        reason=str(row["reason"]) if row["reason"] is not None else None,
        result_count=int(row["result_count"] or 0),
        duration_ms=int(row["duration_ms"]) if row["duration_ms"] is not None else None,
        attempted_at=_parse_datetime(row["attempted_at"]),
        expires_at=_parse_datetime_optional(row["expires_at"]),
    )


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_datetime(value: Any) -> datetime:
    parsed = _parse_datetime_optional(value)
    if parsed is None:
        raise ValueError("Expected datetime value.")
    return parsed


def _parse_datetime_optional(value: Any) -> datetime | None:
    if value is None or not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _current_utc() -> datetime:
    return datetime.now(timezone.utc)


def _ref_kind_from_artifact_type(value: str) -> SourceRefKind | None:
    normalized = value.strip().lower()
    mapping = {
        "meeting_series": SourceRefKind.MEETING_SERIES,
        "teams_chat": SourceRefKind.TEAMS_CHAT,
        "teams_channel": SourceRefKind.TEAMS_CHANNEL,
        "email_thread": SourceRefKind.EMAIL_THREAD,
    }
    return mapping.get(normalized)


def candidate_evidence_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True)
