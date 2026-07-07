from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.commands.doctor_checks.models import DoctorCheck
from src.core.exceptions import ConfigError
from src.core.keyword_topic_router import suggest_keyword_expansions
from src.core.m365_registry_store import (
    M365RegistryArtifact,
    describe_current_m365_registry_promotion_blockers,
    is_current_m365_registry_promotion_candidate,
    load_m365_registry,
    read_m365_routing_feedback_events,
)
from src.core.m365_signal_corpus import build_m365_corpus_texts_by_workstream, load_approved_m365_corpus_signals
from src.core.program_fact_store import load_current_workstreams


def m365_discovery_check(entry: dict[str, Any], *, previous_entry: dict[str, Any] | None = None) -> DoctorCheck:
    metadata = {key: value for key, value in entry.items()}
    if not bool(entry.get("active")):
        reason = str(entry.get("reason_not_active") or "inactive")
        return DoctorCheck("M365 Discovery", "ok", f"Inactive on the latest gather ({reason}).", metadata=metadata)

    issues: list[str] = []
    first_discovery_completed_at = str(entry.get("first_discovery_completed_at") or "").strip()
    if not bool(entry.get("registry_bootstrapped")):
        issues.append("registry bootstrap missing")
    untracked_threads = int(entry.get("untracked_observed_thread_ids") or 0)
    if untracked_threads > 0:
        issues.append(f"{untracked_threads} observed thread(s) are not yet tracked")
    signals_without_workstream = int(entry.get("signals_without_workstream") or 0)
    if signals_without_workstream > 0:
        issues.append(f"{signals_without_workstream} WorkIQ signal(s) lack workstream attribution")
    chat_thread_id_null = int(entry.get("chat_thread_id_null") or 0)
    if chat_thread_id_null > 0:
        issues.append(f"{chat_thread_id_null} configured chat(s) are missing thread_id")
    promotion_blocked_missing_id_count = int(entry.get("promotion_blocked_missing_id_count") or 0)
    if promotion_blocked_missing_id_count > 0:
        issues.append(f"{promotion_blocked_missing_id_count} PM-confirmed artifact(s) are promotion-blocked by missing series_id/thread_id")
    discovery_last_error = str(entry.get("discovery_last_error") or "").strip()
    if discovery_last_error:
        issues.append(f"discovery runtime failure: {discovery_last_error}")
    if first_discovery_completed_at:
        issues.append(f"first active discovery completed at {first_discovery_completed_at}")
    comparison_summary = summarize_m365_discovery_comparison(entry, previous_entry)

    if not issues:
        detail = "Registry bootstrap present; observed M365 artifacts are attributed and tracked."
        if comparison_summary:
            detail = f"{detail} {comparison_summary}"
        return DoctorCheck(
            "M365 Discovery",
            "ok",
            detail,
            metadata=metadata,
        )
    detail = "; ".join(issues) + "."
    if comparison_summary:
        detail = f"{detail} {comparison_summary}"
    return DoctorCheck("M365 Discovery", "warn", detail, metadata=metadata)


def summarize_m365_discovery(entry: dict[str, Any]) -> str:
    if not bool(entry.get("active")):
        return ""
    fragments: list[str] = []
    first_discovery_completed_at = str(entry.get("first_discovery_completed_at") or "").strip()
    if not bool(entry.get("registry_bootstrapped")):
        fragments.append("M365 registry bootstrap missing")
    untracked_threads = int(entry.get("untracked_observed_thread_ids") or 0)
    if untracked_threads > 0:
        fragments.append(f"Untracked M365 threads: {untracked_threads}")
    signals_without_workstream = int(entry.get("signals_without_workstream") or 0)
    if signals_without_workstream > 0:
        fragments.append(f"WorkIQ signals without workstream: {signals_without_workstream}")
    promotion_blocked_missing_id_count = int(entry.get("promotion_blocked_missing_id_count") or 0)
    if promotion_blocked_missing_id_count > 0:
        fragments.append(f"PM-confirmed artifacts blocked on missing IDs: {promotion_blocked_missing_id_count}")
    seeded_outcome_counts = entry.get("seeded_resolution_outcome_counts") or {}
    if isinstance(seeded_outcome_counts, dict):
        no_candidate_count = int(seeded_outcome_counts.get("no_candidates") or 0)
        if no_candidate_count > 0:
            fragments.append(f"Seeded source intents with completed no-candidate attempts: {no_candidate_count}")
    broad_query_count = int(entry.get("broad_query_count") or 0)
    if broad_query_count > 0:
        fragments.append(f"Broad M365 discovery queries executed: {broad_query_count}")
    discovery_last_error = str(entry.get("discovery_last_error") or "").strip()
    if discovery_last_error:
        fragments.append(f"WorkIQ discovery failure: {discovery_last_error}")
    if first_discovery_completed_at:
        fragments.append(f"First active discovery completed at {first_discovery_completed_at}")
    if not fragments:
        return ""
    return ". ".join(fragments) + "."


def build_m365_registry_review_metadata(
    program_id: str,
    *,
    programs_root: Path,
    load_m365_registry_fn: Callable[..., Any] = load_m365_registry,
    read_m365_routing_feedback_events_fn: Callable[..., Any] = read_m365_routing_feedback_events,
    load_current_workstreams_fn: Callable[..., Any] = load_current_workstreams,
    load_approved_m365_corpus_signals_fn: Callable[..., Any] = load_approved_m365_corpus_signals,
    build_m365_corpus_texts_by_workstream_fn: Callable[..., Any] = build_m365_corpus_texts_by_workstream,
    suggest_keyword_expansions_fn: Callable[..., Any] = suggest_keyword_expansions,
    describe_current_m365_registry_promotion_blockers_fn: Callable[..., Any] = describe_current_m365_registry_promotion_blockers,
    is_current_m365_registry_promotion_candidate_fn: Callable[..., Any] = is_current_m365_registry_promotion_candidate,
) -> dict[str, Any]:
    registry = load_m365_registry_fn(program_id, programs_root)
    artifacts = registry.artifacts
    feedback_events = read_m365_routing_feedback_events_fn(program_id, programs_root)
    workstreams_path = programs_root / program_id / "workstreams.yaml"
    if not workstreams_path.exists():
        raise ConfigError(f"Missing workstreams.yaml for program '{program_id}'.")
    workstreams = load_current_workstreams_fn(program_id, programs_root=programs_root)
    as_of = registry.last_updated or datetime.now(timezone.utc)
    approved_m365_signals = load_approved_m365_corpus_signals_fn(program_id, as_of=as_of, programs_root=programs_root)
    medium_review_ids: list[str] = []
    unclassified_ids: list[str] = []
    missing_id_ids: list[str] = []
    missing_id_artifacts: list[dict[str, Any]] = []
    promotion_candidate_ids: list[str] = []
    promotion_blocked_recent_rejection_ids: list[str] = []
    promotion_blocked_missing_id_ids: list[str] = []
    promotion_blocked_signal_yield_ids: list[str] = []
    rejected_ids: list[str] = []

    for artifact in artifacts:
        promotion_blockers = describe_current_m365_registry_promotion_blockers_fn(
            artifact,
            feedback_events=feedback_events,
            as_of=as_of,
        )
        if artifact_is_missing_m365_id(artifact):
            missing_id_ids.append(artifact.artifact_id)
            missing_id_artifacts.append(
                {
                    "artifact_id": artifact.artifact_id,
                    "artifact_type": artifact.artifact_type,
                    "inferred_workstream": artifact.inferred_workstream,
                }
            )
        if is_current_m365_registry_promotion_candidate_fn(artifact, feedback_events=feedback_events, as_of=as_of):
            promotion_candidate_ids.append(artifact.artifact_id)
            continue
        if artifact.pm_confirmed or "insufficient_confidence" not in promotion_blockers:
            if "recent_rejection" in promotion_blockers:
                promotion_blocked_recent_rejection_ids.append(artifact.artifact_id)
            elif "missing_required_id" in promotion_blockers:
                promotion_blocked_missing_id_ids.append(artifact.artifact_id)
            elif "insufficient_signal_yield" in promotion_blockers:
                promotion_blocked_signal_yield_ids.append(artifact.artifact_id)
        if artifact.confidence_source == "pm_rejected" or artifact.confidence < 0.40:
            rejected_ids.append(artifact.artifact_id)
            continue
        if artifact.pm_confirmed:
            continue
        if artifact.confidence >= 0.60:
            medium_review_ids.append(artifact.artifact_id)
            continue
        unclassified_ids.append(artifact.artifact_id)

    corpus_texts_by_workstream = build_m365_corpus_texts_by_workstream_fn(
        workstreams=workstreams,
        registry_artifacts=artifacts,
        feedback_events=feedback_events,
        approved_signals=approved_m365_signals,
        as_of=as_of,
    )

    keyword_suggestions_by_workstream: dict[str, list[str]] = {}
    for workstream in workstreams:
        signal_sources = workstream.signal_sources
        existing_keywords = signal_sources.workiq_keywords if signal_sources is not None else ()
        suggestions = suggest_keyword_expansions_fn(
            existing_keywords=existing_keywords,
            texts=corpus_texts_by_workstream.get(workstream.id, ()),
        )
        if suggestions:
            keyword_suggestions_by_workstream[workstream.id] = list(suggestions)

    return {
        "artifact_count": len(artifacts),
        "medium_review_ids": medium_review_ids,
        "medium_review_count": len(medium_review_ids),
        "unclassified_ids": unclassified_ids,
        "unclassified_count": len(unclassified_ids),
        "missing_id_ids": missing_id_ids,
        "missing_id_artifacts": missing_id_artifacts,
        "missing_id_count": len(missing_id_ids),
        "promotion_candidate_ids": promotion_candidate_ids,
        "promotion_candidate_count": len(promotion_candidate_ids),
        "promotion_blocked_recent_rejection_ids": promotion_blocked_recent_rejection_ids,
        "promotion_blocked_recent_rejection_count": len(promotion_blocked_recent_rejection_ids),
        "promotion_blocked_missing_id_ids": promotion_blocked_missing_id_ids,
        "promotion_blocked_missing_id_count": len(promotion_blocked_missing_id_ids),
        "promotion_blocked_signal_yield_ids": promotion_blocked_signal_yield_ids,
        "promotion_blocked_signal_yield_count": len(promotion_blocked_signal_yield_ids),
        "rejected_ids": rejected_ids,
        "rejected_count": len(rejected_ids),
        "keyword_suggestions_by_workstream": keyword_suggestions_by_workstream,
        "keyword_suggestion_count": sum(len(suggestions) for suggestions in keyword_suggestions_by_workstream.values()),
        "approved_m365_signal_corpus_count": len(approved_m365_signals),
        "has_issues": bool(
            medium_review_ids
            or unclassified_ids
            or missing_id_ids
            or promotion_blocked_signal_yield_ids
        ),
    }


def artifact_is_missing_m365_id(artifact: M365RegistryArtifact) -> bool:
    if not artifact.pm_confirmed:
        return False
    if artifact.artifact_type == "meeting_series":
        return artifact.series_id is None
    return artifact.thread_id is None


def summarize_m365_registry_review(metadata: dict[str, Any]) -> str:
    fragments: list[str] = []
    medium_review_count = int(metadata.get("medium_review_count") or 0)
    if medium_review_count > 0:
        fragments.append(f"M365 review queue: {medium_review_count} medium-confidence artifact(s)")
    unclassified_count = int(metadata.get("unclassified_count") or 0)
    if unclassified_count > 0:
        fragments.append(f"{unclassified_count} unclassified artifact(s)")
    missing_id_count = int(metadata.get("missing_id_count") or 0)
    if missing_id_count > 0:
        fragments.append(f"{missing_id_count} confirmed artifact(s) missing IDs")
    blocked_signal_yield_count = int(metadata.get("promotion_blocked_signal_yield_count") or 0)
    if blocked_signal_yield_count > 0:
        fragments.append(f"{blocked_signal_yield_count} artifact(s) blocked on recent signal yield")
    promotion_candidate_count = int(metadata.get("promotion_candidate_count") or 0)
    if promotion_candidate_count > 0:
        fragments.append(f"{promotion_candidate_count} artifact(s) ready for current promotion")
    if not fragments:
        return ""
    return "; ".join(fragments) + "."


def m365_registry_review_check(metadata: dict[str, Any]) -> DoctorCheck:
    medium_review_count = int(metadata.get("medium_review_count") or 0)
    unclassified_count = int(metadata.get("unclassified_count") or 0)
    missing_id_count = int(metadata.get("missing_id_count") or 0)
    blocked_signal_yield_count = int(metadata.get("promotion_blocked_signal_yield_count") or 0)
    detail_fragments: list[str] = []
    if medium_review_count > 0:
        detail_fragments.append(f"{medium_review_count} medium-confidence artifact(s) need PM review")
    if unclassified_count > 0:
        detail_fragments.append(f"{unclassified_count} artifact(s) are in the UNCLASSIFIED band")
    if missing_id_count > 0:
        detail_fragments.append(f"{missing_id_count} PM-confirmed artifact(s) are missing series_id/thread_id")
    if blocked_signal_yield_count > 0:
        detail_fragments.append(f"{blocked_signal_yield_count} artifact(s) are blocked on recent signal yield")
    keyword_suggestions_by_workstream = metadata.get("keyword_suggestions_by_workstream") or {}
    if isinstance(keyword_suggestions_by_workstream, dict) and keyword_suggestions_by_workstream:
        suggestion_fragments = [
            f"{workstream_id}: {', '.join(str(value) for value in suggestions)}"
            for workstream_id, suggestions in sorted(keyword_suggestions_by_workstream.items())
            if isinstance(suggestions, list) and suggestions
        ]
        if suggestion_fragments:
            detail_fragments.append("keyword expansion suggestions -> " + "; ".join(suggestion_fragments))
    if not detail_fragments:
        detail = "Registry artifacts are either confirmed, high-confidence, or explicitly rejected."
        return DoctorCheck("M365 Registry Review", "ok", detail, metadata=metadata)
    return DoctorCheck("M365 Registry Review", "warn", "; ".join(detail_fragments) + ".", metadata=metadata)


def m365_registry_promotion_check(metadata: dict[str, Any]) -> DoctorCheck:
    promotion_candidate_count = int(metadata.get("promotion_candidate_count") or 0)
    blocked_recent_rejection_count = int(metadata.get("promotion_blocked_recent_rejection_count") or 0)
    blocked_missing_id_count = int(metadata.get("promotion_blocked_missing_id_count") or 0)
    blocked_signal_yield_count = int(metadata.get("promotion_blocked_signal_yield_count") or 0)
    if (
        promotion_candidate_count == 0
        and blocked_recent_rejection_count == 0
        and blocked_missing_id_count == 0
        and blocked_signal_yield_count == 0
    ):
        return DoctorCheck(
            "M365 Registry Promotion",
            "ok",
            "No eligible artifacts are waiting for current promotion.",
            metadata=metadata,
        )
    detail_fragments: list[str] = []
    if promotion_candidate_count > 0:
        detail_fragments.append(
            f"{promotion_candidate_count} eligible artifact(s) are ready for current promotion via 'vertex registry promote'"
        )
    if blocked_recent_rejection_count > 0:
        detail_fragments.append(
            f"{blocked_recent_rejection_count} artifact(s) are promotion-blocked by recent rejection"
        )
    if blocked_missing_id_count > 0:
        detail_fragments.append(
            f"{blocked_missing_id_count} artifact(s) are promotion-blocked by missing series_id/thread_id"
        )
    if blocked_signal_yield_count > 0:
        detail_fragments.append(
            f"{blocked_signal_yield_count} artifact(s) are promotion-blocked by insufficient recent signal yield"
        )
    detail = "; ".join(detail_fragments) + "."
    return DoctorCheck("M365 Registry Promotion", "warn", detail, metadata=metadata)


def summarize_m365_discovery_comparison(entry: dict[str, Any], previous_entry: dict[str, Any] | None) -> str:
    if not previous_entry:
        return ""
    fragments: list[str] = []
    for field, label in (
        ("observed_thread_ids", "observed thread ids"),
        ("untracked_observed_thread_ids", "untracked threads"),
        ("signals_without_workstream", "unattributed WorkIQ signals"),
    ):
        current_value = int(entry.get(field) or 0)
        previous_value = int(previous_entry.get(field) or 0)
        if current_value != previous_value:
            fragments.append(f"{label} {previous_value} -> {current_value}")
    previous_bootstrapped = bool(previous_entry.get("registry_bootstrapped"))
    current_bootstrapped = bool(entry.get("registry_bootstrapped"))
    if previous_bootstrapped != current_bootstrapped:
        fragments.append(f"registry bootstrap {previous_bootstrapped} -> {current_bootstrapped}")
    if not fragments:
        return ""
    return "Previous run: " + "; ".join(fragments) + "."
