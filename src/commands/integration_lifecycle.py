from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import typer

from src.commands.integration_discovery import _candidate_store, _channel_config
from src.commands.integration_intent_status import (
    _accepted_candidates_for_intent_with_conn,
    _intent_match_confidence_with_conn,
    _recompute_intent_status_with_conn,
    _registration_exists_with_conn,
    _update_intent_status_if_needed_with_conn,
)
from src.core.channel_registry_store import ChannelRegistryStore
from src.core.discovery_intent import (
    SourceCandidate,
    SourceCandidateStatus,
    SourceIntent,
    SourceIntentStatus,
    SourceRefKind,
)
from src.core.discovery_service import (
    build_accepted_candidate_result,
    channel_for_source_ref_kind,
    upsert_candidate_registration_with_conn,
)
from src.core.integration_types import DiscoveryResult, RegistrationStatus
from src.core.models_v2 import Program
from src.core.source_candidate_store import SourceCandidateStore
from src.core.source_intent_audit import append_intent_decision_log, intent_decision_payload

LoadProgramFn = Callable[[str, Path], Program]
StoreFactory = Callable[..., ChannelRegistryStore]
BootstrapDiscoveryStateFn = Callable[..., None]


def _clear_candidate_rejection(
    *,
    program: str,
    candidate_store: SourceCandidateStore,
    candidate: SourceCandidate,
    pm_alias: str,
) -> tuple[SourceCandidate, tuple[tuple[SourceIntent, SourceIntent], ...]]:
    del program
    current_time = datetime.now(timezone.utc)
    intent_updates: list[tuple[SourceIntent, SourceIntent]] = []
    with candidate_store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        updated_candidate = candidate_store.update_candidate_status_with_conn(
            conn,
            candidate.candidate_id,
            status=SourceCandidateStatus.PENDING,
            decided_by=pm_alias,
            decision_reason=None,
            expected_decision_version=candidate.decision_version,
        )
        for match in candidate_store.get_intent_matches_with_conn(conn, candidate.candidate_id):
            intent = candidate_store.get_intent_with_conn(conn, match.intent_id)
            if intent is None or intent.status in {
                SourceIntentStatus.SUPPRESSED,
                SourceIntentStatus.RETIRED,
                SourceIntentStatus.SUPERSEDED,
            }:
                continue
            updated_intent = _update_intent_status_if_needed_with_conn(
                conn,
                candidate_store,
                intent,
                status=_recompute_intent_status_with_conn(
                    conn,
                    candidate_store,
                    intent.intent_id,
                    as_of=current_time,
                ),
                updated_by=pm_alias,
            )
            intent_updates.append((intent, updated_intent))
        conn.commit()
    return updated_candidate, tuple(intent_updates)


def _reassign_candidate(
    *,
    program: str,
    candidate_store: SourceCandidateStore,
    candidate: SourceCandidate,
    workstream_id: str,
    pm_alias: str,
    reason: str | None,
    from_intent_id: str | None,
) -> tuple[tuple[SourceIntent, SourceIntent], tuple[SourceIntent, SourceIntent]]:
    del program, reason
    current_time = datetime.now(timezone.utc)
    with candidate_store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        matches = candidate_store.get_intent_matches_with_conn(conn, candidate.candidate_id)
        intents = tuple(
            intent
            for match in matches
            for intent in (candidate_store.get_intent_with_conn(conn, match.intent_id),)
            if intent is not None
        )
        if not intents:
            conn.rollback()
            raise typer.BadParameter(
                f"Candidate '{candidate.candidate_id}' is not linked to any source intent."
            )
        if from_intent_id is not None:
            current_intent = next(
                (intent for intent in intents if intent.intent_id == from_intent_id),
                None,
            )
            if current_intent is None:
                conn.rollback()
                raise typer.BadParameter(
                    f"Candidate '{candidate.candidate_id}' is not linked to intent '{from_intent_id}'."
                )
        elif len(intents) > 1:
            conn.rollback()
            raise typer.BadParameter(
                f"Candidate '{candidate.candidate_id}' matches multiple intents; pass --from-intent-id to reassign one explicitly."
            )
        else:
            current_intent = intents[0]
        target_name = (
            current_intent.display_name if current_intent.display_name else (candidate.display_name or "")
        )
        target_intent = candidate_store.get_intent_by_name_with_conn(
            conn,
            workstream_id=workstream_id,
            ref_kind=candidate.ref_kind,
            display_name=target_name,
        )
        if target_intent is None:
            conn.rollback()
            raise typer.BadParameter(
                f"No {candidate.ref_kind.value} intent named '{target_name}' exists for workstream '{workstream_id}'."
            )
        if target_intent.intent_id == current_intent.intent_id:
            conn.commit()
            return (current_intent, current_intent), (target_intent, target_intent)
        previous_score = _intent_match_confidence_with_conn(
            conn,
            candidate_store,
            candidate.candidate_id,
            current_intent.intent_id,
        )
        candidate_store.unlink_candidate_from_intent_with_conn(
            conn,
            candidate.candidate_id,
            current_intent.intent_id,
        )
        candidate_store.link_candidate_to_intent_with_conn(
            conn,
            candidate.candidate_id,
            target_intent.intent_id,
            previous_score,
        )
        updated_old_intent = _update_intent_status_if_needed_with_conn(
            conn,
            candidate_store,
            current_intent,
            status=_recompute_intent_status_with_conn(
                conn,
                candidate_store,
                current_intent.intent_id,
                as_of=current_time,
            ),
            updated_by=pm_alias,
        )
        updated_new_intent = _update_intent_status_if_needed_with_conn(
            conn,
            candidate_store,
            target_intent,
            status=_recompute_intent_status_with_conn(
                conn,
                candidate_store,
                target_intent.intent_id,
                as_of=current_time,
            ),
            updated_by=pm_alias,
        )
        conn.commit()
    return (current_intent, updated_old_intent), (target_intent, updated_new_intent)


def _accept_candidate_for_intent_impl(
    *,
    program: str,
    programs_root: Path,
    candidate_store: SourceCandidateStore,
    intent: SourceIntent,
    candidate: SourceCandidate,
    pm_alias: str,
    reason: str | None,
    match_confidence: float,
    existing_candidate: bool,
    load_program: LoadProgramFn,
    store_factory: StoreFactory,
    unlinked_intent_ids: tuple[str, ...] = (),
) -> tuple[SourceCandidate, SourceIntent, tuple[tuple[SourceIntent, SourceIntent], ...]]:
    program_config = load_program(program, programs_root)
    channel = channel_for_source_ref_kind(intent.ref_kind)
    channel_config = _channel_config(program_config, channel, programs_root=programs_root)
    channel_store = store_factory(program, programs_root)
    current_time = datetime.now(timezone.utc)
    stale_updates: list[tuple[SourceIntent, SourceIntent]] = []
    channel_store = store_factory(program, programs_root, ensure_schema=False)
    with candidate_store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        live_intent = candidate_store.get_intent_with_conn(conn, intent.intent_id)
        if live_intent is None:
            conn.rollback()
            raise typer.BadParameter(f"Unknown source intent '{intent.intent_id}'.")
        if existing_candidate:
            resolved_candidate = candidate_store.update_candidate_status_with_conn(
                conn,
                candidate.candidate_id,
                status=SourceCandidateStatus.ACCEPTED,
                decided_by=pm_alias,
                decision_reason=reason,
                expected_decision_version=candidate.decision_version,
            )
        else:
            candidate_store.upsert_candidate_with_conn(conn, candidate, pii_prescrubbed=True)
            resolved_candidate = candidate
        candidate_store.link_candidate_to_intent_with_conn(
            conn,
            candidate.candidate_id,
            intent.intent_id,
            match_confidence,
        )
        for other_intent_id in unlinked_intent_ids:
            stale_before = candidate_store.get_intent_with_conn(conn, other_intent_id)
            if stale_before is None:
                continue
            candidate_store.unlink_candidate_from_intent_with_conn(
                conn,
                candidate.candidate_id,
                other_intent_id,
            )
            stale_after = _update_intent_status_if_needed_with_conn(
                conn,
                candidate_store,
                stale_before,
                status=_recompute_intent_status_with_conn(
                    conn,
                    candidate_store,
                    other_intent_id,
                    as_of=current_time,
                ),
                updated_by=pm_alias,
            )
            stale_updates.append((stale_before, stale_after))
        upsert_candidate_registration_with_conn(
            conn=conn,
            program_id=program,
            programs_root=programs_root,
            intent=live_intent,
            candidate=resolved_candidate,
            current_time=current_time,
            ttl_days=channel_config.ttl_days,
            scope_prefix="manual",
            auto_resolved=False,
            first_discovered_at=current_time,
        )
        updated_intent = candidate_store.update_intent_status_with_conn(
            conn,
            live_intent.intent_id,
            status=SourceIntentStatus.RESOLVED,
            updated_by=pm_alias,
            expected_decision_version=live_intent.decision_version,
        )
        conn.commit()
    return resolved_candidate, updated_intent, tuple(stale_updates)


def _reject_candidate_impl(
    *,
    program: str,
    programs_root: Path,
    candidate_store: SourceCandidateStore,
    candidate: SourceCandidate,
    pm_alias: str,
    reason: str,
    store_factory: StoreFactory,
) -> tuple[SourceCandidate, tuple[tuple[SourceIntent, SourceIntent], ...], bool]:
    channel_store = store_factory(program, programs_root)
    current_time = datetime.now(timezone.utc)
    intent_updates: list[tuple[SourceIntent, SourceIntent]] = []
    with candidate_store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        updated_candidate = candidate_store.update_candidate_status_with_conn(
            conn,
            candidate.candidate_id,
            status=SourceCandidateStatus.REJECTED,
            decided_by=pm_alias,
            decision_reason=reason,
            expected_decision_version=candidate.decision_version,
        )
        registration_exists = _registration_exists_with_conn(conn, channel_store, candidate)
        if registration_exists:
            channel_store._set_status(
                candidate.channel,
                candidate.ref_id,
                candidate.ref_kind.value,
                RegistrationStatus.SUPPRESSED,
                provider_instance_id=candidate.provider_instance_id,
                conn=conn,
            )
        for match in candidate_store.get_intent_matches_with_conn(conn, candidate.candidate_id):
            intent = candidate_store.get_intent_with_conn(conn, match.intent_id)
            if intent is None or intent.status in {
                SourceIntentStatus.SUPPRESSED,
                SourceIntentStatus.RETIRED,
                SourceIntentStatus.SUPERSEDED,
            }:
                continue
            updated_intent = _update_intent_status_if_needed_with_conn(
                conn,
                candidate_store,
                intent,
                status=_recompute_intent_status_with_conn(
                    conn,
                    candidate_store,
                    intent.intent_id,
                    as_of=current_time,
                ),
                updated_by=pm_alias,
            )
            intent_updates.append((intent, updated_intent))
        conn.commit()
    return updated_candidate, tuple(intent_updates), registration_exists


def _mutate_intent_lifecycle_impl(
    *,
    program: str,
    programs_root: Path,
    workstream_id: str,
    ref_kind: SourceRefKind,
    display_name: str,
    pm_alias: str,
    reason: str,
    target_status: SourceIntentStatus,
    store_factory: StoreFactory,
    bootstrap_discovery_state: BootstrapDiscoveryStateFn,
) -> None:
    candidate_store = _candidate_store(program, programs_root)
    channel_store = store_factory(program, programs_root, ensure_schema=False)
    bootstrap_discovery_state(
        program,
        programs_root=programs_root,
        candidate_store=candidate_store,
    )
    with candidate_store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        intent = candidate_store.get_intent_by_name_with_conn(
            conn,
            workstream_id=workstream_id,
            ref_kind=ref_kind,
            display_name=display_name,
        )
        if intent is None:
            conn.rollback()
            raise typer.BadParameter(
                f"Unknown source intent for workstream='{workstream_id}' kind='{ref_kind.value}' name='{display_name}'."
            )
        accepted_candidates = _accepted_candidates_for_intent_with_conn(
            conn,
            candidate_store,
            intent.intent_id,
        )
        updated_intent = candidate_store.update_intent_status_with_conn(
            conn,
            intent.intent_id,
            status=target_status,
            updated_by=pm_alias,
            expected_decision_version=intent.decision_version,
        )
        if accepted_candidates:
            registration_status = (
                RegistrationStatus.SUPPRESSED
                if target_status == SourceIntentStatus.SUPPRESSED
                else RegistrationStatus.RETIRED
            )
            for candidate in accepted_candidates:
                channel_store._set_status(
                    candidate.channel,
                    candidate.ref_id,
                    candidate.ref_kind.value,
                    registration_status,
                    provider_instance_id=candidate.provider_instance_id,
                    conn=conn,
                )
        conn.commit()
    append_intent_decision_log(
        program,
        programs_root=programs_root,
        payload=intent_decision_payload(
            ts=datetime.now(timezone.utc),
            intent=intent,
            action=f"intent_{target_status.value}",
            actor_alias=pm_alias,
            old_status=intent.status.value,
            new_status=updated_intent.status.value,
            reason=reason,
        ),
    )


def _restore_intent_lifecycle_impl(
    *,
    program: str,
    programs_root: Path,
    workstream_id: str,
    ref_kind: SourceRefKind,
    display_name: str,
    pm_alias: str,
    expected_status: SourceIntentStatus,
    action: str,
    load_program: LoadProgramFn,
    store_factory: StoreFactory,
    bootstrap_discovery_state: BootstrapDiscoveryStateFn,
) -> None:
    candidate_store = _candidate_store(program, programs_root)
    bootstrap_discovery_state(
        program,
        programs_root=programs_root,
        candidate_store=candidate_store,
    )
    channel_store = store_factory(program, programs_root, ensure_schema=False)
    current_time = datetime.now(timezone.utc)
    with candidate_store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        intent = candidate_store.get_intent_by_name_with_conn(
            conn,
            workstream_id=workstream_id,
            ref_kind=ref_kind,
            display_name=display_name,
        )
        if intent is None:
            conn.rollback()
            raise typer.BadParameter(
                f"Unknown source intent for workstream='{workstream_id}' kind='{ref_kind.value}' name='{display_name}'."
            )
        if intent.status != expected_status:
            conn.rollback()
            raise typer.BadParameter(
                f"Intent '{intent.intent_id}' is {intent.status.value}, not {expected_status.value}."
            )
        accepted_candidates = _accepted_candidates_for_intent_with_conn(
            conn,
            candidate_store,
            intent.intent_id,
        )
        next_status = _recompute_intent_status_with_conn(
            conn,
            candidate_store,
            intent.intent_id,
            as_of=current_time,
        )
        updated_intent = candidate_store.update_intent_status_with_conn(
            conn,
            intent.intent_id,
            status=next_status,
            updated_by=pm_alias,
            expected_decision_version=intent.decision_version,
        )
        if accepted_candidates and next_status == SourceIntentStatus.RESOLVED:
            program_config = load_program(program, programs_root)
            channel_config = _channel_config(
                program_config,
                channel_for_source_ref_kind(ref_kind),
                programs_root=programs_root,
            )
            for candidate in accepted_candidates:
                accepted_result = _accepted_candidate_result(
                    program=program,
                    intent=updated_intent,
                    candidate=candidate,
                    current_time=current_time,
                )
                upsert_candidate_registration_with_conn(
                    conn=conn,
                    program_id=program,
                    programs_root=programs_root,
                    intent=updated_intent,
                    candidate=candidate,
                    current_time=current_time,
                    ttl_days=channel_config.ttl_days,
                    scope_prefix="manual",
                    auto_resolved=False,
                    first_discovered_at=current_time,
                )
        conn.commit()
    append_intent_decision_log(
        program,
        programs_root=programs_root,
        payload=intent_decision_payload(
            ts=current_time,
            intent=intent,
            action=action,
            actor_alias=pm_alias,
            old_status=intent.status.value,
            new_status=updated_intent.status.value,
            reason=None,
        ),
    )


def _accepted_candidate_result(
    *,
    program: str,
    intent: SourceIntent,
    candidate: SourceCandidate,
    current_time: datetime,
) -> DiscoveryResult:
    return build_accepted_candidate_result(
        program_id=program,
        intent=intent,
        candidate=candidate,
        current_time=current_time,
        scope_prefix="manual",
        auto_resolved=False,
        first_discovered_at=current_time,
    )
