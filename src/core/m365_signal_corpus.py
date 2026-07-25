from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from src.core.journal import PROGRAMS_ROOT
from src.core.m365_registry_store import M365RegistryArtifact, M365RoutingFeedbackEvent
from src.core.m365_router_interface import M365ReassignCorrection
from src.core.models_v2 import Signal, Workstream
from src.core.signal_review import signal_is_approved_for_evidence
from src.core.store_factory import build_signal_store_for_program_id


_DEFAULT_M365_ROUTING_CORPUS_WINDOW_DAYS = 30


def build_m365_corpus_texts_by_workstream(
    *,
    workstreams: tuple[Workstream, ...],
    registry_artifacts: tuple[M365RegistryArtifact, ...],
    feedback_events: tuple[M365RoutingFeedbackEvent, ...],
    approved_signals: tuple[Signal, ...],
    as_of: datetime | None = None,
    window_days: int = _DEFAULT_M365_ROUTING_CORPUS_WINDOW_DAYS,
) -> dict[str, tuple[str, ...]]:
    artifacts_by_id = _artifacts_by_known_id(registry_artifacts)
    corpus_texts_by_workstream: dict[str, tuple[str, ...]] = {}

    for workstream in workstreams:
        corpus_texts: list[str] = []
        corpus_texts.extend(
            text
            for artifact in registry_artifacts
            if artifact.pm_confirmed
            and artifact.inferred_workstream == workstream.id
            and _artifact_is_within_window(artifact, as_of=as_of, window_days=window_days)
            for text in (artifact.display_name, artifact.routing_reasoning)
            if text
        )
        corpus_texts.extend(
            event.reason
            for event in feedback_events
            if event.action in {"confirm", "reassign"}
            and event.reason
            and _feedback_event_is_within_window(event, as_of=as_of, window_days=window_days)
            and artifacts_by_id.get(event.artifact_id) is not None
            and artifacts_by_id[event.artifact_id].inferred_workstream == workstream.id
        )
        corpus_texts.extend(
            signal.text
            for signal in approved_signals
            if workstream.id in signal.workstream_ids and signal.text
        )
        if corpus_texts:
            corpus_texts_by_workstream[workstream.id] = tuple(corpus_texts)

    return corpus_texts_by_workstream


def build_m365_rejected_texts_by_workstream(
    *,
    workstreams: tuple[Workstream, ...],
    registry_artifacts: tuple[M365RegistryArtifact, ...],
    feedback_events: tuple[M365RoutingFeedbackEvent, ...],
    as_of: datetime | None = None,
    window_days: int = _DEFAULT_M365_ROUTING_CORPUS_WINDOW_DAYS,
) -> dict[str, tuple[str, ...]]:
    rejected_artifacts_by_id = {
        artifact.artifact_id: artifact
        for artifact in registry_artifacts
        if artifact.confidence_source == "pm_rejected"
        and _artifact_is_within_window(artifact, as_of=as_of, window_days=window_days)
    }
    artifacts_by_id = _artifacts_by_known_id(registry_artifacts)
    rejected_texts_by_workstream: dict[str, tuple[str, ...]] = {}

    for workstream in workstreams:
        rejected_texts: list[str] = []
        rejected_texts.extend(
            text
            for artifact in rejected_artifacts_by_id.values()
            if artifact.inferred_workstream == workstream.id
            for text in (artifact.display_name, artifact.routing_reasoning)
            if text
        )
        rejected_texts.extend(
            event.reason
            for event in feedback_events
            if event.action == "reject"
            and event.reason
            and _feedback_event_is_within_window(event, as_of=as_of, window_days=window_days)
            and rejected_artifacts_by_id.get(event.artifact_id) is not None
            and rejected_artifacts_by_id[event.artifact_id].inferred_workstream == workstream.id
        )
        rejected_texts.extend(
            text
            for event in feedback_events
            if event.action == "reassign"
            and event.prior_workstream_id == workstream.id
            and _feedback_event_is_within_window(event, as_of=as_of, window_days=window_days)
            and artifacts_by_id.get(event.artifact_id) is not None
            for text in (artifacts_by_id[event.artifact_id].display_name, event.reason)
            if text
        )
        if rejected_texts:
            rejected_texts_by_workstream[workstream.id] = tuple(rejected_texts)

    return rejected_texts_by_workstream


def build_m365_reassign_corrections_by_workstream(
    *,
    workstreams: tuple[Workstream, ...],
    registry_artifacts: tuple[M365RegistryArtifact, ...],
    feedback_events: tuple[M365RoutingFeedbackEvent, ...],
    as_of: datetime | None = None,
    window_days: int = _DEFAULT_M365_ROUTING_CORPUS_WINDOW_DAYS,
) -> dict[str, tuple[M365ReassignCorrection, ...]]:
    workstream_ids = {workstream.id for workstream in workstreams}
    artifacts_by_id = _artifacts_by_known_id(registry_artifacts)
    corrections_by_workstream: dict[str, tuple[M365ReassignCorrection, ...]] = {}

    for workstream in workstreams:
        corrections: list[M365ReassignCorrection] = []
        for event in feedback_events:
            if event.action != "reassign":
                continue
            if event.workstream_id != workstream.id or event.prior_workstream_id is None:
                continue
            if event.prior_workstream_id not in workstream_ids:
                continue
            if not _feedback_event_is_within_window(event, as_of=as_of, window_days=window_days):
                continue
            artifact = artifacts_by_id.get(event.artifact_id)
            corrections.append(
                M365ReassignCorrection(
                    prior_workstream_id=event.prior_workstream_id,
                    corrected_workstream_id=workstream.id,
                    artifact_display_name=artifact.display_name if artifact is not None else None,
                    reason=event.reason,
                )
            )
        if corrections:
            corrections_by_workstream[workstream.id] = tuple(corrections)

    return corrections_by_workstream


def load_approved_m365_corpus_signals(
    program_id: str,
    *,
    as_of: datetime,
    programs_root: Path = PROGRAMS_ROOT,
    window_days: int = 30,
) -> tuple[Signal, ...]:
    signal_store = build_signal_store_for_program_id(program_id, programs_root=programs_root)
    review_states = signal_store.read_reviews(program_id)
    window_start = as_of - timedelta(days=window_days)
    return tuple(
        signal
        for signal in signal_store.read(program_id, start=window_start, end=as_of)
        if signal.source.startswith("workiq/")
        and signal.workstream_id is not None
        and signal_is_approved_for_evidence(signal, review_states)
    )


def _artifact_is_within_window(
    artifact: M365RegistryArtifact,
    *,
    as_of: datetime | None,
    window_days: int,
) -> bool:
    if as_of is None:
        return True
    cutoff = (as_of - timedelta(days=window_days)).date()
    return artifact.last_seen >= cutoff


def _feedback_event_is_within_window(
    event: M365RoutingFeedbackEvent,
    *,
    as_of: datetime | None,
    window_days: int,
) -> bool:
    if as_of is None:
        return True
    cutoff = as_of - timedelta(days=window_days)
    return event.ts >= cutoff


def _artifacts_by_known_id(
    registry_artifacts: tuple[M365RegistryArtifact, ...],
) -> dict[str, M365RegistryArtifact]:
    artifact_lookup: dict[str, M365RegistryArtifact] = {}
    for artifact in registry_artifacts:
        for artifact_id in (artifact.artifact_id, *artifact.legacy_artifact_ids):
            artifact_lookup[artifact_id] = artifact
    return artifact_lookup