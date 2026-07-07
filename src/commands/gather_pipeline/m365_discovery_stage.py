from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from src.commands.gather_pipeline.models import (
    M365DiscoveryStageInput,
    M365DiscoveryStageResult,
    M365PromotionBlockedArtifact,
    M365PromotionCandidate,
)
from src.core.m365_registry_store import (
    M365RegistryArtifact,
    describe_current_m365_registry_promotion_blockers,
    is_current_m365_registry_promotion_candidate,
    load_m365_registry,
    read_m365_routing_feedback_events,
)
from src.core.program_paths import resolve_m365_registry_path_for_read
from src.core.models import WorkItem
from src.core.models_v2 import IntegrationError, Program, Signal, Workstream


def run_m365_discovery_stage(stage_input: M365DiscoveryStageInput) -> M365DiscoveryStageResult:
    registry_artifacts = (
        load_m365_registry(stage_input.program_id, stage_input.programs_root).artifacts
        if stage_input.program.m365 is not None and stage_input.program.m365.enabled
        else ()
    )
    feedback_events = (
        read_m365_routing_feedback_events(stage_input.program_id, stage_input.programs_root)
        if stage_input.program.m365 is not None and stage_input.program.m365.enabled
        else ()
    )
    promotion_candidates = build_current_m365_promotion_candidates(
        registry_artifacts=registry_artifacts,
        gather_flags=stage_input.gather_flags,
        feedback_events=feedback_events,
        as_of=stage_input.as_of,
    )
    promotion_blocked_artifacts = build_current_m365_promotion_blocked_artifacts(
        registry_artifacts=registry_artifacts,
        gather_flags=stage_input.gather_flags,
        feedback_events=feedback_events,
        as_of=stage_input.as_of,
    )
    m365_discovery_state = build_m365_discovery_state(
        program_id=stage_input.program_id,
        programs_root=stage_input.programs_root,
        program=stage_input.program,
        workstreams=stage_input.workstreams,
        items=stage_input.items,
        workiq_signals=stage_input.workiq_signals,
        gather_flags=stage_input.gather_flags,
        integration_error_details=stage_input.integration_error_details,
        registry_artifacts=registry_artifacts,
        feedback_events=feedback_events,
        as_of=stage_input.as_of,
        previous_entry=stage_input.previous_entry,
        count_transcript_series_state=stage_input.count_transcript_series_state,
        count_chat_thread_state=stage_input.count_chat_thread_state,
        tracked_m365_artifact_ids=stage_input.tracked_m365_artifact_ids,
        observed_m365_thread_ids=stage_input.observed_m365_thread_ids,
        load_discovery_milestones=stage_input.load_discovery_milestones,
        build_workiq_query_plans=stage_input.build_workiq_query_plans,
        build_m365_discovery_queries=stage_input.build_m365_discovery_queries,
        build_seeded_source_discovery_state=stage_input.build_seeded_source_discovery_state,
        build_adaptive_workiq_state=stage_input.build_adaptive_workiq_state,
    )
    return M365DiscoveryStageResult(
        m365_discovery_state=m365_discovery_state,
        promotion_candidates=promotion_candidates,
        promotion_blocked_artifacts=promotion_blocked_artifacts,
    )


def build_m365_discovery_state(
    *,
    program_id: str,
    programs_root: Path,
    program: Program,
    workstreams: tuple[Workstream, ...],
    workiq_signals: tuple[Signal, ...],
    gather_flags: dict[str, bool],
    items: tuple[WorkItem, ...] = (),
    integration_error_details: tuple[IntegrationError, ...] = (),
    registry_artifacts: tuple[M365RegistryArtifact, ...] | None = None,
    feedback_events: tuple[Any, ...] = (),
    as_of: datetime | None = None,
    previous_entry: dict[str, Any] | None = None,
    count_transcript_series_state,
    count_chat_thread_state,
    tracked_m365_artifact_ids,
    observed_m365_thread_ids,
    load_discovery_milestones,
    build_workiq_query_plans,
    build_m365_discovery_queries,
    build_seeded_source_discovery_state,
    build_adaptive_workiq_state,
) -> dict[str, Any]:
    configured_series, series_id_null = count_transcript_series_state(workstreams)
    configured_chats, chat_thread_id_null = count_chat_thread_state(workstreams)
    milestones = load_discovery_milestones(program_id, programs_root)
    if registry_artifacts is None:
        registry_artifacts = load_m365_registry(program_id, programs_root).artifacts if program.m365 is not None and program.m365.enabled else ()
    tracked_ids = tracked_m365_artifact_ids(
        workstreams,
        registry_artifacts,
        feedback_events=feedback_events,
        as_of=as_of,
    )
    observed_thread_ids = observed_m365_thread_ids(workiq_signals)
    registry_path = resolve_m365_registry_path_for_read(program_id, programs_root=programs_root)
    feedback_path = programs_root / program_id / "_feedback" / "m365_routing_feedback.jsonl"
    query_plan_count = (
        len(
            build_workiq_query_plans(
                program=program,
                workstreams=workstreams,
                items=items,
                milestones=milestones,
                registry_artifacts=registry_artifacts,
                feedback_events=feedback_events,
                as_of=as_of,
            )
        )
        if program.m365 is not None and program.m365.enabled
        else 0
    )
    broad_discovery_queries = (
        list(
            build_m365_discovery_queries(
                workstreams=workstreams,
                registry_artifacts=registry_artifacts,
                feedback_events=feedback_events,
                as_of=as_of,
            )
        )
        if program.m365 is not None and program.m365.enabled
        else []
    )
    promotion_candidate_ids = [
        artifact.artifact_id
        for artifact in registry_artifacts
        if gather_flags.get("workiq", False)
        and is_current_m365_registry_promotion_candidate(artifact, feedback_events=feedback_events, as_of=as_of)
    ]
    promotion_blocked_artifacts = build_current_m365_promotion_blocked_artifacts(
        registry_artifacts=registry_artifacts,
        gather_flags=gather_flags,
        feedback_events=feedback_events,
        as_of=as_of,
    )
    promotion_blocked_recent_rejection_ids = [
        artifact.artifact_id for artifact in promotion_blocked_artifacts if artifact.blocker_reason == "recent rejection"
    ]
    promotion_blocked_missing_id_ids = [
        artifact.artifact_id for artifact in promotion_blocked_artifacts if artifact.blocker_reason == "missing series_id/thread_id"
    ]
    promotion_blocked_missing_id_artifacts = [
        {
            "artifact_id": artifact.artifact_id,
            "artifact_type": artifact.artifact_type,
            "inferred_workstream": artifact.workstream_id,
        }
        for artifact in promotion_blocked_artifacts
        if artifact.blocker_reason == "missing series_id/thread_id"
    ]
    promotion_blocked_signal_yield_ids = [
        artifact.artifact_id for artifact in promotion_blocked_artifacts if artifact.blocker_reason == "insufficient recent signal yield"
    ]
    discovery_last_error = next(
        (
            detail.message
            for detail in reversed(integration_error_details)
            if detail.source == "workiq" and detail.stage == "discovery"
        ),
        None,
    )
    seeded_resolution_state = build_seeded_source_discovery_state(
        program_id=program_id,
        programs_root=programs_root,
        as_of=as_of,
    )
    first_discovery_completed_at = (
        str(previous_entry.get("first_discovery_completed_at") or "").strip()
        if isinstance(previous_entry, dict)
        else ""
    )
    if not first_discovery_completed_at and seeded_resolution_state["first_attempted_at"] is not None:
        first_discovery_completed_at = str(seeded_resolution_state["first_attempted_at"])
    adaptive_learning_state = build_adaptive_workiq_state(
        program_id=program_id,
        programs_root=programs_root,
        workstreams=workstreams,
        items=items,
        milestones=milestones,
        registry_artifacts=registry_artifacts,
        feedback_events=feedback_events,
        as_of=as_of,
    )
    return {
        "active": gather_flags.get("workiq", False),
        "reason_not_active": None if gather_flags.get("workiq", False) else "flag_not_passed",
        "first_discovery_completed_at": first_discovery_completed_at or None,
        "query_plan_count": query_plan_count,
        "broad_query_count": len(broad_discovery_queries),
        "broad_queries": broad_discovery_queries[:12],
        "configured_series": configured_series,
        "series_id_null": series_id_null,
        "configured_chats": configured_chats,
        "chat_thread_id_null": chat_thread_id_null,
        "observed_thread_ids": len(observed_thread_ids),
        "untracked_observed_thread_ids": len(observed_thread_ids - tracked_ids),
        "signals_without_thread_id": sum(1 for signal in workiq_signals if signal_thread_id(signal) is None),
        "signals_without_workstream": sum(1 for signal in workiq_signals if signal.workstream_id is None),
        "registry_file_present": registry_path.exists(),
        "feedback_file_present": feedback_path.exists(),
        "registry_bootstrapped": registry_path.exists() and feedback_path.exists(),
        "discovery_last_error": discovery_last_error,
        "promotion_candidate_count": len(promotion_candidate_ids),
        "promotion_candidate_ids": promotion_candidate_ids,
        "promotion_blocked_recent_rejection_count": len(promotion_blocked_recent_rejection_ids),
        "promotion_blocked_recent_rejection_ids": promotion_blocked_recent_rejection_ids,
        "promotion_blocked_missing_id_count": len(promotion_blocked_missing_id_ids),
        "promotion_blocked_missing_id_ids": promotion_blocked_missing_id_ids,
        "promotion_blocked_missing_id_artifacts": promotion_blocked_missing_id_artifacts,
        "promotion_blocked_signal_yield_count": len(promotion_blocked_signal_yield_ids),
        "promotion_blocked_signal_yield_ids": promotion_blocked_signal_yield_ids,
        "adaptive_learning": adaptive_learning_state,
        "seeded_resolution_intent_count": seeded_resolution_state["intent_count"],
        "seeded_resolution_attempt_count": seeded_resolution_state["attempt_count"],
        "seeded_resolution_attempted_intent_count": seeded_resolution_state["attempted_intent_count"],
        "seeded_resolution_candidate_count": seeded_resolution_state["candidate_count"],
        "seeded_resolution_pending_candidate_count": seeded_resolution_state["pending_candidate_count"],
        "seeded_resolution_latest_attempted_at": seeded_resolution_state["latest_attempted_at"],
        "seeded_resolution_outcome_counts": seeded_resolution_state["outcome_counts"],
        "seeded_resolution_latest_attempts": seeded_resolution_state["latest_attempts"],
    }


def build_current_m365_promotion_candidates(
    *,
    registry_artifacts: tuple[M365RegistryArtifact, ...],
    gather_flags: dict[str, bool],
    feedback_events: tuple[Any, ...] = (),
    as_of: datetime | None = None,
) -> tuple[M365PromotionCandidate, ...]:
    if not gather_flags.get("workiq", False):
        return ()
    return tuple(
        M365PromotionCandidate(
            artifact_id=artifact.artifact_id,
            display_name=artifact.display_name,
            workstream_id=artifact.inferred_workstream,
            confidence=artifact.confidence,
            signal_yield_last_3=artifact.signal_yield_last_3,
        )
        for artifact in registry_artifacts
        if is_current_m365_registry_promotion_candidate(artifact, feedback_events=feedback_events, as_of=as_of)
    )


def build_current_m365_promotion_blocked_artifacts(
    *,
    registry_artifacts: tuple[M365RegistryArtifact, ...],
    gather_flags: dict[str, bool],
    feedback_events: tuple[Any, ...] = (),
    as_of: datetime | None = None,
) -> tuple[M365PromotionBlockedArtifact, ...]:
    if not gather_flags.get("workiq", False):
        return ()
    blocked_artifacts: list[M365PromotionBlockedArtifact] = []
    for artifact in registry_artifacts:
        if is_current_m365_registry_promotion_candidate(artifact, feedback_events=feedback_events, as_of=as_of):
            continue
        promotion_blockers = describe_current_m365_registry_promotion_blockers(
            artifact,
            feedback_events=feedback_events,
            as_of=as_of,
        )
        if artifact.pm_confirmed or "insufficient_confidence" not in promotion_blockers:
            blocker_reason: str | None = None
            if "recent_rejection" in promotion_blockers:
                blocker_reason = "recent rejection"
            elif "missing_required_id" in promotion_blockers:
                blocker_reason = "missing series_id/thread_id"
            elif "insufficient_signal_yield" in promotion_blockers:
                blocker_reason = "insufficient recent signal yield"
            if blocker_reason is None:
                continue
            blocked_artifacts.append(
                M365PromotionBlockedArtifact(
                    artifact_id=artifact.artifact_id,
                    artifact_type=artifact.artifact_type,
                    display_name=artifact.display_name,
                    workstream_id=artifact.inferred_workstream,
                    blocker_reason=blocker_reason,
                )
            )
    return tuple(blocked_artifacts)


def signal_thread_id(signal: Signal) -> str | None:
    if signal.thread_id is not None and signal.thread_id.strip():
        return signal.thread_id.strip()
    metadata = signal.metadata
    if isinstance(metadata, dict):
        for key in ("thread_id", "conversation_id", "meeting_id", "series_id"):
            value = metadata.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
    return None
