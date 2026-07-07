from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class SourceRefKind(str, Enum):
    MEETING_SERIES = "meeting_series"
    TEAMS_CHAT = "teams_chat"
    TEAMS_CHANNEL = "teams_channel"
    EMAIL_THREAD = "email_thread"


class SourceIntentStatus(str, Enum):
    DECLARED = "declared"
    SEARCHING = "searching"
    NO_CANDIDATES = "no_candidates"
    CANDIDATE_FOUND = "candidate_found"
    AMBIGUOUS = "ambiguous"
    RESOLVED = "resolved"
    ACTIVE = "active"
    STALE = "stale"
    AUTH_BLOCKED = "auth_blocked"
    OUT_OF_IDENTITY_SCOPE = "out_of_identity_scope"
    SUPPRESSED = "suppressed"
    SUPERSEDED = "superseded"
    RETIRED = "retired"


class SourceCandidateStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class DiscoveryAttemptOutcome(str, Enum):
    CANDIDATES_FOUND = "candidates_found"
    NO_CANDIDATES = "no_candidates"
    AMBIGUOUS = "ambiguous"
    AUTH_BLOCKED = "auth_blocked"
    OUT_OF_IDENTITY_SCOPE = "out_of_identity_scope"
    BUDGET_EXCEEDED = "budget_exceeded"
    ERROR = "error"
    STALE_PLAN = "stale_plan"
    REJECTED_CANDIDATE_SUPPRESSED = "rejected_candidate_suppressed"


@dataclass(frozen=True, slots=True)
class SourceIntent:
    intent_id: str
    program_id: str
    workstream_id: str
    ref_kind: SourceRefKind
    display_name: str
    normalized_name: str
    status: SourceIntentStatus
    created_at: datetime
    updated_at: datetime
    updated_by: str | None = None
    decision_version: int = 0


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    candidate_id: str
    program_id: str
    channel: str
    provider_instance_id: str
    ref_id: str
    ref_kind: SourceRefKind
    display_name: str | None
    confidence: float
    source_provider: str
    status: SourceCandidateStatus
    evidence_json: str
    first_discovered_at: datetime
    last_seen_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_reason: str | None = None
    old_status: str | None = None
    decision_version: int = 0


@dataclass(frozen=True, slots=True)
class CandidateIntentMatch:
    candidate_id: str
    intent_id: str
    match_confidence: float


@dataclass(frozen=True, slots=True)
class DiscoveryAttempt:
    attempt_id: str
    program_id: str
    intent_id: str | None
    workstream_id: str | None
    channel: str | None
    provider_instance_id: str
    ref_kind: SourceRefKind | None
    source_provider: str
    query_hash: str
    config_hash: str
    autonomous_run_id: str | None
    outcome: DiscoveryAttemptOutcome
    reason: str | None
    result_count: int
    duration_ms: int | None
    attempted_at: datetime
    expires_at: datetime | None = None


def normalize_intent_display_name(value: str) -> str:
    return value.strip().lower()


def build_source_intent_id(
    *,
    program_id: str,
    workstream_id: str,
    ref_kind: SourceRefKind,
    display_name: str,
) -> str:
    normalized_name = normalize_intent_display_name(display_name)
    return hashlib.sha1(
        f"{program_id}|{workstream_id}|{ref_kind.value}|{normalized_name}".encode("utf-8")
    ).hexdigest()


def build_source_candidate_id(
    *,
    program_id: str,
    channel: str,
    provider_instance_id: str,
    ref_kind: SourceRefKind,
    ref_id: str,
) -> str:
    return hashlib.sha1(
        f"{program_id}|{channel}|{provider_instance_id}|{ref_kind.value}|{ref_id}".encode("utf-8")
    ).hexdigest()


def build_discovery_attempt_id(
    *,
    program_id: str,
    intent_id: str | None,
    source_provider: str,
    query_hash: str,
    attempted_at: datetime,
) -> str:
    return hashlib.sha1(
        f"{program_id}|{intent_id or ''}|{source_provider}|{query_hash}|{attempted_at.isoformat()}".encode("utf-8")
    ).hexdigest()
