from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from src.core.jsonl_utils import (
    append_jsonl_line,
    read_jsonl_records,
    validate_jsonl_row,
)
from src.core.knowledge.predicate_registry import (
    knowledge_class_for_predicate,
    validate_predicate_value,
)
from src.core.knowledge_store import get_shared_knowledge_root
from src.core.ledger.event_log import ConfidenceTier
from src.core.ledger.source_refs import (
    SourceRef,
    source_document_key,
    source_ref_from_dict,
    source_ref_to_dict,
)
from src.core.workspace_lease import cooperative_capture_fence
from src.core.workspace_paths import PROGRAMS_ROOT

SKIP_EXPIRY_DAYS = 90
PENDING_MAX_BYTES = 10 * 1024 * 1024
TRIAGED_MAX_BYTES = 10 * 1024 * 1024
REVIEW_EVENT_KINDS = frozenset({"reviewed", "review_renewed"})
TERMINAL_DECISION_KINDS = frozenset({"approved", "rejected", "skipped"})
RESOLUTION_VERIFIED_KIND = "resolution_verified"


class KnowledgeCandidateReviewState(str, Enum):
    NOT_REQUIRED = "not_required"
    UNREVIEWED = "unreviewed"
    CURRENT = "current"
    EXPIRED = "expired"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class KnowledgeCandidateEntityResolution:
    raw_name: str
    resolved_entity_id: str | None
    match_kind: str
    score: float


@dataclass(frozen=True, slots=True)
class ProposedKnowledgeClaim:
    subject: str
    predicate: str
    value: Any
    valid_from: datetime
    valid_until: datetime | None


@dataclass(frozen=True, slots=True)
class KnowledgeCandidate:
    candidate_id: str
    scope: str
    proposed_claim: ProposedKnowledgeClaim
    proposed_confidence: str
    source_ref: SourceRef
    pipeline: str
    extraction_confidence: float
    entity_resolution: tuple[KnowledgeCandidateEntityResolution, ...]
    dedupe_key: str
    source_document_key: str
    corroborating_refs: tuple[SourceRef, ...]
    batch_id: str
    created_at: datetime | None = None
    knowledge_class: str | None = None
    requires_authorized_review: bool = False


@dataclass(frozen=True, slots=True)
class KnowledgeCandidateDecisionRecord:
    candidate_id: str
    kind: str
    decided_at: datetime
    triage_actor: str
    batch_id: str | None = None
    reason: str | None = None
    edited: bool | None = None
    resulting_claim_id: str | None = None
    review_valid_until: datetime | None = None
    actor_principal: str | None = None
    actor_machine: str | None = None
    actor_session: str | None = None
    candidate_sha256: str | None = None
    review_authority_program: str | None = None
    reviewed_subject: str | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeCandidateLifecycleSummary:
    latest_review: KnowledgeCandidateDecisionRecord | None
    latest_terminal_decision: KnowledgeCandidateDecisionRecord | None
    latest_verified_resolution: KnowledgeCandidateDecisionRecord | None
    review_state: KnowledgeCandidateReviewState
    history: tuple[KnowledgeCandidateDecisionRecord, ...]


@dataclass(frozen=True, slots=True)
class KnowledgeTriageSessionSummary:
    actor: str
    started_at: datetime
    ended_at: datetime
    decision_count: int
    duration_seconds: int
    throughput_per_minute: float


@dataclass(frozen=True, slots=True)
class KnowledgeTriageActivitySummary:
    total_decision_count: int
    latest_decision_at: datetime | None
    latest_session: KnowledgeTriageSessionSummary | None
    session_gap_minutes: int


@dataclass(frozen=True, slots=True)
class KnowledgeBatchProgressRecord:
    batch_id: str
    status: str
    total_candidate_count: int
    active_candidate_count: int
    approved_count: int
    rejected_count: int
    skipped_count: int
    quarantined_candidate_count: int
    latest_decision_at: datetime | None
    pipelines: dict[str, int]


@dataclass(frozen=True, slots=True)
class KnowledgeBatchProgressSummary:
    batch_count: int
    staged_batch_count: int
    approved_batch_count: int
    quarantined_batch_count: int
    records: tuple[KnowledgeBatchProgressRecord, ...]


def get_candidates_dir(*, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root.parent / "knowledge" / "candidates"


def get_pending_path(*, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_candidates_dir(programs_root=programs_root) / "pending.jsonl"


def get_triaged_path(*, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_candidates_dir(programs_root=programs_root) / "triaged.jsonl"


def append_candidate(candidate: KnowledgeCandidate, *, programs_root: Path = PROGRAMS_ROOT) -> None:
    with cooperative_capture_fence(get_shared_knowledge_root(programs_root)):
        validate_predicate_value(candidate.proposed_claim.predicate, candidate.proposed_claim.value)
        append_jsonl_line(
            get_pending_path(programs_root=programs_root),
            json.dumps(_candidate_to_record(candidate), sort_keys=True) + "\n",
            max_bytes=PENDING_MAX_BYTES,
        )
        from src.core.knowledge_index import upsert_candidate_vault_refs

        upsert_candidate_vault_refs(candidate, knowledge_root=get_shared_knowledge_root(programs_root))


def append_triage_decision(decision: KnowledgeCandidateDecisionRecord, *, programs_root: Path = PROGRAMS_ROOT) -> None:
    with cooperative_capture_fence(get_shared_knowledge_root(programs_root)):
        append_jsonl_line(
            get_triaged_path(programs_root=programs_root),
            json.dumps(_decision_to_record(decision), sort_keys=True) + "\n",
            max_bytes=TRIAGED_MAX_BYTES,
        )
        from src.core.knowledge_index import apply_candidate_decision_to_index

        apply_candidate_decision_to_index(decision, knowledge_root=get_shared_knowledge_root(programs_root))


def load_pending_candidates(*, programs_root: Path = PROGRAMS_ROOT) -> tuple[KnowledgeCandidate, ...]:
    rows = read_jsonl_records(get_pending_path(programs_root=programs_root))
    candidates: list[KnowledgeCandidate] = []
    for row in rows:
        validate_jsonl_row(
            row,
            (
                "candidate_id",
                "scope",
                "proposed_claim",
                "proposed_confidence",
                "source_ref",
                "pipeline",
                "extraction_confidence",
                "entity_resolution",
                "dedupe_key",
                "source_document_key",
                "corroborating_refs",
                "batch_id",
            ),
            field_name="knowledge candidate",
        )
        candidates.append(_candidate_from_record(row))
    return tuple(candidates)


def load_triage_decisions(*, programs_root: Path = PROGRAMS_ROOT) -> tuple[KnowledgeCandidateDecisionRecord, ...]:
    rows = read_jsonl_records(get_triaged_path(programs_root=programs_root))
    decisions: list[KnowledgeCandidateDecisionRecord] = []
    for row in rows:
        validate_jsonl_row(row, ("candidate_id", "kind", "decided_at", "triage_actor"), field_name="knowledge triage decision")
        decisions.append(_decision_from_record(row))
    return tuple(decisions)


def active_candidates(*, programs_root: Path = PROGRAMS_ROOT, scope: str | None = None, batch_id: str | None = None, as_of: datetime | None = None) -> tuple[KnowledgeCandidate, ...]:
    current = as_of or datetime.now(timezone.utc)
    pending = load_pending_candidates(programs_root=programs_root)
    terminal_decisions: dict[str, KnowledgeCandidateDecisionRecord] = {}
    for decision in load_triage_decisions(programs_root=programs_root):
        if decision.kind in TERMINAL_DECISION_KINDS:
            terminal_decisions[decision.candidate_id] = decision
    active: list[KnowledgeCandidate] = []
    for candidate in pending:
        if scope is not None and candidate.scope != scope:
            continue
        if batch_id is not None and candidate.batch_id != batch_id:
            continue
        terminal_decision = terminal_decisions.get(candidate.candidate_id)
        if terminal_decision is None:
            active.append(candidate)
            continue
        if (
            terminal_decision.kind == "skipped"
            and terminal_decision.decided_at + timedelta(days=SKIP_EXPIRY_DAYS) > current
        ):
            active.append(candidate)
    return tuple(sorted(active, key=lambda item: (-item.extraction_confidence, item.candidate_id)))


def active_count(*, programs_root: Path = PROGRAMS_ROOT, scope: str | None = None, batch_id: str | None = None, as_of: datetime | None = None) -> int:
    return len(active_candidates(programs_root=programs_root, scope=scope, batch_id=batch_id, as_of=as_of))


def summarize_triage_activity(
    *,
    programs_root: Path = PROGRAMS_ROOT,
    session_gap_minutes: int = 30,
) -> KnowledgeTriageActivitySummary:
    decisions = tuple(
        sorted(
            (
                decision
                for decision in load_triage_decisions(programs_root=programs_root)
                if decision.kind in TERMINAL_DECISION_KINDS
            ),
            key=lambda item: (item.decided_at, item.candidate_id, item.kind),
        )
    )
    if not decisions:
        return KnowledgeTriageActivitySummary(
            total_decision_count=0,
            latest_decision_at=None,
            latest_session=None,
            session_gap_minutes=session_gap_minutes,
        )

    session_gap = timedelta(minutes=session_gap_minutes)
    sessions: list[list[KnowledgeCandidateDecisionRecord]] = []
    current_session: list[KnowledgeCandidateDecisionRecord] = [decisions[0]]
    for decision in decisions[1:]:
        previous = current_session[-1]
        if decision.triage_actor != previous.triage_actor or decision.decided_at - previous.decided_at > session_gap:
            sessions.append(current_session)
            current_session = [decision]
            continue
        current_session.append(decision)
    sessions.append(current_session)

    latest_session_records = sessions[-1]
    started_at = latest_session_records[0].decided_at
    ended_at = latest_session_records[-1].decided_at
    duration_seconds = max(0, int((ended_at - started_at).total_seconds()))
    duration_minutes = max(duration_seconds / 60.0, 1.0)
    latest_session = KnowledgeTriageSessionSummary(
        actor=latest_session_records[0].triage_actor,
        started_at=started_at,
        ended_at=ended_at,
        decision_count=len(latest_session_records),
        duration_seconds=duration_seconds,
        throughput_per_minute=round(len(latest_session_records) / duration_minutes, 3),
    )
    return KnowledgeTriageActivitySummary(
        total_decision_count=len(decisions),
        latest_decision_at=decisions[-1].decided_at,
        latest_session=latest_session,
        session_gap_minutes=session_gap_minutes,
    )


def summarize_batch_progress(*, programs_root: Path = PROGRAMS_ROOT) -> KnowledgeBatchProgressSummary:
    pending_candidates = load_pending_candidates(programs_root=programs_root)
    active_by_batch: dict[str, int] = {}
    for candidate in active_candidates(programs_root=programs_root):
        active_by_batch[candidate.batch_id] = active_by_batch.get(candidate.batch_id, 0) + 1

    latest_decisions_by_candidate: dict[str, KnowledgeCandidateDecisionRecord] = {}
    for decision in load_triage_decisions(programs_root=programs_root):
        if decision.kind in TERMINAL_DECISION_KINDS:
            latest_decisions_by_candidate[decision.candidate_id] = decision

    candidates_by_batch: dict[str, list[KnowledgeCandidate]] = {}
    for candidate in pending_candidates:
        candidates_by_batch.setdefault(candidate.batch_id, []).append(candidate)

    decision_only_batch_ids = {
        decision.batch_id
        for decision in latest_decisions_by_candidate.values()
        if isinstance(decision.batch_id, str) and decision.batch_id
    }
    batch_ids = sorted({*candidates_by_batch.keys(), *(batch_id for batch_id in decision_only_batch_ids if batch_id)})
    records: list[KnowledgeBatchProgressRecord] = []
    staged_batch_count = 0
    approved_batch_count = 0
    quarantined_batch_count = 0

    for batch_id in batch_ids:
        candidates = candidates_by_batch.get(batch_id, [])
        decisions = [
            latest_decisions_by_candidate[candidate.candidate_id]
            for candidate in candidates
            if candidate.candidate_id in latest_decisions_by_candidate
        ]
        approved_count = sum(1 for decision in decisions if decision.kind == "approved")
        rejected_count = sum(1 for decision in decisions if decision.kind == "rejected")
        skipped_count = sum(1 for decision in decisions if decision.kind == "skipped")
        quarantined_candidate_count = sum(
            1
            for decision in decisions
            if decision.kind == "rejected" and isinstance(decision.reason, str) and decision.reason.startswith("quarantined:")
        )
        active_candidate_count = active_by_batch.get(batch_id, 0)
        latest_decision_at = max((decision.decided_at for decision in decisions), default=None)
        pipelines: dict[str, int] = {}
        for candidate in candidates:
            pipelines[candidate.pipeline] = pipelines.get(candidate.pipeline, 0) + 1

        if active_candidate_count > 0:
            status = "staged"
            staged_batch_count += 1
        elif candidates and quarantined_candidate_count == len(candidates) and approved_count == 0:
            status = "quarantined"
            quarantined_batch_count += 1
        elif approved_count > 0 and active_candidate_count == 0 and quarantined_candidate_count == 0:
            status = "approved"
            approved_batch_count += 1
        else:
            status = "mixed"

        records.append(
            KnowledgeBatchProgressRecord(
                batch_id=batch_id,
                status=status,
                total_candidate_count=len(candidates),
                active_candidate_count=active_candidate_count,
                approved_count=approved_count,
                rejected_count=rejected_count,
                skipped_count=skipped_count,
                quarantined_candidate_count=quarantined_candidate_count,
                latest_decision_at=latest_decision_at,
                pipelines=dict(sorted(pipelines.items())),
            )
        )

    return KnowledgeBatchProgressSummary(
        batch_count=len(records),
        staged_batch_count=staged_batch_count,
        approved_batch_count=approved_batch_count,
        quarantined_batch_count=quarantined_batch_count,
        records=tuple(records),
    )


def candidate_history(
    candidate_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[KnowledgeCandidateDecisionRecord, ...]:
    return tuple(
        record
        for record in load_triage_decisions(programs_root=programs_root)
        if record.candidate_id == candidate_id
    )


def candidate_payload_sha256(candidate: KnowledgeCandidate) -> str:
    payload = json.dumps(
        _candidate_to_record(candidate),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def candidate_knowledge_class(candidate: KnowledgeCandidate) -> str:
    derived = knowledge_class_for_predicate(candidate.proposed_claim.predicate).value
    if candidate.knowledge_class is not None and candidate.knowledge_class != derived:
        raise ValueError(
            f"Knowledge candidate '{candidate.candidate_id}' class does not match predicate "
            f"'{candidate.proposed_claim.predicate}'."
        )
    return derived


def latest_candidate_review(
    candidate_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> KnowledgeCandidateDecisionRecord | None:
    reviews = [
        record
        for record in candidate_history(candidate_id, programs_root=programs_root)
        if record.kind in REVIEW_EVENT_KINDS
    ]
    return max(reviews, key=lambda item: (item.decided_at, item.kind), default=None)


def latest_candidate_terminal_decision(
    candidate_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> KnowledgeCandidateDecisionRecord | None:
    terminal = [
        record
        for record in candidate_history(candidate_id, programs_root=programs_root)
        if record.kind in TERMINAL_DECISION_KINDS
    ]
    return max(terminal, key=lambda item: (item.decided_at, item.kind), default=None)


def latest_verified_candidate_resolution(
    candidate_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> KnowledgeCandidateDecisionRecord | None:
    resolutions = [
        record
        for record in candidate_history(candidate_id, programs_root=programs_root)
        if record.kind == RESOLUTION_VERIFIED_KIND
    ]
    return max(resolutions, key=lambda item: (item.decided_at, item.kind), default=None)


def candidate_lifecycle_summary(
    candidate: KnowledgeCandidate,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    as_of: datetime | None = None,
    review_authority_valid: bool | None = None,
) -> KnowledgeCandidateLifecycleSummary:
    current = as_of or datetime.now(timezone.utc)
    history = candidate_history(candidate.candidate_id, programs_root=programs_root)
    reviews = [record for record in history if record.kind in REVIEW_EVENT_KINDS]
    terminal = [record for record in history if record.kind in TERMINAL_DECISION_KINDS]
    resolutions = [record for record in history if record.kind == RESOLUTION_VERIFIED_KIND]
    review = max(reviews, key=lambda item: (item.decided_at, item.kind), default=None)
    review_state = _candidate_review_state(
        candidate,
        review,
        as_of=current,
        review_authority_valid=review_authority_valid,
    )
    return KnowledgeCandidateLifecycleSummary(
        latest_review=review,
        latest_terminal_decision=max(
            terminal, key=lambda item: (item.decided_at, item.kind), default=None
        ),
        latest_verified_resolution=max(
            resolutions, key=lambda item: (item.decided_at, item.kind), default=None
        ),
        review_state=review_state,
        history=history,
    )


def _candidate_review_state(
    candidate: KnowledgeCandidate,
    review: KnowledgeCandidateDecisionRecord | None,
    *,
    as_of: datetime,
    review_authority_valid: bool | None,
) -> KnowledgeCandidateReviewState:
    if not candidate.requires_authorized_review:
        return KnowledgeCandidateReviewState.NOT_REQUIRED
    if review is None:
        return KnowledgeCandidateReviewState.UNREVIEWED
    if (
        review.candidate_sha256 != candidate_payload_sha256(candidate)
        or review.actor_principal is None
        or review.actor_machine is None
        or review.actor_session is None
        or review.review_valid_until is None
        or review.decided_at > as_of
        or review_authority_valid is False
    ):
        return KnowledgeCandidateReviewState.INVALID
    if review.review_valid_until <= as_of:
        return KnowledgeCandidateReviewState.EXPIRED
    return KnowledgeCandidateReviewState.CURRENT


def derive_candidate_dedupe_key(scope: str, subject: str, predicate: str, value: Any) -> str:
    digest = hashlib.sha256(f"{scope}|{subject}|{predicate}|{json.dumps(value, sort_keys=True, separators=(',', ':'))}".encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def build_candidate(
    *,
    candidate_id: str,
    scope: str,
    subject: str,
    predicate: str,
    value: Any,
    valid_from: datetime,
    valid_until: datetime | None,
    proposed_confidence: ConfidenceTier,
    source_ref: SourceRef,
    pipeline: str,
    extraction_confidence: float,
    entity_resolution: tuple[KnowledgeCandidateEntityResolution, ...],
    corroborating_refs: tuple[SourceRef, ...],
    batch_id: str,
    created_at: datetime | None = None,
    requires_authorized_review: bool = False,
) -> KnowledgeCandidate:
    return KnowledgeCandidate(
        candidate_id=candidate_id,
        scope=scope,
        proposed_claim=ProposedKnowledgeClaim(
            subject=subject,
            predicate=predicate,
            value=value,
            valid_from=valid_from,
            valid_until=valid_until,
        ),
        proposed_confidence=proposed_confidence.value,
        source_ref=source_ref,
        pipeline=pipeline,
        extraction_confidence=extraction_confidence,
        entity_resolution=entity_resolution,
        dedupe_key=derive_candidate_dedupe_key(scope, subject, predicate, value),
        source_document_key=source_document_key(source_ref),
        corroborating_refs=corroborating_refs,
        batch_id=batch_id,
        created_at=(created_at or datetime.now(timezone.utc)).astimezone(timezone.utc),
        knowledge_class=knowledge_class_for_predicate(predicate).value,
        requires_authorized_review=requires_authorized_review,
    )


def _candidate_to_record(candidate: KnowledgeCandidate) -> dict[str, Any]:
    return {
        "candidate_id": candidate.candidate_id,
        "scope": candidate.scope,
        "proposed_claim": {
            "subject": candidate.proposed_claim.subject,
            "predicate": candidate.proposed_claim.predicate,
            "value": candidate.proposed_claim.value,
            "valid_from": candidate.proposed_claim.valid_from.isoformat(),
            "valid_until": candidate.proposed_claim.valid_until.isoformat() if candidate.proposed_claim.valid_until is not None else None,
        },
        "proposed_confidence": candidate.proposed_confidence,
        "source_ref": source_ref_to_dict(candidate.source_ref),
        "pipeline": candidate.pipeline,
        "extraction_confidence": candidate.extraction_confidence,
        "entity_resolution": [
            {
                "raw_name": resolution.raw_name,
                "resolved_entity_id": resolution.resolved_entity_id,
                "match_kind": resolution.match_kind,
                "score": resolution.score,
            }
            for resolution in candidate.entity_resolution
        ],
        "dedupe_key": candidate.dedupe_key,
        "source_document_key": candidate.source_document_key,
        "corroborating_refs": [source_ref_to_dict(ref) for ref in candidate.corroborating_refs],
        "batch_id": candidate.batch_id,
        "created_at": candidate.created_at.isoformat() if candidate.created_at is not None else None,
        "knowledge_class": candidate_knowledge_class(candidate),
        "requires_authorized_review": candidate.requires_authorized_review,
    }


def _candidate_from_record(record: dict[str, Any]) -> KnowledgeCandidate:
    claim = record["proposed_claim"]
    candidate_id = str(record["candidate_id"])
    predicate = str(claim["predicate"])
    derived_class = knowledge_class_for_predicate(predicate).value
    stored_class = record.get("knowledge_class")
    if stored_class is not None and (not isinstance(stored_class, str) or stored_class != derived_class):
        raise ValueError(f"Knowledge candidate '{candidate_id}' has an invalid knowledge class.")
    requires_review = record.get("requires_authorized_review", False)
    if not isinstance(requires_review, bool):
        raise ValueError(f"Knowledge candidate '{candidate_id}' has an invalid review requirement.")
    return KnowledgeCandidate(
        candidate_id=candidate_id,
        scope=str(record["scope"]),
        proposed_claim=ProposedKnowledgeClaim(
            subject=str(claim["subject"]),
            predicate=predicate,
            value=claim.get("value"),
            valid_from=datetime.fromisoformat(str(claim["valid_from"])).astimezone(timezone.utc),
            valid_until=datetime.fromisoformat(str(claim["valid_until"])).astimezone(timezone.utc) if isinstance(claim.get("valid_until"), str) else None,
        ),
        proposed_confidence=str(record["proposed_confidence"]),
        source_ref=source_ref_from_dict(dict(record["source_ref"])),
        pipeline=str(record["pipeline"]),
        extraction_confidence=float(record["extraction_confidence"]),
        entity_resolution=tuple(
            KnowledgeCandidateEntityResolution(
                raw_name=str(item["raw_name"]),
                resolved_entity_id=item.get("resolved_entity_id") if isinstance(item.get("resolved_entity_id"), str) else None,
                match_kind=str(item["match_kind"]),
                score=float(item["score"]),
            )
            for item in record["entity_resolution"]
        ),
        dedupe_key=str(record["dedupe_key"]),
        source_document_key=str(record["source_document_key"]),
        corroborating_refs=tuple(source_ref_from_dict(dict(item)) for item in record["corroborating_refs"]),
        batch_id=str(record["batch_id"]),
        created_at=datetime.fromisoformat(str(record["created_at"])).astimezone(timezone.utc) if isinstance(record.get("created_at"), str) else None,
        knowledge_class=derived_class,
        requires_authorized_review=requires_review,
    )


def _decision_to_record(decision: KnowledgeCandidateDecisionRecord) -> dict[str, Any]:
    return {
        "candidate_id": decision.candidate_id,
        "kind": decision.kind,
        "decided_at": decision.decided_at.isoformat(),
        "triage_actor": decision.triage_actor,
        "batch_id": decision.batch_id,
        "reason": decision.reason,
        "edited": decision.edited,
        "resulting_claim_id": decision.resulting_claim_id,
        "review_valid_until": decision.review_valid_until.isoformat() if decision.review_valid_until is not None else None,
        "actor_principal": decision.actor_principal,
        "actor_machine": decision.actor_machine,
        "actor_session": decision.actor_session,
        "candidate_sha256": decision.candidate_sha256,
        "review_authority_program": decision.review_authority_program,
        "reviewed_subject": decision.reviewed_subject,
    }


def _decision_from_record(record: dict[str, Any]) -> KnowledgeCandidateDecisionRecord:
    return KnowledgeCandidateDecisionRecord(
        candidate_id=str(record["candidate_id"]),
        kind=str(record["kind"]),
        decided_at=datetime.fromisoformat(str(record["decided_at"])).astimezone(timezone.utc),
        triage_actor=str(record["triage_actor"]),
        batch_id=str(record["batch_id"]) if isinstance(record.get("batch_id"), str) else None,
        reason=str(record["reason"]) if isinstance(record.get("reason"), str) else None,
        edited=bool(record["edited"]) if isinstance(record.get("edited"), bool) else None,
        resulting_claim_id=str(record["resulting_claim_id"]) if isinstance(record.get("resulting_claim_id"), str) else None,
        review_valid_until=(
            datetime.fromisoformat(str(record["review_valid_until"])).astimezone(timezone.utc)
            if isinstance(record.get("review_valid_until"), str)
            else None
        ),
        actor_principal=str(record["actor_principal"]) if isinstance(record.get("actor_principal"), str) else None,
        actor_machine=str(record["actor_machine"]) if isinstance(record.get("actor_machine"), str) else None,
        actor_session=str(record["actor_session"]) if isinstance(record.get("actor_session"), str) else None,
        candidate_sha256=str(record["candidate_sha256"]) if isinstance(record.get("candidate_sha256"), str) else None,
        review_authority_program=(
            str(record["review_authority_program"])
            if isinstance(record.get("review_authority_program"), str)
            else None
        ),
        reviewed_subject=(
            str(record["reviewed_subject"])
            if isinstance(record.get("reviewed_subject"), str)
            else None
        ),
    )