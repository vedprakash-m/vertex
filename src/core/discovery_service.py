from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from typing import Any

from src.core.channel_registry_store import ChannelRegistryStore
from src.core.program_paths import get_channel_registry_path
from src.core.discovery_intent import (
    DiscoveryAttempt,
    DiscoveryAttemptOutcome,
    SourceCandidate,
    SourceCandidateStatus,
    SourceIntent,
    SourceIntentStatus,
    SourceRefKind,
    build_discovery_attempt_id,
    build_source_candidate_id,
)
from src.core.discovery_resolution import ResolutionContext, passes_auto_resolution_gate
from src.core.integration_types import (
    ChannelRegistration,
    DiscoveredRef,
    DiscoveryCompleteness,
    DiscoveryResult,
    RegistrationBinding,
    RegistrationStatus,
    ScopeStatus,
    ScopeStatusKind,
)
from src.core.m365_discovery_support import RegistryIdCandidate
from src.core.m365_identifiers import normalize_meeting_id, normalize_thread_id
from src.core.m365_registry_store import M365RegistryArtifact
from src.core.source_candidate_store import (
    CANDIDATE_REJECTION_SUPPRESSION_DAYS,
    SourceCandidateStore,
    candidate_evidence_json,
)


_DEFAULT_PROVIDER_INSTANCE_ID = "default"
_DEFAULT_SOURCE_PROVIDER = "seeded_resolution"
_AUTO_ACCEPT_REASON = "auto-resolved unique high-confidence seeded candidate"


@dataclass(frozen=True, slots=True)
class SeededDiscoveryPersistenceResult:
    allowed_candidates: tuple[RegistryIdCandidate, ...]
    suppressed_candidates: tuple[RegistryIdCandidate, ...]
    auto_resolve_candidate: RegistryIdCandidate | None
    query_hash: str
    config_hash: str


@dataclass(frozen=True, slots=True)
class CandidateResolutionResult:
    stale_plan: bool
    accepted_candidate: SourceCandidate | None = None
    updated_intent: SourceIntent | None = None


def channel_for_source_ref_kind(ref_kind: SourceRefKind) -> str:
    if ref_kind in (SourceRefKind.MEETING_SERIES, SourceRefKind.TEAMS_CHAT, SourceRefKind.TEAMS_CHANNEL):
        return "teams"
    if ref_kind == SourceRefKind.EMAIL_THREAD:
        return "email"
    raise ValueError(f"Unsupported source ref kind '{ref_kind.value}'.")


def build_accepted_candidate_result(
    *,
    program_id: str,
    intent: SourceIntent,
    candidate: SourceCandidate,
    current_time: datetime,
    scope_prefix: str,
    auto_resolved: bool,
    first_discovered_at: datetime,
) -> DiscoveryResult:
    scope_id = f"{scope_prefix}:{intent.intent_id}"
    binding = RegistrationBinding(
        workstream_id=intent.workstream_id,
        scope_id=scope_id,
        source_type=candidate.source_provider,
        confidence=candidate.confidence,
        confidence_source=candidate.source_provider,
        pm_confirmed=True,
        promoted=True,
        status=RegistrationStatus.ACTIVE,
    )
    metadata: dict[str, str | int | float | bool | None] = {
        "decision_reason": candidate.decision_reason or "",
        "source_intent_id": intent.intent_id,
        "source_candidate_id": candidate.candidate_id,
    }
    if auto_resolved:
        metadata["auto_resolved"] = True
    registration = ChannelRegistration(
        channel=candidate.channel,
        program_id=program_id,
        provider_instance_id=candidate.provider_instance_id,
        ref_kind=intent.ref_kind.value,
        ref_id=candidate.ref_id,
        status=RegistrationStatus.ACTIVE,
        first_discovered_at=first_discovered_at,
        last_seen_at=current_time,
        confidence=candidate.confidence,
        confidence_source=candidate.source_provider,
        pm_confirmed=True,
        promoted=True,
        ref_title=intent.display_name,
        metadata=metadata,
        workstream_ids=(intent.workstream_id,),
    )
    discovered_ref = DiscoveredRef(
        registration=registration,
        bindings=(binding,),
    )
    return DiscoveryResult(
        channel=candidate.channel,
        program_id=program_id,
        provider_instance_id=candidate.provider_instance_id,
        discovered_refs=(discovered_ref,),
        completeness=DiscoveryCompleteness.INCREMENTAL,
        scope_statuses={
            scope_id: ScopeStatus(
                scope_id=scope_id,
                status=ScopeStatusKind.SUCCESS,
                completeness=DiscoveryCompleteness.INCREMENTAL,
                item_count=1,
            )
        },
        scope_state_updates={},
        errors=(),
        computed_at=current_time,
    )


def upsert_candidate_registration_with_conn(
    *,
    conn,
    program_id: str,
    programs_root: Path,
    intent: SourceIntent,
    candidate: SourceCandidate,
    current_time: datetime,
    ttl_days: int | None,
    scope_prefix: str,
    auto_resolved: bool,
    first_discovered_at: datetime,
) -> DiscoveryResult:
    accepted_result = build_accepted_candidate_result(
        program_id=program_id,
        intent=intent,
        candidate=candidate,
        current_time=current_time,
        scope_prefix=scope_prefix,
        auto_resolved=auto_resolved,
        first_discovered_at=first_discovered_at,
    )
    channel_store = ChannelRegistryStore(get_channel_registry_path(program_id, programs_root=programs_root), program_id, ensure_schema=False)
    channel_store.upsert_discovered_ref_with_conn(
        conn,
        accepted_result.discovered_refs[0],
        ttl_days=ttl_days,
        seen_at=current_time,
    )
    return accepted_result


def accept_candidate_and_resolve_intent(
    *,
    candidate_store: SourceCandidateStore,
    program_id: str,
    programs_root: Path,
    intent: SourceIntent,
    candidate_id: str,
    as_of: datetime,
    ttl_days: int | None,
    actor_alias: str,
    decision_reason: str = _AUTO_ACCEPT_REASON,
    scope_prefix: str = "auto",
    auto_resolved: bool = True,
    first_discovered_at_override: datetime | None = None,
) -> CandidateResolutionResult:
    with candidate_store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        live_intent = candidate_store.get_intent_with_conn(conn, intent.intent_id)
        live_candidate = candidate_store.get_candidate_with_conn(conn, candidate_id)
        if live_intent is None or live_candidate is None:
            conn.rollback()
            return CandidateResolutionResult(stale_plan=False)
        if live_intent.decision_version != intent.decision_version:
            conn.rollback()
            return CandidateResolutionResult(stale_plan=True)
        if live_candidate.status == SourceCandidateStatus.PENDING:
            accepted_candidate = candidate_store.update_candidate_status_with_conn(
                conn,
                live_candidate.candidate_id,
                status=SourceCandidateStatus.ACCEPTED,
                decided_by=actor_alias,
                decision_reason=decision_reason,
                expected_decision_version=live_candidate.decision_version,
            )
        elif live_candidate.status == SourceCandidateStatus.ACCEPTED:
            accepted_candidate = live_candidate
        else:
            conn.rollback()
            return CandidateResolutionResult(stale_plan=False)
        upsert_candidate_registration_with_conn(
            conn=conn,
            program_id=program_id,
            programs_root=programs_root,
            intent=live_intent,
            candidate=accepted_candidate,
            current_time=as_of,
            ttl_days=ttl_days,
            scope_prefix=scope_prefix,
            auto_resolved=auto_resolved,
            first_discovered_at=first_discovered_at_override or accepted_candidate.first_discovered_at,
        )
        updated_intent = candidate_store.update_intent_status_with_conn(
            conn,
            live_intent.intent_id,
            status=SourceIntentStatus.RESOLVED,
            updated_by=actor_alias,
            expected_decision_version=live_intent.decision_version,
        )
        conn.commit()
    return CandidateResolutionResult(
        stale_plan=False,
        accepted_candidate=accepted_candidate,
        updated_intent=updated_intent,
    )


def persist_seeded_source_discovery(
    *,
    candidate_store: SourceCandidateStore,
    program_id: str,
    intent: SourceIntent,
    candidates: tuple[RegistryIdCandidate, ...],
    topics: tuple[str, ...],
    owner_aliases: tuple[str, ...],
    available_tools: set[str],
    registry_artifacts: tuple[M365RegistryArtifact, ...],
    as_of: datetime,
    autonomous_run_id: str | None,
    unavailable_reason: str | None,
    duration_ms: int | None,
    discovery_limit: int,
    attempt_ttl_hours: int,
) -> SeededDiscoveryPersistenceResult:
    channel = channel_for_source_ref_kind(intent.ref_kind)
    allowed_candidates: list[RegistryIdCandidate] = []
    suppressed_candidates: list[RegistryIdCandidate] = []
    for candidate in candidates:
        rejected_candidate = candidate_store.get_recent_rejected_candidate_by_ref(
            ref_id=candidate.discovered_id,
            ref_kind=intent.ref_kind,
            as_of=as_of,
            channel=channel,
        )
        if rejected_candidate is not None:
            suppressed_candidates.append(candidate)
            continue
        allowed_candidates.append(candidate)
    query_hash = hashlib.sha1(
        json.dumps(
            {
                "display_name": intent.display_name,
                "owner_aliases": list(owner_aliases),
                "ref_kind": intent.ref_kind.value,
                "topics": list(topics),
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    config_hash = hashlib.sha1(
        json.dumps(
            {
                "limit": discovery_limit,
                "ref_kind": intent.ref_kind.value,
                "supported_tools": sorted(available_tools),
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    candidate_store.record_attempt(
        DiscoveryAttempt(
            attempt_id=build_discovery_attempt_id(
                program_id=program_id,
                intent_id=intent.intent_id,
                source_provider=_DEFAULT_SOURCE_PROVIDER,
                query_hash=query_hash,
                attempted_at=as_of,
            ),
            program_id=program_id,
            intent_id=intent.intent_id,
            workstream_id=intent.workstream_id,
            channel=channel,
            provider_instance_id=_DEFAULT_PROVIDER_INSTANCE_ID,
            ref_kind=intent.ref_kind,
            source_provider=_DEFAULT_SOURCE_PROVIDER,
            query_hash=query_hash,
            config_hash=config_hash,
            autonomous_run_id=autonomous_run_id,
            outcome=seeded_source_attempt_outcome(
                candidates=tuple(allowed_candidates),
                unavailable_reason=unavailable_reason,
                suppressed_candidate_count=len(suppressed_candidates),
            ),
            reason=seeded_source_attempt_reason(
                unavailable_reason=unavailable_reason,
                suppressed_candidate_count=len(suppressed_candidates),
            ),
            result_count=len(candidates),
            duration_ms=duration_ms,
            attempted_at=as_of,
            expires_at=as_of + timedelta(hours=attempt_ttl_hours),
        )
    )
    for candidate in allowed_candidates:
        persisted_candidate = SourceCandidate(
            candidate_id=build_source_candidate_id(
                program_id=program_id,
                channel=channel,
                provider_instance_id=_DEFAULT_PROVIDER_INSTANCE_ID,
                ref_kind=intent.ref_kind,
                ref_id=candidate.discovered_id,
            ),
            program_id=program_id,
            channel=channel,
            provider_instance_id=_DEFAULT_PROVIDER_INSTANCE_ID,
            ref_id=candidate.discovered_id,
            ref_kind=intent.ref_kind,
            display_name=candidate.label,
            confidence=1.0 if candidate.exact_match else candidate.match_score,
            source_provider=_DEFAULT_SOURCE_PROVIDER,
            status=SourceCandidateStatus.PENDING,
            evidence_json=candidate_evidence_json(
                {
                    "display_name": intent.display_name,
                    "exact_match": candidate.exact_match,
                    "match_score": candidate.match_score,
                    "match_origin": seeded_candidate_match_origin(candidate, registry_artifacts=registry_artifacts),
                    "query_topics": list(topics),
                    "ref_kind": intent.ref_kind.value,
                    "source_url": candidate.source_url,
                }
            ),
            first_discovered_at=as_of,
            last_seen_at=as_of,
        )
        candidate_store.upsert_candidate(persisted_candidate, pii_prescrubbed=True)
        candidate_store.link_candidate_to_intent(
            persisted_candidate.candidate_id,
            intent.intent_id,
            persisted_candidate.confidence,
        )
    return SeededDiscoveryPersistenceResult(
        allowed_candidates=tuple(allowed_candidates),
        suppressed_candidates=tuple(suppressed_candidates),
        auto_resolve_candidate=select_seeded_source_auto_resolve_candidate(
            tuple(allowed_candidates),
            ref_kind=intent.ref_kind,
        ),
        query_hash=query_hash,
        config_hash=config_hash,
    )


def select_seeded_source_auto_resolve_candidate(
    candidates: tuple[RegistryIdCandidate, ...],
    *,
    ref_kind: SourceRefKind,
) -> RegistryIdCandidate | None:
    if len(candidates) != 1:
        return None
    candidate = candidates[0]
    context = ResolutionContext(
        confidence=float(candidate.match_score),
        unique=True,
        exact_match=bool(candidate.exact_match),
    )
    if passes_auto_resolution_gate(ref_kind, context):
        return candidate
    return None


def registry_artifact_discovered_id(artifact: M365RegistryArtifact) -> str | None:
    if artifact.artifact_type == "meeting_series":
        return normalize_meeting_id(artifact.series_id)
    return normalize_thread_id(artifact.thread_id)


def seeded_candidate_match_origin(
    candidate: RegistryIdCandidate,
    *,
    registry_artifacts: tuple[M365RegistryArtifact, ...],
) -> str:
    for artifact in registry_artifacts:
        if registry_artifact_discovered_id(artifact) == candidate.discovered_id:
            return "registry_artifact"
    return "live_discovery"


def seeded_source_attempt_outcome(
    *,
    candidates: tuple[RegistryIdCandidate, ...],
    unavailable_reason: str | None,
    suppressed_candidate_count: int = 0,
) -> DiscoveryAttemptOutcome:
    if unavailable_reason is not None:
        return DiscoveryAttemptOutcome.AUTH_BLOCKED
    if suppressed_candidate_count > 0 and not candidates:
        return DiscoveryAttemptOutcome.REJECTED_CANDIDATE_SUPPRESSED
    if not candidates:
        return DiscoveryAttemptOutcome.NO_CANDIDATES
    high_confidence_candidates = [
        candidate
        for candidate in candidates
        if candidate.exact_match or float(candidate.match_score) >= 0.75
    ]
    if len(high_confidence_candidates) > 1:
        return DiscoveryAttemptOutcome.AMBIGUOUS
    return DiscoveryAttemptOutcome.CANDIDATES_FOUND


def seeded_source_attempt_reason(
    *,
    unavailable_reason: str | None,
    suppressed_candidate_count: int,
) -> str | None:
    if unavailable_reason is not None:
        return unavailable_reason
    if suppressed_candidate_count <= 0:
        return None
    noun = "candidate" if suppressed_candidate_count == 1 else "candidates"
    return (
        f"Suppressed {suppressed_candidate_count} recently rejected {noun} "
        f"within the {CANDIDATE_REJECTION_SUPPRESSION_DAYS}-day rejection window."
    )
