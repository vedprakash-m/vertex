"""Intent-status computation helpers for the integration command (D-13).

Extracted from the ``integration.py`` god module (§28.4 strangler fig): pure
intent-status derivation over a SQLite connection (match confidence, status
recompute, conditional status update, registration existence, accepted-candidate
collection). All dependencies (connection, candidate/channel stores) are passed
by argument — no global state, not monkeypatched. ``integration.py`` re-imports
these so its attribute surface and call sites are unchanged.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from src.core.channel_registry_store import ChannelRegistryStore
from src.core.discovery_intent import SourceCandidate, SourceCandidateStatus, SourceIntent, SourceIntentStatus
from src.core.source_candidate_store import SourceCandidateStore


def _intent_match_confidence(
    candidate_store: SourceCandidateStore,
    candidate_id: str,
    intent_id: str,
) -> float:
    for match in candidate_store.get_intent_matches(candidate_id):
        if match.intent_id == intent_id:
            return match.match_confidence
    return 1.0


def _intent_match_confidence_with_conn(
    conn: sqlite3.Connection,
    candidate_store: SourceCandidateStore,
    candidate_id: str,
    intent_id: str,
) -> float:
    for match in candidate_store.get_intent_matches_with_conn(conn, candidate_id):
        if match.intent_id == intent_id:
            return match.match_confidence
    return 1.0


def _recompute_intent_status_with_conn(
    conn: sqlite3.Connection,
    candidate_store: SourceCandidateStore,
    intent_id: str,
    *,
    as_of: datetime,
) -> SourceIntentStatus:
    candidates = candidate_store.list_candidates_for_intent_with_conn(conn, intent_id)
    attempts = candidate_store.get_attempts_with_conn(conn, intent_id, exclude_expired=False)
    if any(candidate.status == SourceCandidateStatus.ACCEPTED for candidate in candidates):
        return SourceIntentStatus.RESOLVED
    pending_candidates = [candidate for candidate in candidates if candidate.status == SourceCandidateStatus.PENDING]
    high_confidence_pending = [candidate for candidate in pending_candidates if candidate.confidence >= 0.75]
    if len(high_confidence_pending) > 1:
        return SourceIntentStatus.AMBIGUOUS
    if pending_candidates:
        return SourceIntentStatus.CANDIDATE_FOUND
    if any(attempt.outcome.name == "OUT_OF_IDENTITY_SCOPE" for attempt in attempts):
        return SourceIntentStatus.OUT_OF_IDENTITY_SCOPE
    if any(attempt.outcome.name == "AUTH_BLOCKED" for attempt in attempts):
        return SourceIntentStatus.AUTH_BLOCKED
    if any(attempt.expires_at is None or attempt.expires_at >= as_of for attempt in attempts):
        return SourceIntentStatus.SEARCHING
    return SourceIntentStatus.DECLARED


def _update_intent_status_if_needed_with_conn(
    conn: sqlite3.Connection,
    candidate_store: SourceCandidateStore,
    intent: SourceIntent,
    *,
    status: SourceIntentStatus,
    updated_by: str,
) -> SourceIntent:
    if intent.status == status:
        return intent
    return candidate_store.update_intent_status_with_conn(
        conn,
        intent.intent_id,
        status=status,
        updated_by=updated_by,
        expected_decision_version=intent.decision_version,
    )


def _registration_exists_with_conn(
    conn: sqlite3.Connection,
    channel_store: ChannelRegistryStore,
    candidate: SourceCandidate,
) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM registrations
        WHERE channel = ? AND program_id = ? AND provider_instance_id = ? AND ref_id = ? AND ref_kind = ?
        LIMIT 1
        """,
        (
            candidate.channel,
            channel_store.program_id,
            candidate.provider_instance_id,
            candidate.ref_id,
            candidate.ref_kind.value,
        ),
    ).fetchone()
    return row is not None


def _accepted_candidates_for_intent_with_conn(
    conn: sqlite3.Connection,
    candidate_store: SourceCandidateStore,
    intent_id: str,
) -> tuple[SourceCandidate, ...]:
    return tuple(
        candidate
        for candidate in candidate_store.list_candidates_for_intent_with_conn(conn, intent_id)
        if candidate.status == SourceCandidateStatus.ACCEPTED
    )
