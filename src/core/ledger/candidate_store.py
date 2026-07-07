from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from src.core.jsonl_utils import append_jsonl_line, read_jsonl_records, validate_jsonl_row
from src.core.ledger import candidate_sqlite_store as _sqls
from src.core.ledger.event_types import validate_event_payload
from src.core.ledger.rev_evidence import EvidenceRef, evidence_refs_from_dict, evidence_refs_to_dict
from src.core.ledger.source_refs import SourceRef, source_ref_from_dict, source_ref_to_dict, validate_typed_source_ref


PROGRAMS_ROOT = Path(__file__).resolve().parents[3] / "programs"
SKIP_EXPIRY_DAYS = 90
PENDING_MAX_BYTES = 10 * 1024 * 1024
TRIAGED_MAX_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class CandidateEntityResolution:
    raw_name: str
    resolved_entity_id: str | None
    match_kind: str
    score: float


@dataclass(frozen=True, slots=True)
class CandidateEvent:
    candidate_id: str
    program_id: str
    proposed_event_type: str
    proposed_payload: dict[str, Any]
    proposed_occurred_at: datetime
    proposed_temporal_confidence: str
    proposed_confidence: str
    source_ref: SourceRef
    pipeline: str
    extraction_confidence: float
    entity_resolution: tuple[CandidateEntityResolution, ...]
    dedupe_key: str
    dedupe_core_hash: str
    source_document_key: str
    corroborating_refs: tuple[SourceRef, ...]
    batch_id: str
    staged_at: datetime | None = None
    # REV (specs/program-context-intelligence.md §5.7/§5.9). Both default so the
    # 25 existing callsites and old on-disk records round-trip unchanged; old
    # records parse as evidence_refs=() / schema_version="1".
    schema_version: str = "1"
    evidence_refs: tuple[EvidenceRef, ...] = ()
    # activation.md §6.12 / O-21 — extraction-provenance lineage: the prompt
    # version that produced this candidate (enables EXPLAIN-min, A/B testing,
    # and quality-regression rollback) and a 1-sentence rationale / verbatim
    # quote snippet grounding the extraction. Both default so old records and
    # non-LLM (deterministic) candidates round-trip unchanged.
    prompt_version: str | None = None
    extraction_rationale: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateDecisionRecord:
    candidate_id: str
    kind: str
    decided_at: datetime
    triage_actor: str
    batch_id: str | None = None
    reason: str | None = None
    edited: bool | None = None
    resulting_event_id: str | None = None
    suppress_until: datetime | None = None
    gap_event_id: str | None = None
    approval_event_id: str | None = None


@dataclass(frozen=True, slots=True)
class LedgerTriageSessionSummary:
    actor: str
    started_at: datetime
    ended_at: datetime
    decision_count: int
    duration_seconds: int
    throughput_per_minute: float


@dataclass(frozen=True, slots=True)
class LedgerTriageActivitySummary:
    total_decision_count: int
    latest_decision_at: datetime | None
    latest_session: LedgerTriageSessionSummary | None
    session_gap_minutes: int


@dataclass(frozen=True, slots=True)
class LedgerBatchProgressRecord:
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
class LedgerBatchProgressSummary:
    batch_count: int
    staged_batch_count: int
    approved_batch_count: int
    quarantined_batch_count: int
    records: tuple[LedgerBatchProgressRecord, ...]


def get_candidate_dir(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "ledger" / "candidates"


def get_candidate_db_dir(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    """Return the db_dir for SQLite outbox and ledger ops (public, for callers that need raw db_dir)."""
    return _get_candidate_db_dir(program_id, programs_root=programs_root)


def get_pending_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_candidate_dir(program_id, programs_root=programs_root) / "pending.jsonl"


def get_triaged_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_candidate_dir(program_id, programs_root=programs_root) / "triaged.jsonl"


def _get_candidate_db_dir(program_id: str, *, programs_root: Path) -> Path:
    """Return the SQLite db dir for ``program_id``, initializing + migrating from JSONL on first use."""
    db_dir = get_candidate_dir(program_id, programs_root=programs_root)
    db_path = _sqls.candidate_db_path(db_dir)
    if not db_path.exists():
        _sqls.init_candidate_db(db_dir)
        _sqls.migrate_jsonl_to_sqlite(
            db_dir,
            pending_path=get_pending_path(program_id, programs_root=programs_root),
            triaged_path=get_triaged_path(program_id, programs_root=programs_root),
        )
    return db_dir


def append_candidate(candidate: CandidateEvent, *, programs_root: Path = PROGRAMS_ROOT) -> bool:
    """Stage ``candidate`` into SQLite (with JSONL as audit trail).

    Returns True if written, False if already present (idempotent on
    ``candidate_id`` and ``dedupe_key`` — crash-resume safe, P1-0c).
    """
    validate_event_payload(candidate.proposed_event_type, candidate.proposed_payload)
    validate_typed_source_ref(candidate.source_ref)
    for corroborating_ref in candidate.corroborating_refs:
        validate_typed_source_ref(corroborating_ref)

    db_dir = _get_candidate_db_dir(candidate.program_id, programs_root=programs_root)
    payload = _candidate_to_record(candidate)
    payload_json = json.dumps(payload, sort_keys=True)

    inserted = _sqls.sqlite_insert_candidate(
        db_dir,
        candidate_id=candidate.candidate_id,
        program_id=candidate.program_id,
        batch_id=candidate.batch_id,
        source_document_key=candidate.source_document_key,
        dedupe_key=candidate.dedupe_key,
        staged_at=str(payload.get("staged_at", "")),
        payload_json=payload_json,
    )
    if not inserted:
        return False

    # JSONL is audit-only; rotation is fine since reads go to SQLite.
    append_jsonl_line(
        get_pending_path(candidate.program_id, programs_root=programs_root),
        payload_json + "\n",
        max_bytes=PENDING_MAX_BYTES,
    )
    return True


def append_triage_decision(
    decision: CandidateDecisionRecord,
    *,
    program_id: str,
    programs_root: Path = PROGRAMS_ROOT,
    staged_at: datetime | None = None,
    active_pending_count: int | None = None,
    oldest_pending_age_seconds: float | None = None,
) -> None:
    db_dir = _get_candidate_db_dir(program_id, programs_root=programs_root)
    payload = _decision_to_record(decision)
    payload_json = json.dumps(payload, sort_keys=True)
    _sqls.sqlite_insert_decision(
        db_dir,
        candidate_id=decision.candidate_id,
        kind=decision.kind,
        decided_at=decision.decided_at.isoformat(),
        triage_actor=decision.triage_actor,
        payload_json=payload_json,
    )
    # JSONL audit trail.
    append_jsonl_line(
        get_triaged_path(program_id, programs_root=programs_root),
        payload_json + "\n",
        max_bytes=TRIAGED_MAX_BYTES,
    )
    # activation.md §6.10 / AG-13 / O-14 — emit per-decision telemetry
    # (incl. time-to-triage) so accept/reject/edit rates + triage friction are
    # measured, not inferred. Best-effort: never raises into the decision path.
    from src.core.ledger.triage_telemetry import record_triage_decision_telemetry

    record_triage_decision_telemetry(
        program_id=program_id,
        candidate_id=decision.candidate_id,
        kind=decision.kind,
        decided_at=decision.decided_at,
        triage_actor=decision.triage_actor,
        staged_at=staged_at,
        edited=decision.edited,
        reason=decision.reason,
        batch_id=decision.batch_id,
        active_pending_count=active_pending_count,
        oldest_pending_age_seconds=oldest_pending_age_seconds,
        programs_root=programs_root,
    )


def load_pending_candidates(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> tuple[CandidateEvent, ...]:
    db_dir = _get_candidate_db_dir(program_id, programs_root=programs_root)
    rows = _sqls.sqlite_load_candidates(db_dir)
    candidates: list[CandidateEvent] = []
    for row in rows:
        validate_jsonl_row(
            row,
            (
                "candidate_id",
                "program_id",
                "proposed_event_type",
                "proposed_payload",
                "proposed_occurred_at",
                "proposed_temporal_confidence",
                "proposed_confidence",
                "source_ref",
                "pipeline",
                "extraction_confidence",
                "entity_resolution",
                "dedupe_key",
                "dedupe_core_hash",
                "source_document_key",
                "corroborating_refs",
                "batch_id",
            ),
            field_name="candidate",
        )
        candidates.append(_candidate_from_record(row))
    return tuple(candidates)


def load_triage_decisions(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> tuple[CandidateDecisionRecord, ...]:
    db_dir = _get_candidate_db_dir(program_id, programs_root=programs_root)
    rows = _sqls.sqlite_load_decisions(db_dir)
    decisions: list[CandidateDecisionRecord] = []
    for row in rows:
        validate_jsonl_row(row, ("candidate_id", "kind", "decided_at", "triage_actor"), field_name="triage decision")
        decisions.append(_decision_from_record(row))
    return tuple(decisions)


def active_candidates(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    as_of: datetime | None = None,
    batch_id: str | None = None,
) -> tuple[CandidateEvent, ...]:
    current = as_of or datetime.now(timezone.utc)
    candidates = load_pending_candidates(program_id, programs_root=programs_root)
    if batch_id is not None:
        candidates = tuple(candidate for candidate in candidates if candidate.batch_id == batch_id)
    decisions = load_triage_decisions(program_id, programs_root=programs_root)
    final_decisions: dict[str, CandidateDecisionRecord] = {}
    for decision in decisions:
        final_decisions[decision.candidate_id] = decision
    active: list[CandidateEvent] = []
    for candidate in candidates:
        active_decision = final_decisions.get(candidate.candidate_id)
        if active_decision is None:
            active.append(candidate)
            continue
        if active_decision.kind != "skipped":
            continue
        if active_decision.decided_at + timedelta(days=SKIP_EXPIRY_DAYS) > current:
            active.append(candidate)
    return tuple(sorted(active, key=lambda candidate: (-candidate.extraction_confidence, candidate.candidate_id)))


def active_count(program_id: str, *, programs_root: Path = PROGRAMS_ROOT, as_of: datetime | None = None, batch_id: str | None = None) -> int:
    return len(active_candidates(program_id, programs_root=programs_root, as_of=as_of, batch_id=batch_id))


def batch_ids(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> tuple[str, ...]:
    return tuple(sorted({candidate.batch_id for candidate in load_pending_candidates(program_id, programs_root=programs_root)}))


def summarize_triage_activity(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    session_gap_minutes: int = 30,
) -> LedgerTriageActivitySummary:
    decisions = tuple(
        sorted(
            load_triage_decisions(program_id, programs_root=programs_root),
            key=lambda item: (item.decided_at, item.candidate_id, item.kind),
        )
    )
    if not decisions:
        return LedgerTriageActivitySummary(
            total_decision_count=0,
            latest_decision_at=None,
            latest_session=None,
            session_gap_minutes=session_gap_minutes,
        )

    session_gap = timedelta(minutes=session_gap_minutes)
    sessions: list[list[CandidateDecisionRecord]] = []
    current_session: list[CandidateDecisionRecord] = [decisions[0]]
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
    latest_session = LedgerTriageSessionSummary(
        actor=latest_session_records[0].triage_actor,
        started_at=started_at,
        ended_at=ended_at,
        decision_count=len(latest_session_records),
        duration_seconds=duration_seconds,
        throughput_per_minute=round(len(latest_session_records) / duration_minutes, 3),
    )
    return LedgerTriageActivitySummary(
        total_decision_count=len(decisions),
        latest_decision_at=decisions[-1].decided_at,
        latest_session=latest_session,
        session_gap_minutes=session_gap_minutes,
    )


def summarize_batch_progress(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> LedgerBatchProgressSummary:
    pending_candidates = load_pending_candidates(program_id, programs_root=programs_root)
    active_by_batch: dict[str, int] = {}
    for candidate in active_candidates(program_id, programs_root=programs_root):
        active_by_batch[candidate.batch_id] = active_by_batch.get(candidate.batch_id, 0) + 1

    latest_decisions_by_candidate: dict[str, CandidateDecisionRecord] = {}
    for decision in load_triage_decisions(program_id, programs_root=programs_root):
        latest_decisions_by_candidate[decision.candidate_id] = decision

    candidates_by_batch: dict[str, list[CandidateEvent]] = {}
    for candidate in pending_candidates:
        candidates_by_batch.setdefault(candidate.batch_id, []).append(candidate)

    records: list[LedgerBatchProgressRecord] = []
    staged_batch_count = 0
    approved_batch_count = 0
    quarantined_batch_count = 0
    for batch_id in sorted(candidates_by_batch):
        candidates = candidates_by_batch[batch_id]
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
            LedgerBatchProgressRecord(
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

    return LedgerBatchProgressSummary(
        batch_count=len(records),
        staged_batch_count=staged_batch_count,
        approved_batch_count=approved_batch_count,
        quarantined_batch_count=quarantined_batch_count,
        records=tuple(records),
    )


def derive_candidate_dedupe_key(source_document_key: str, dedupe_core_hash: str) -> str:
    digest = hashlib.sha256(f"{source_document_key}|{dedupe_core_hash}".encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _candidate_to_record(candidate: CandidateEvent) -> dict[str, Any]:
    staged_at = candidate.staged_at or datetime.now(timezone.utc)
    return {
        "candidate_id": candidate.candidate_id,
        "program_id": candidate.program_id,
        "proposed_event_type": candidate.proposed_event_type,
        "proposed_payload": candidate.proposed_payload,
        "proposed_occurred_at": candidate.proposed_occurred_at.isoformat(),
        "proposed_temporal_confidence": candidate.proposed_temporal_confidence,
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
        "dedupe_core_hash": candidate.dedupe_core_hash,
        "source_document_key": candidate.source_document_key,
        "corroborating_refs": [source_ref_to_dict(ref) for ref in candidate.corroborating_refs],
        "batch_id": candidate.batch_id,
        "staged_at": staged_at.isoformat(),
        "schema_version": candidate.schema_version,
        "evidence_refs": evidence_refs_to_dict(candidate.evidence_refs),
        "prompt_version": candidate.prompt_version,
        "extraction_rationale": candidate.extraction_rationale,
    }


def _candidate_from_record(record: dict[str, Any]) -> CandidateEvent:
    entity_resolution = tuple(
        CandidateEntityResolution(
            raw_name=str(item["raw_name"]),
            resolved_entity_id=item.get("resolved_entity_id") if isinstance(item.get("resolved_entity_id"), str) else None,
            match_kind=str(item["match_kind"]),
            score=float(item["score"]),
        )
        for item in record["entity_resolution"]
    )
    return CandidateEvent(
        candidate_id=str(record["candidate_id"]),
        program_id=str(record["program_id"]),
        proposed_event_type=str(record["proposed_event_type"]),
        proposed_payload=dict(record["proposed_payload"]),
        proposed_occurred_at=datetime.fromisoformat(str(record["proposed_occurred_at"])).astimezone(timezone.utc),
        proposed_temporal_confidence=str(record["proposed_temporal_confidence"]),
        proposed_confidence=str(record["proposed_confidence"]),
        source_ref=source_ref_from_dict(dict(record["source_ref"])),
        pipeline=str(record["pipeline"]),
        extraction_confidence=float(record["extraction_confidence"]),
        entity_resolution=entity_resolution,
        dedupe_key=str(record["dedupe_key"]),
        dedupe_core_hash=str(record["dedupe_core_hash"]),
        source_document_key=str(record["source_document_key"]),
        corroborating_refs=tuple(source_ref_from_dict(dict(item)) for item in record["corroborating_refs"]),
        batch_id=str(record["batch_id"]),
        staged_at=(
            datetime.fromisoformat(str(record["staged_at"])).astimezone(timezone.utc)
            if isinstance(record.get("staged_at"), str)
            else datetime.fromisoformat(str(record["proposed_occurred_at"])).astimezone(timezone.utc)
        ),
        schema_version=str(record.get("schema_version", "1")),
        evidence_refs=evidence_refs_from_dict(record.get("evidence_refs", ())),
        prompt_version=(
            str(record["prompt_version"])
            if isinstance(record.get("prompt_version"), str) and record.get("prompt_version")
            else None
        ),
        extraction_rationale=(
            str(record["extraction_rationale"])
            if isinstance(record.get("extraction_rationale"), str) and record.get("extraction_rationale")
            else None
        ),
    )


def _decision_to_record(decision: CandidateDecisionRecord) -> dict[str, Any]:
    return {
        "candidate_id": decision.candidate_id,
        "kind": decision.kind,
        "decided_at": decision.decided_at.isoformat(),
        "triage_actor": decision.triage_actor,
        "batch_id": decision.batch_id,
        "reason": decision.reason,
        "edited": decision.edited,
        "resulting_event_id": decision.resulting_event_id,
        "approval_event_id": decision.approval_event_id,
        "suppress_until": decision.suppress_until.isoformat() if decision.suppress_until is not None else None,
        "gap_event_id": decision.gap_event_id,
    }


def _decision_from_record(record: dict[str, Any]) -> CandidateDecisionRecord:
    suppress_until_raw = record.get("suppress_until")
    return CandidateDecisionRecord(
        candidate_id=str(record["candidate_id"]),
        kind=str(record["kind"]),
        decided_at=datetime.fromisoformat(str(record["decided_at"])).astimezone(timezone.utc),
        triage_actor=str(record["triage_actor"]),
        batch_id=str(record["batch_id"]) if isinstance(record.get("batch_id"), str) else None,
        reason=str(record["reason"]) if isinstance(record.get("reason"), str) else None,
        edited=bool(record["edited"]) if isinstance(record.get("edited"), bool) else None,
        resulting_event_id=str(record["resulting_event_id"]) if isinstance(record.get("resulting_event_id"), str) else None,
        approval_event_id=str(record["approval_event_id"]) if isinstance(record.get("approval_event_id"), str) else None,
        suppress_until=datetime.fromisoformat(str(suppress_until_raw)).astimezone(timezone.utc) if isinstance(suppress_until_raw, str) else None,
        gap_event_id=str(record["gap_event_id"]) if isinstance(record.get("gap_event_id"), str) else None,
    )
