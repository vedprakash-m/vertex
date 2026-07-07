from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Sequence, cast

from src.core.exceptions import ConfigError
from src.core.jsonl_utils import append_jsonl_line, parse_jsonl_line
from src.core.knowledge_candidate_store import active_candidates as active_knowledge_candidates, summarize_batch_progress, summarize_triage_activity
from src.core.knowledge.vault import load_shared_vault_verify_status, vault_content_matches_metadata
from src.core.knowledge.predicate_registry import count as registered_predicate_count, validate_predicate_value
from src.core.knowledge_store import get_shared_knowledge_root
from src.core.ledger.event_log import DEFAULT_MAX_BYTES, ConfidenceTier, canonical_json
from src.core.ledger.source_refs import OperatorAssertionRef, SourceRef, source_document_key, source_ref_from_dict, source_ref_priority, source_ref_to_dict, validate_typed_source_ref
from src.core.ledger.ulid import new_ulid
from src.core.yaml_utils import load_yaml_mapping


PROGRAMS_ROOT = Path(__file__).resolve().parents[2] / "programs"

_CONFIDENCE_RANK = {
    ConfidenceTier.INFERRED: 1,
    ConfidenceTier.AI_EXTRACTED: 2,
    ConfidenceTier.SOURCE_AUTHORITATIVE: 3,
    ConfidenceTier.OPERATOR_CONFIRMED: 4,
}

CLAIMS_MAX_BYTES = DEFAULT_MAX_BYTES


@dataclass(frozen=True, slots=True)
class KnowledgeClaimRevision:
    claim_id: str
    scope: str
    subject: str
    predicate: str
    value: Any
    valid_from: datetime
    valid_until: datetime | None
    recorded_at: datetime
    confidence: ConfidenceTier
    source_ref: SourceRef
    supersedes: str | None
    natural_key: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "scope": self.scope,
            "subject": self.subject,
            "predicate": self.predicate,
            "value": self.value,
            "valid_from": self.valid_from.isoformat(),
            "valid_until": self.valid_until.isoformat() if self.valid_until is not None else None,
            "recorded_at": self.recorded_at.isoformat(),
            "confidence": self.confidence.value,
            "source_ref": self.source_ref_to_dict(),
            "supersedes": self.supersedes,
            "natural_key": self.natural_key,
        }

    def source_ref_to_dict(self) -> dict[str, Any]:
        from src.core.ledger.source_refs import source_ref_to_dict

        return source_ref_to_dict(self.source_ref)


@dataclass(frozen=True, slots=True)
class ResolvedKnowledgeClaim:
    claim_id: str
    scope: str
    subject: str
    predicate: str
    value: Any
    tombstoned: bool
    confidence: str
    valid_from: datetime
    valid_until: datetime | None
    recorded_at: datetime
    source_document_key: str
    overridden_claim_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class KnowledgeContextEntry:
    entity_id: str
    projection_coverage: str
    claims: tuple[ResolvedKnowledgeClaim, ...]


@dataclass(frozen=True, slots=True)
class KnowledgeContext:
    scope_chain: tuple[str, ...]
    as_of: datetime | None
    knowledge_as_of: datetime | None
    entries: tuple[KnowledgeContextEntry, ...]

    def entry(self, entity_id: str) -> KnowledgeContextEntry | None:
        for entry in self.entries:
            if entry.entity_id == entity_id:
                return entry
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_chain": list(self.scope_chain),
            "as_of": self.as_of.isoformat() if self.as_of is not None else None,
            "knowledge_as_of": self.knowledge_as_of.isoformat() if self.knowledge_as_of is not None else None,
            "entries": [
                {
                    "entity_id": entry.entity_id,
                    "projection_coverage": entry.projection_coverage,
                    "claims": [
                        {
                            "claim_id": claim.claim_id,
                            "scope": claim.scope,
                            "subject": claim.subject,
                            "predicate": claim.predicate,
                            "value": claim.value,
                            "tombstoned": claim.tombstoned,
                            "confidence": claim.confidence,
                            "valid_from": claim.valid_from.isoformat(),
                            "valid_until": claim.valid_until.isoformat() if claim.valid_until is not None else None,
                            "recorded_at": claim.recorded_at.isoformat(),
                            "source_document_key": claim.source_document_key,
                            "overridden_claim_ids": list(claim.overridden_claim_ids),
                        }
                        for claim in entry.claims
                    ],
                }
                for entry in self.entries
            ],
        }


@dataclass(frozen=True, slots=True)
class LatestClaimFreshnessSummary:
    expired: tuple[KnowledgeClaimRevision, ...]
    expiring_soon: tuple[KnowledgeClaimRevision, ...]
    warning_window_days: int

    @property
    def expired_count(self) -> int:
        return len(self.expired)

    @property
    def expiring_soon_count(self) -> int:
        return len(self.expiring_soon)


@dataclass(frozen=True, slots=True)
class StaleOperatorAssertionSummary:
    stale_without_ttl: tuple[KnowledgeClaimRevision, ...]
    age_threshold_days: int

    @property
    def stale_without_ttl_count(self) -> int:
        return len(self.stale_without_ttl)


@dataclass(frozen=True, slots=True)
class ActiveKnowledgeOverrideRecord:
    program_id: str
    entity_id: str
    predicate: str
    claim_id: str
    overridden_claim_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ActiveKnowledgeOverrideSummary:
    records: tuple[ActiveKnowledgeOverrideRecord, ...]

    @property
    def override_count(self) -> int:
        return len(self.records)

    @property
    def override_program_count(self) -> int:
        return len({record.program_id for record in self.records})


@dataclass(frozen=True, slots=True)
class KnowledgeScopeStatus:
    scope: str
    revision_count: int
    active_claim_count: int
    tombstoned_claim_count: int
    subject_count: int
    predicate_count: int
    latest_recorded_at: datetime | None
    active_claims_by_confidence: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "revision_count": self.revision_count,
            "active_claim_count": self.active_claim_count,
            "tombstoned_claim_count": self.tombstoned_claim_count,
            "subject_count": self.subject_count,
            "predicate_count": self.predicate_count,
            "latest_recorded_at": self.latest_recorded_at.isoformat() if self.latest_recorded_at is not None else None,
            "active_claims_by_confidence": dict(self.active_claims_by_confidence),
        }


@dataclass(frozen=True, slots=True)
class KnowledgeVaultStatus:
    file_count: int
    total_bytes: int
    missing_meta_count: int
    hash_mismatch_count: int
    missing_source_record_count: int
    missing_claim_ref_count: int
    missing_candidate_ref_count: int
    last_deep_verify_at: datetime | None
    last_deep_verify_ok: bool | None
    last_deep_verify_age_seconds: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "missing_meta_count": self.missing_meta_count,
            "hash_mismatch_count": self.hash_mismatch_count,
            "missing_source_record_count": self.missing_source_record_count,
            "missing_claim_ref_count": self.missing_claim_ref_count,
            "missing_candidate_ref_count": self.missing_candidate_ref_count,
            "last_deep_verify_at": self.last_deep_verify_at.isoformat() if self.last_deep_verify_at is not None else None,
            "last_deep_verify_ok": self.last_deep_verify_ok,
            "last_deep_verify_age_seconds": self.last_deep_verify_age_seconds,
        }


@dataclass(frozen=True, slots=True)
class KnowledgeStatusSummary:
    scopes: tuple[KnowledgeScopeStatus, ...]
    pending_candidate_count: int
    pending_candidates_by_pipeline: dict[str, int]
    oldest_pending_candidate_created_at: datetime | None
    oldest_pending_candidate_age_seconds: int | None
    pending_candidates_missing_created_at_count: int
    triaged_candidate_count: int
    latest_triage_decision_at: datetime | None
    latest_triage_session_actor: str | None
    latest_triage_session_started_at: datetime | None
    latest_triage_session_ended_at: datetime | None
    latest_triage_session_decision_count: int
    latest_triage_session_duration_seconds: int | None
    latest_triage_session_throughput_per_minute: float | None
    triage_session_gap_minutes: int
    registered_predicate_count: int
    batch_count: int
    staged_batch_count: int
    approved_batch_count: int
    quarantined_batch_count: int
    batches: tuple[dict[str, Any], ...]
    expired_claim_count: int
    expiring_soon_claim_count: int
    warning_window_days: int
    active_override_count: int
    active_override_program_count: int
    vault: KnowledgeVaultStatus

    def to_dict(self) -> dict[str, Any]:
        return {
            "scopes": [scope.to_dict() for scope in self.scopes],
            "pending_candidate_count": self.pending_candidate_count,
            "pending_candidates_by_pipeline": dict(self.pending_candidates_by_pipeline),
            "oldest_pending_candidate_created_at": self.oldest_pending_candidate_created_at.isoformat() if self.oldest_pending_candidate_created_at is not None else None,
            "oldest_pending_candidate_age_seconds": self.oldest_pending_candidate_age_seconds,
            "pending_candidates_missing_created_at_count": self.pending_candidates_missing_created_at_count,
            "triaged_candidate_count": self.triaged_candidate_count,
            "latest_triage_decision_at": self.latest_triage_decision_at.isoformat() if self.latest_triage_decision_at is not None else None,
            "latest_triage_session_actor": self.latest_triage_session_actor,
            "latest_triage_session_started_at": self.latest_triage_session_started_at.isoformat() if self.latest_triage_session_started_at is not None else None,
            "latest_triage_session_ended_at": self.latest_triage_session_ended_at.isoformat() if self.latest_triage_session_ended_at is not None else None,
            "latest_triage_session_decision_count": self.latest_triage_session_decision_count,
            "latest_triage_session_duration_seconds": self.latest_triage_session_duration_seconds,
            "latest_triage_session_throughput_per_minute": self.latest_triage_session_throughput_per_minute,
            "triage_session_gap_minutes": self.triage_session_gap_minutes,
            "registered_predicate_count": self.registered_predicate_count,
            "batch_count": self.batch_count,
            "staged_batch_count": self.staged_batch_count,
            "approved_batch_count": self.approved_batch_count,
            "quarantined_batch_count": self.quarantined_batch_count,
            "batches": [dict(batch) for batch in self.batches],
            "expired_claim_count": self.expired_claim_count,
            "expiring_soon_claim_count": self.expiring_soon_claim_count,
            "warning_window_days": self.warning_window_days,
            "active_override_count": self.active_override_count,
            "active_override_program_count": self.active_override_program_count,
            "vault": self.vault.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class KnowledgeClaimRedactionRecord:
    claim_id: str
    scope: str
    redacted_at: datetime
    actor: str
    reason: str
    original_claim_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "scope": self.scope,
            "redacted_at": self.redacted_at.isoformat(),
            "actor": self.actor,
            "reason": self.reason,
            "original_claim_hash": self.original_claim_hash,
        }


def load_program_knowledge_scopes(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> tuple[str, ...]:
    program_yaml = load_yaml_mapping(programs_root / program_id / "program.yaml", required=False)
    program_data = program_yaml.get("program", program_yaml) if isinstance(program_yaml, dict) else {}
    raw_scopes = program_data.get("knowledge_scopes", []) if isinstance(program_data, dict) else []
    scopes = tuple(str(scope).strip() for scope in raw_scopes if str(scope).strip())
    return (f"program:{program_id}", *scopes)


def load_program_knowledge_claims(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> tuple[KnowledgeClaimRevision, ...]:
    scope_chain = load_program_knowledge_scopes(program_id, programs_root=programs_root)
    knowledge_root = get_shared_knowledge_root(programs_root)
    return load_scoped_claim_revisions(scope_chain, knowledge_root=knowledge_root)


def append_claim_revision(
    *,
    scope: str,
    subject: str,
    predicate: str,
    value: Any,
    valid_from: datetime,
    valid_until: datetime | None,
    confidence: ConfidenceTier,
    source_ref: SourceRef,
    knowledge_root: Path,
    recorded_at: datetime | None = None,
) -> KnowledgeClaimRevision:
    validate_predicate_value(predicate, value)
    validate_typed_source_ref(source_ref)
    normalized_recorded_at = _ensure_utc(recorded_at or datetime.now(timezone.utc))
    normalized_valid_from = _ensure_utc(valid_from)
    normalized_valid_until = _ensure_utc(valid_until) if valid_until is not None else None
    natural_key = natural_key_for_claim(scope=scope, subject=subject, predicate=predicate)
    supersedes = latest_claim_for_natural_key(natural_key, scope=scope, knowledge_root=knowledge_root)
    revision = KnowledgeClaimRevision(
        claim_id=new_ulid(normalized_recorded_at),
        scope=scope,
        subject=subject,
        predicate=predicate,
        value=value,
        valid_from=normalized_valid_from,
        valid_until=normalized_valid_until,
        recorded_at=normalized_recorded_at,
        confidence=confidence,
        source_ref=source_ref,
        supersedes=supersedes.claim_id if supersedes is not None else None,
        natural_key=natural_key,
    )
    target_path = _resolve_claim_write_path(get_claims_dir_for_scope(scope, knowledge_root=knowledge_root), normalized_recorded_at, max_bytes=CLAIMS_MAX_BYTES)
    append_jsonl_line(target_path, canonical_json(revision.to_dict()) + "\n")
    from src.core.knowledge_index import upsert_claim_vault_refs

    upsert_claim_vault_refs(revision, knowledge_root=knowledge_root)
    return revision


def latest_claim_for_natural_key(
    natural_key: str,
    *,
    scope: str,
    knowledge_root: Path,
) -> KnowledgeClaimRevision | None:
    revisions = [
        revision
        for revision in load_scoped_claim_revisions((scope,), knowledge_root=knowledge_root)
        if revision.natural_key == natural_key
    ]
    if not revisions:
        return None
    return max(revisions, key=lambda revision: (revision.recorded_at, revision.claim_id))


def find_claim_revision_by_id(claim_id: str, *, knowledge_root: Path, include_redacted: bool = False) -> KnowledgeClaimRevision | None:
    redacted_ids = _redacted_claim_ids(knowledge_root) if not include_redacted else set()
    for path in sorted(knowledge_root.glob("**/claims-*.jsonl")):
        for revision in _read_claim_file(path):
            if revision.claim_id == claim_id and revision.claim_id not in redacted_ids:
                return revision
    return None


def find_claim_redaction_by_id(claim_id: str, *, knowledge_root: Path) -> KnowledgeClaimRedactionRecord | None:
    for record in reversed(load_claim_redactions(knowledge_root=knowledge_root)):
        if record.claim_id == claim_id:
            return record
    return None


def find_claim_revision_storage_path(claim_id: str, *, knowledge_root: Path) -> Path | None:
    located = _locate_claim_revision_row(claim_id, knowledge_root=knowledge_root)
    if located is None:
        return None
    path, _line_index, _payload = located
    return path


def load_claim_redactions(*, knowledge_root: Path) -> tuple[KnowledgeClaimRedactionRecord, ...]:
    path = _claim_redactions_path(knowledge_root)
    if not path.exists():
        return ()
    records: list[KnowledgeClaimRedactionRecord] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        payload = parse_jsonl_line(stripped)
        if not isinstance(payload, dict):
            raise ValueError(f"claim redaction row must be an object in {path}")
        records.append(_claim_redaction_record_from_dict(payload))
    return tuple(records)


def claim_ids_referencing_vault_hash(vault_hash: str, *, knowledge_root: Path) -> tuple[str, ...]:
    redacted_ids = _redacted_claim_ids(knowledge_root)
    claim_ids: list[str] = []
    for path in sorted(knowledge_root.glob("**/claims-*.jsonl")):
        for revision in _read_claim_file(path):
            if revision.claim_id in redacted_ids:
                continue
            if getattr(revision.source_ref, "vault_hash", None) == vault_hash:
                claim_ids.append(revision.claim_id)
    return tuple(claim_ids)


def redact_claim_revision(
    claim_id: str,
    *,
    knowledge_root: Path,
    actor: str,
    reason: str,
    redacted_at: datetime | None = None,
) -> KnowledgeClaimRedactionRecord:
    existing = find_claim_redaction_by_id(claim_id, knowledge_root=knowledge_root)
    if existing is not None:
        return existing
    located = _locate_claim_revision_row(claim_id, knowledge_root=knowledge_root)
    if located is None:
        raise ValueError(f"Unknown claim id: {claim_id}")
    path, line_index, payload = located
    normalized_redacted_at = _ensure_utc(redacted_at or datetime.now(timezone.utc))
    rewritten = dict(payload)
    rewritten["value"] = None
    rewritten["source_ref"] = source_ref_to_dict(
        OperatorAssertionRef(
            asserted_by=actor,
            asserted_at=normalized_redacted_at,
            context=f"redacted: {reason}",
        )
    )
    rewritten["redacted"] = True
    rewritten["redacted_at"] = normalized_redacted_at.isoformat()
    rewritten["redaction_reason"] = reason
    _rewrite_jsonl_line(path, line_index=line_index, replacement=canonical_json(rewritten))
    record = KnowledgeClaimRedactionRecord(
        claim_id=claim_id,
        scope=_required_str(payload, "scope"),
        redacted_at=normalized_redacted_at,
        actor=actor,
        reason=reason,
        original_claim_hash=_claim_payload_hash(payload),
    )
    redactions_path = _claim_redactions_path(knowledge_root)
    redactions_path.parent.mkdir(parents=True, exist_ok=True)
    append_jsonl_line(redactions_path, canonical_json(record.to_dict()) + "\n")
    from src.core.knowledge_index import remove_claim_vault_refs

    remove_claim_vault_refs(claim_id, knowledge_root=knowledge_root)
    return record


def summarize_knowledge_status(*, knowledge_root: Path) -> KnowledgeStatusSummary:
    scope_statuses: list[KnowledgeScopeStatus] = []
    all_revisions: list[KnowledgeClaimRevision] = []
    programs_root = _programs_root_for_knowledge_root(knowledge_root)
    for scope in _discover_claim_scopes(knowledge_root):
        revisions = load_scoped_claim_revisions((scope,), knowledge_root=knowledge_root)
        all_revisions.extend(revisions)
        latest_by_natural_key: dict[str, KnowledgeClaimRevision] = {}
        for revision in revisions:
            current = latest_by_natural_key.get(revision.natural_key)
            if current is None or (revision.recorded_at, revision.claim_id) > (current.recorded_at, current.claim_id):
                latest_by_natural_key[revision.natural_key] = revision
        winners = tuple(latest_by_natural_key.values())
        active_claims_by_confidence = {tier.value: 0 for tier in ConfidenceTier}
        for revision in winners:
            if revision.value is None:
                continue
            active_claims_by_confidence[revision.confidence.value] += 1
        scope_statuses.append(
            KnowledgeScopeStatus(
                scope=scope,
                revision_count=len(revisions),
                active_claim_count=sum(1 for revision in winners if revision.value is not None),
                tombstoned_claim_count=sum(1 for revision in winners if revision.value is None),
                subject_count=len({revision.subject for revision in winners}),
                predicate_count=len({revision.predicate for revision in winners if revision.value is not None}),
                latest_recorded_at=max((revision.recorded_at for revision in revisions), default=None),
                active_claims_by_confidence=active_claims_by_confidence,
            )
        )
    freshness = summarize_latest_claim_freshness(all_revisions)
    override_summary = summarize_active_knowledge_overrides(programs_root=programs_root)
    active_pending_candidates = active_knowledge_candidates(programs_root=programs_root)
    pending_candidates_by_pipeline: dict[str, int] = {}
    for candidate in active_pending_candidates:
        pending_candidates_by_pipeline[candidate.pipeline] = pending_candidates_by_pipeline.get(candidate.pipeline, 0) + 1
    pending_candidates_with_created_at = [candidate.created_at for candidate in active_pending_candidates if candidate.created_at is not None]
    oldest_pending_candidate_created_at = min(pending_candidates_with_created_at) if pending_candidates_with_created_at else None
    oldest_pending_candidate_age_seconds = None
    if oldest_pending_candidate_created_at is not None:
        oldest_pending_candidate_age_seconds = max(0, int((datetime.now(timezone.utc) - oldest_pending_candidate_created_at).total_seconds()))
    pending_candidates_missing_created_at_count = sum(1 for candidate in active_pending_candidates if candidate.created_at is None)
    triage_activity = summarize_triage_activity(programs_root=programs_root)
    batch_progress = summarize_batch_progress(programs_root=programs_root)

    candidates_dir = knowledge_root / "candidates"
    verify_status = load_shared_vault_verify_status(programs_root=programs_root)
    verify_age_seconds = None
    if verify_status is not None:
        verify_age_seconds = max(0, int((datetime.now(timezone.utc) - verify_status.verified_at).total_seconds()))
    present_vault_hashes = {
        f"sha256:{path.name}"
        for path in (knowledge_root / "vault").glob("**/*")
        if path.is_file() and not path.name.endswith(".meta.json")
    } if (knowledge_root / "vault").exists() else set()
    missing_source_record_count = 0
    for sources_path in knowledge_root.glob("**/sources.yaml"):
        document = load_yaml_mapping(sources_path, required=False, default={"sources": []})
        for item in document.get("sources", []):
            if not isinstance(item, dict):
                continue
            vault_hash = item.get("vault_hash")
            if isinstance(vault_hash, str) and vault_hash and vault_hash not in present_vault_hashes:
                missing_source_record_count += 1
    missing_claim_ref_count = 0
    for revision in load_all_claim_revisions(knowledge_root=knowledge_root):
        if any(vault_hash not in present_vault_hashes for vault_hash in _vault_hashes_for_source_refs((revision.source_ref,))):
            missing_claim_ref_count += 1
    missing_candidate_ref_count = 0
    for candidate in active_knowledge_candidates(programs_root=programs_root):
        candidate_refs = (candidate.source_ref, *candidate.corroborating_refs)
        if any(vault_hash not in present_vault_hashes for vault_hash in _vault_hashes_for_source_refs(candidate_refs)):
            missing_candidate_ref_count += 1
    return KnowledgeStatusSummary(
        scopes=tuple(sorted(scope_statuses, key=lambda item: item.scope)),
        pending_candidate_count=len(active_pending_candidates),
        pending_candidates_by_pipeline=dict(sorted(pending_candidates_by_pipeline.items())),
        oldest_pending_candidate_created_at=oldest_pending_candidate_created_at,
        oldest_pending_candidate_age_seconds=oldest_pending_candidate_age_seconds,
        pending_candidates_missing_created_at_count=pending_candidates_missing_created_at_count,
        triaged_candidate_count=_count_jsonl_rows(candidates_dir / "triaged.jsonl"),
        latest_triage_decision_at=triage_activity.latest_decision_at,
        latest_triage_session_actor=None if triage_activity.latest_session is None else triage_activity.latest_session.actor,
        latest_triage_session_started_at=None if triage_activity.latest_session is None else triage_activity.latest_session.started_at,
        latest_triage_session_ended_at=None if triage_activity.latest_session is None else triage_activity.latest_session.ended_at,
        latest_triage_session_decision_count=0 if triage_activity.latest_session is None else triage_activity.latest_session.decision_count,
        latest_triage_session_duration_seconds=None if triage_activity.latest_session is None else triage_activity.latest_session.duration_seconds,
        latest_triage_session_throughput_per_minute=None if triage_activity.latest_session is None else triage_activity.latest_session.throughput_per_minute,
        triage_session_gap_minutes=triage_activity.session_gap_minutes,
        registered_predicate_count=registered_predicate_count(),
        batch_count=batch_progress.batch_count,
        staged_batch_count=batch_progress.staged_batch_count,
        approved_batch_count=batch_progress.approved_batch_count,
        quarantined_batch_count=batch_progress.quarantined_batch_count,
        batches=tuple(
            {
                "batch_id": record.batch_id,
                "status": record.status,
                "total_candidate_count": record.total_candidate_count,
                "active_candidate_count": record.active_candidate_count,
                "approved_count": record.approved_count,
                "rejected_count": record.rejected_count,
                "skipped_count": record.skipped_count,
                "quarantined_candidate_count": record.quarantined_candidate_count,
                "latest_decision_at": None if record.latest_decision_at is None else record.latest_decision_at.isoformat(),
                "pipelines": dict(record.pipelines),
            }
            for record in batch_progress.records
        ),
        expired_claim_count=freshness.expired_count,
        expiring_soon_claim_count=freshness.expiring_soon_count,
        warning_window_days=freshness.warning_window_days,
        active_override_count=override_summary.override_count,
        active_override_program_count=override_summary.override_program_count,
        vault=_summarize_vault(
            knowledge_root / "vault",
            missing_source_record_count=missing_source_record_count,
            missing_claim_ref_count=missing_claim_ref_count,
            missing_candidate_ref_count=missing_candidate_ref_count,
            last_deep_verify_at=None if verify_status is None else verify_status.verified_at,
            last_deep_verify_ok=None if verify_status is None else verify_status.ok,
            last_deep_verify_age_seconds=verify_age_seconds,
        ),
    )


def natural_key_for_claim(*, scope: str, subject: str, predicate: str) -> str:
    return f"{scope}/{subject}/{predicate}"


def summarize_latest_claim_freshness(
    revisions: Sequence[KnowledgeClaimRevision],
    *,
    now: datetime | None = None,
    warning_window_days: int = 30,
) -> LatestClaimFreshnessSummary:
    latest_by_natural_key: dict[str, KnowledgeClaimRevision] = {}
    for revision in revisions:
        current = latest_by_natural_key.get(revision.natural_key)
        if current is None or (revision.recorded_at, revision.claim_id) > (current.recorded_at, current.claim_id):
            latest_by_natural_key[revision.natural_key] = revision
    normalized_now = _ensure_utc(now or datetime.now(timezone.utc))
    soon_cutoff = normalized_now + timedelta(days=warning_window_days)
    expired: list[KnowledgeClaimRevision] = []
    expiring_soon: list[KnowledgeClaimRevision] = []
    for revision in latest_by_natural_key.values():
        if revision.valid_until is None:
            continue
        if revision.valid_until <= normalized_now:
            expired.append(revision)
        elif revision.valid_until <= soon_cutoff:
            expiring_soon.append(revision)
    expired.sort(key=lambda revision: (revision.valid_until or datetime.max.replace(tzinfo=timezone.utc), revision.claim_id))
    expiring_soon.sort(key=lambda revision: (revision.valid_until or datetime.max.replace(tzinfo=timezone.utc), revision.claim_id))
    return LatestClaimFreshnessSummary(
        expired=tuple(expired),
        expiring_soon=tuple(expiring_soon),
        warning_window_days=warning_window_days,
    )


def summarize_stale_operator_assertions(
    revisions: Sequence[KnowledgeClaimRevision],
    *,
    now: datetime | None = None,
    age_threshold_days: int = 180,
) -> StaleOperatorAssertionSummary:
    latest_by_natural_key: dict[str, KnowledgeClaimRevision] = {}
    for revision in revisions:
        current = latest_by_natural_key.get(revision.natural_key)
        if current is None or (revision.recorded_at, revision.claim_id) > (current.recorded_at, current.claim_id):
            latest_by_natural_key[revision.natural_key] = revision
    normalized_now = _ensure_utc(now or datetime.now(timezone.utc))
    stale_cutoff = normalized_now - timedelta(days=age_threshold_days)
    stale_without_ttl: list[KnowledgeClaimRevision] = []
    for revision in latest_by_natural_key.values():
        if revision.valid_until is not None:
            continue
        if not isinstance(revision.source_ref, OperatorAssertionRef):
            continue
        asserted_at = _ensure_utc(revision.source_ref.asserted_at)
        if asserted_at > stale_cutoff:
            continue
        stale_without_ttl.append(revision)
    stale_without_ttl.sort(key=lambda r: (_ensure_utc(cast(OperatorAssertionRef, r.source_ref).asserted_at), r.claim_id))
    return StaleOperatorAssertionSummary(
        stale_without_ttl=tuple(stale_without_ttl),
        age_threshold_days=age_threshold_days,
    )


def summarize_active_knowledge_overrides(*, programs_root: Path) -> ActiveKnowledgeOverrideSummary:
    records: list[ActiveKnowledgeOverrideRecord] = []
    if not programs_root.exists():
        return ActiveKnowledgeOverrideSummary(records=())
    for program_dir in sorted(programs_root.iterdir(), key=lambda item: item.name.lower()):
        if not program_dir.is_dir() or not (program_dir / "program.yaml").exists():
            continue
        program_id = program_dir.name
        scope_chain = load_program_knowledge_scopes(program_id, programs_root=programs_root)
        revisions = load_program_knowledge_claims(program_id, programs_root=programs_root)
        entity_ids = sorted({revision.subject for revision in revisions})
        if not entity_ids:
            continue
        context = resolve_knowledge_context(entity_ids, scope_chain=scope_chain, revisions=revisions)
        for entry in context.entries:
            for claim in entry.claims:
                if not claim.overridden_claim_ids:
                    continue
                records.append(
                    ActiveKnowledgeOverrideRecord(
                        program_id=program_id,
                        entity_id=entry.entity_id,
                        predicate=claim.predicate,
                        claim_id=claim.claim_id,
                        overridden_claim_ids=claim.overridden_claim_ids,
                    )
                )
    return ActiveKnowledgeOverrideSummary(records=tuple(records))


def get_claims_dir_for_scope(scope: str, *, knowledge_root: Path) -> Path:
    claims_dir = _claims_dir_for_scope(scope, knowledge_root=knowledge_root)
    if claims_dir is None:
        raise ConfigError(f"Unsupported knowledge scope: {scope}")
    claims_dir.mkdir(parents=True, exist_ok=True)
    return claims_dir


def load_scoped_claim_revisions(
    scope_chain: Sequence[str],
    *,
    knowledge_root: Path,
) -> tuple[KnowledgeClaimRevision, ...]:
    redacted_ids = _redacted_claim_ids(knowledge_root)
    revisions: list[KnowledgeClaimRevision] = []
    for scope in scope_chain:
        claims_dir = _claims_dir_for_scope(scope, knowledge_root=knowledge_root)
        if claims_dir is None or not claims_dir.exists():
            continue
        for path in sorted(claims_dir.glob("claims-*.jsonl")):
            revisions.extend(revision for revision in _read_claim_file(path) if revision.claim_id not in redacted_ids)
    revisions.sort(key=lambda revision: (revision.recorded_at, revision.claim_id))
    return tuple(revisions)


def load_all_claim_revisions(*, knowledge_root: Path, include_redacted: bool = False) -> tuple[KnowledgeClaimRevision, ...]:
    redacted_ids = _redacted_claim_ids(knowledge_root) if not include_redacted else set()
    revisions: list[KnowledgeClaimRevision] = []
    for path in sorted(knowledge_root.glob("**/claims-*.jsonl")):
        revisions.extend(revision for revision in _read_claim_file(path) if revision.claim_id not in redacted_ids)
    revisions.sort(key=lambda revision: (revision.recorded_at, revision.claim_id))
    return tuple(revisions)


def resolve_knowledge_context(
    entity_ids: Sequence[str],
    *,
    scope_chain: Sequence[str],
    revisions: Sequence[KnowledgeClaimRevision],
    projection_coverage: dict[str, str] | None = None,
    as_of: datetime | None = None,
    knowledge_as_of: datetime | None = None,
) -> KnowledgeContext:
    active_as_of = as_of or datetime.now(timezone.utc)
    coverage = dict(projection_coverage or {})
    scope_rank = {scope: index for index, scope in enumerate(scope_chain)}

    latest_by_natural_key: dict[str, KnowledgeClaimRevision] = {}
    for revision in revisions:
        if revision.scope not in scope_rank:
            continue
        if knowledge_as_of is not None and revision.recorded_at > knowledge_as_of:
            continue
        current = latest_by_natural_key.get(revision.natural_key)
        if current is None or (revision.recorded_at, revision.claim_id) > (current.recorded_at, current.claim_id):
            latest_by_natural_key[revision.natural_key] = revision

    candidates_by_subject_predicate: dict[tuple[str, str], list[KnowledgeClaimRevision]] = {}
    for revision in latest_by_natural_key.values():
        if revision.subject not in entity_ids:
            continue
        if revision.valid_from > active_as_of:
            continue
        if revision.valid_until is not None and active_as_of >= revision.valid_until:
            continue
        candidates_by_subject_predicate.setdefault((revision.subject, revision.predicate), []).append(revision)

    entries: list[KnowledgeContextEntry] = []
    for entity_id in entity_ids:
        resolved_claims: list[ResolvedKnowledgeClaim] = []
        predicates = sorted(predicate for subject, predicate in candidates_by_subject_predicate if subject == entity_id)
        for predicate in predicates:
            candidates = candidates_by_subject_predicate[(entity_id, predicate)]
            winner = max(candidates, key=lambda candidate: _claim_sort_key(candidate, scope_rank))
            overridden_claim_ids = tuple(
                candidate.claim_id
                for candidate in sorted(candidates, key=lambda item: (item.recorded_at, item.claim_id))
                if candidate.claim_id != winner.claim_id
                and candidate.confidence == winner.confidence
                and scope_rank[candidate.scope] > scope_rank[winner.scope]
            )
            resolved_claims.append(
                ResolvedKnowledgeClaim(
                    claim_id=winner.claim_id,
                    scope=winner.scope,
                    subject=winner.subject,
                    predicate=winner.predicate,
                    value=winner.value,
                    tombstoned=winner.value is None,
                    confidence=winner.confidence.value,
                    valid_from=winner.valid_from,
                    valid_until=winner.valid_until,
                    recorded_at=winner.recorded_at,
                    source_document_key=source_document_key(winner.source_ref),
                    overridden_claim_ids=overridden_claim_ids,
                )
            )
        entries.append(
            KnowledgeContextEntry(
                entity_id=entity_id,
                projection_coverage=coverage.get(entity_id, "absent"),
                claims=tuple(resolved_claims),
            )
        )

    return KnowledgeContext(
        scope_chain=tuple(scope_chain),
        as_of=as_of,
        knowledge_as_of=knowledge_as_of,
        entries=tuple(entries),
    )


def _claims_dir_for_scope(scope: str, *, knowledge_root: Path) -> Path | None:
    if scope == "org":
        return knowledge_root / "org" / "claims"
    if scope == "operator":
        return knowledge_root / "operator" / "claims"
    if ":" not in scope:
        return None
    family, value = scope.split(":", maxsplit=1)
    if family == "program":
        return knowledge_root / "programs" / value / "claims"
    if family == "domain":
        return knowledge_root / "domains" / value / "claims"
    if family == "portfolio":
        return knowledge_root / "portfolio" / value / "claims"
    return None


def _read_claim_file(path: Path) -> tuple[KnowledgeClaimRevision, ...]:
    revisions: list[KnowledgeClaimRevision] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        payload = parse_jsonl_line(stripped)
        if not isinstance(payload, dict):
            raise ValueError(f"claim row must be an object in {path}")
        revisions.append(_claim_revision_from_dict(payload))
    return tuple(revisions)


def _claim_revision_from_dict(payload: dict[str, Any]) -> KnowledgeClaimRevision:
    return KnowledgeClaimRevision(
        claim_id=_required_str(payload, "claim_id"),
        scope=_required_str(payload, "scope"),
        subject=_required_str(payload, "subject"),
        predicate=_required_str(payload, "predicate"),
        value=payload.get("value"),
        valid_from=_parse_claim_datetime(payload.get("valid_from"), field_name="valid_from"),
        valid_until=_parse_optional_claim_datetime(payload.get("valid_until"), field_name="valid_until"),
        recorded_at=_parse_claim_datetime(payload.get("recorded_at"), field_name="recorded_at"),
        confidence=ConfidenceTier(_required_str(payload, "confidence")),
        source_ref=source_ref_from_dict(_required_mapping(payload, "source_ref")),
        supersedes=_optional_str(payload.get("supersedes")),
        natural_key=_required_str(payload, "natural_key"),
    )


def _claim_redaction_record_from_dict(payload: dict[str, Any]) -> KnowledgeClaimRedactionRecord:
    return KnowledgeClaimRedactionRecord(
        claim_id=_required_str(payload, "claim_id"),
        scope=_required_str(payload, "scope"),
        redacted_at=_parse_claim_datetime(payload.get("redacted_at"), field_name="redacted_at"),
        actor=_required_str(payload, "actor"),
        reason=_required_str(payload, "reason"),
        original_claim_hash=_required_str(payload, "original_claim_hash"),
    )


def _claim_sort_key(revision: KnowledgeClaimRevision, scope_rank: dict[str, int]) -> tuple[int, int, datetime, int, str]:
    return (
        _CONFIDENCE_RANK[revision.confidence],
        -scope_rank[revision.scope],
        revision.valid_from,
        -source_ref_priority(revision.source_ref),
        revision.claim_id,
    )


def _parse_claim_datetime(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string.")
    if "T" not in value:
        return datetime.combine(date.fromisoformat(value), time.min, tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_optional_claim_datetime(value: Any, *, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _parse_claim_datetime(value, field_name=field_name)


def _required_str(payload: dict[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string.")
    return value


def _required_mapping(payload: dict[str, Any], field_name: str) -> dict[str, Any]:
    value = payload.get(field_name)
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a mapping.")
    return value


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("optional string field must be a string when present.")
    return value


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _claim_file_name(recorded_at: datetime, sequence: int) -> str:
    stem = recorded_at.strftime("claims-%Y-%m")
    return f"{stem}.jsonl" if sequence == 1 else f"{stem}.{sequence}.jsonl"


def _resolve_claim_write_path(claims_dir: Path, recorded_at: datetime, *, max_bytes: int) -> Path:
    claims_dir.mkdir(parents=True, exist_ok=True)
    sequence = 1
    while True:
        candidate = claims_dir / _claim_file_name(recorded_at, sequence)
        if not candidate.exists():
            return candidate
        if candidate.stat().st_size < max_bytes:
            return candidate
        sequence += 1


def _claim_redactions_path(knowledge_root: Path) -> Path:
    return knowledge_root / ".claim-redactions.jsonl"


def _redacted_claim_ids(knowledge_root: Path) -> set[str]:
    return {record.claim_id for record in load_claim_redactions(knowledge_root=knowledge_root)}


def _locate_claim_revision_row(claim_id: str, *, knowledge_root: Path) -> tuple[Path, int, dict[str, Any]] | None:
    for path in sorted(knowledge_root.glob("**/claims-*.jsonl")):
        for index, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            stripped = raw_line.strip()
            if not stripped:
                continue
            payload = parse_jsonl_line(stripped)
            if not isinstance(payload, dict):
                raise ValueError(f"claim row must be an object in {path}")
            if payload.get("claim_id") == claim_id:
                return path, index, payload
    return None


def _rewrite_jsonl_line(path: Path, *, line_index: int, replacement: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    if line_index < 0 or line_index >= len(lines):
        raise ValueError(f"Line index {line_index} out of range for {path}")
    lines[line_index] = replacement
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def _claim_payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _discover_claim_scopes(knowledge_root: Path) -> tuple[str, ...]:
    scopes: set[str] = set()
    if (knowledge_root / "org" / "claims").exists():
        scopes.add("org")
    if (knowledge_root / "operator" / "claims").exists():
        scopes.add("operator")
    for family, prefix in (("domains", "domain"), ("portfolio", "portfolio"), ("programs", "program")):
        family_root = knowledge_root / family
        if not family_root.exists():
            continue
        for child in family_root.iterdir():
            if child.is_dir() and (child / "claims").exists():
                scopes.add(f"{prefix}:{child.name}")
    return tuple(sorted(scopes))


def _count_jsonl_rows(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def _summarize_vault(
    vault_root: Path,
    *,
    missing_source_record_count: int,
    missing_claim_ref_count: int,
    missing_candidate_ref_count: int,
    last_deep_verify_at: datetime | None,
    last_deep_verify_ok: bool | None,
    last_deep_verify_age_seconds: int | None,
) -> KnowledgeVaultStatus:
    if not vault_root.exists():
        return KnowledgeVaultStatus(
            file_count=0,
            total_bytes=0,
            missing_meta_count=0,
            hash_mismatch_count=0,
            missing_source_record_count=missing_source_record_count,
            missing_claim_ref_count=missing_claim_ref_count,
            missing_candidate_ref_count=missing_candidate_ref_count,
            last_deep_verify_at=last_deep_verify_at,
            last_deep_verify_ok=last_deep_verify_ok,
            last_deep_verify_age_seconds=last_deep_verify_age_seconds,
        )
    file_count = 0
    total_bytes = 0
    missing_meta_count = 0
    hash_mismatch_count = 0
    for path in vault_root.glob("**/*"):
        if not path.is_file() or path.name.endswith(".meta.json"):
            continue
        file_count += 1
        total_bytes += path.stat().st_size
        metadata_path = path.with_name(path.name + ".meta.json")
        if not metadata_path.exists():
            missing_meta_count += 1
            continue
        try:
            if not vault_content_matches_metadata(path, metadata_path):
                hash_mismatch_count += 1
        except Exception:
            hash_mismatch_count += 1
    return KnowledgeVaultStatus(
        file_count=file_count,
        total_bytes=total_bytes,
        missing_meta_count=missing_meta_count,
        hash_mismatch_count=hash_mismatch_count,
        missing_source_record_count=missing_source_record_count,
        missing_claim_ref_count=missing_claim_ref_count,
        missing_candidate_ref_count=missing_candidate_ref_count,
        last_deep_verify_at=last_deep_verify_at,
        last_deep_verify_ok=last_deep_verify_ok,
        last_deep_verify_age_seconds=last_deep_verify_age_seconds,
    )


def _programs_root_for_knowledge_root(knowledge_root: Path) -> Path:
    return knowledge_root.parent / "programs"


def _vault_hashes_for_source_refs(source_refs: tuple[SourceRef, ...]) -> tuple[str, ...]:
    hashes: list[str] = []
    for source_ref in source_refs:
        vault_hash = getattr(source_ref, "vault_hash", None)
        if isinstance(vault_hash, str) and vault_hash:
            hashes.append(vault_hash)
    return tuple(hashes)