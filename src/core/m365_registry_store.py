from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
import json
from src.core.jsonl_utils import parse_jsonl_line
import os
from pathlib import Path
import re
from typing import Any

import portalocker
import yaml

from src.core.m365_discovery_support import candidate_match_score, normalize_match_text, tokenize_match_text
from src.core.m365_identifiers import normalize_thread_id
from src.core.workstream_documents import save_workstreams_document
from src.core.exceptions import ConfigError
from src.core.journal import PROGRAMS_ROOT
from src.core.models_v2 import Workstream
from src.core.program_paths import get_m365_registry_path, resolve_m365_registry_path_for_read


@dataclass(frozen=True, slots=True)
class M365RegistryArtifact:
    artifact_id: str
    artifact_type: str
    inferred_workstream: str
    confidence: float
    confidence_source: str
    pm_confirmed: bool
    promoted_to_workstreams_yaml: bool
    first_seen: date
    last_seen: date
    signal_yield_last_3: tuple[int, int, int] = (0, 0, 0)
    display_name: str | None = None
    series_id: str | None = None
    thread_id: str | None = None
    topics: tuple[str, ...] = ()
    routing_reasoning: str | None = None
    high_confidence_streak: int = 0
    legacy_artifact_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class M365Registry:
    schema_version: str
    program_id: str
    last_updated: datetime | None
    artifacts: tuple[M365RegistryArtifact, ...]


@dataclass(frozen=True, slots=True)
class M365RoutingFeedbackEvent:
    ts: datetime
    artifact_id: str
    action: str
    pm_alias: str
    workstream_id: str | None = None
    prior_workstream_id: str | None = None
    topics: tuple[str, ...] = ()
    reason: str | None = None
    series_id: str | None = None
    thread_id: str | None = None
    new_artifact_id: str | None = None


_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")
_M365_CONFIDENCE_DECAY_STEP = 0.05
_M365_CONFIDENCE_DECAY_FLOOR = 0.20
_M365_PROMOTION_MIN_SIGNAL_YIELD = 3
_M365_ACTIVE_REJECTION_LOOKBACK_DAYS = 60
_M365_AUTO_PROMOTION_CONFIDENCE_THRESHOLD = 0.85
_M365_AUTO_PROMOTION_STREAK_THRESHOLD = 3
_M365_DRIFT_REBIND_MIN_SCORE = 0.78
_M365_DRIFT_REBIND_AMBIGUITY_GAP = 0.05


def get_m365_routing_feedback_path(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "_feedback" / "m365_routing_feedback.jsonl"


def get_program_workstreams_path(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "workstreams.yaml"


def load_m365_registry(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> M365Registry:
    path = resolve_m365_registry_path_for_read(program_id, programs_root=programs_root)
    if not path.exists():
        return M365Registry(schema_version="1.0", program_id=program_id, last_updated=None, artifacts=())

    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigError(f"Invalid YAML in {path}.") from error

    if not isinstance(document, dict):
        raise ConfigError(f"Expected mapping in {path}.")

    schema_version = _required_string(document.get("schema_version"), field_name="schema_version").strip()
    if schema_version.split(".", 1)[0] != "1":
        raise ConfigError(f"Unsupported M365 registry schema_version {schema_version!r} in {path}.")

    raw_artifacts = document.get("artifacts")
    if raw_artifacts is None:
        raw_artifacts = []
    if not isinstance(raw_artifacts, list):
        raise ConfigError(f"Expected 'artifacts' list in {path}.")

    artifacts: list[M365RegistryArtifact] = []
    seen_ids: set[str] = set()
    for index, raw_entry in enumerate(raw_artifacts, start=1):
        if not isinstance(raw_entry, dict):
            raise ConfigError(f"Artifact entry #{index} in {path} must be a mapping.")
        artifact = _parse_artifact(raw_entry)
        if artifact.artifact_id in seen_ids:
            raise ConfigError(f"Duplicate M365 artifact id '{artifact.artifact_id}' in {path}.")
        seen_ids.add(artifact.artifact_id)
        artifacts.append(artifact)

    return M365Registry(
        schema_version=schema_version or "1.0",
        program_id=_optional_string(document.get("program_id"), field_name="program_id") or program_id,
        last_updated=_parse_optional_datetime(document.get("last_updated"), field_name="last_updated"),
        artifacts=tuple(artifacts),
    )


def save_m365_registry(registry: M365Registry, programs_root: Path = PROGRAMS_ROOT) -> Path:
    path = get_m365_registry_path(registry.program_id, programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": registry.schema_version,
        "program_id": registry.program_id,
        "last_updated": _format_optional_datetime(registry.last_updated),
        "artifacts": [_artifact_to_record(artifact) for artifact in registry.artifacts],
    }
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=False), encoding="utf-8")
    os.replace(temp_path, path)
    return path


def ensure_m365_registry_bootstrap(
    program_id: str,
    *,
    workstreams: tuple[Workstream, ...],
    programs_root: Path = PROGRAMS_ROOT,
    as_of: datetime | None = None,
) -> M365Registry:
    existing_registry = load_m365_registry(program_id, programs_root)
    artifacts_by_id = {artifact.artifact_id: artifact for artifact in existing_registry.artifacts}
    observed_on = (as_of or datetime.now(timezone.utc)).date()
    for artifact in _bootstrap_artifacts(program_id=program_id, workstreams=workstreams, observed_on=observed_on):
        artifacts_by_id.setdefault(artifact.artifact_id, artifact)

    registry = M365Registry(
        schema_version="1.0",
        program_id=program_id,
        last_updated=(as_of or datetime.now(timezone.utc)).astimezone(timezone.utc),
        artifacts=tuple(artifacts_by_id[artifact_id] for artifact_id in sorted(artifacts_by_id)),
    )
    save_m365_registry(registry, programs_root)

    feedback_path = get_m365_routing_feedback_path(program_id, programs_root)
    feedback_path.parent.mkdir(parents=True, exist_ok=True)
    if not feedback_path.exists():
        feedback_path.write_text("", encoding="utf-8")
    return registry


def build_auto_thread_artifact_id(thread_id: str) -> str:
    snippet = _SLUG_PATTERN.sub("", thread_id.strip().lower())[:8]
    return f"thread:auto:{snippet or 'thread'}"


def build_auto_meeting_artifact_id(series_id: str) -> str:
    snippet = _SLUG_PATTERN.sub("", series_id.strip().lower())[:8]
    return f"meeting:auto:{snippet or 'meeting'}"


def upsert_m365_registry_artifacts(
    program_id: str,
    *,
    artifacts: tuple[M365RegistryArtifact, ...],
    programs_root: Path = PROGRAMS_ROOT,
    as_of: datetime | None = None,
) -> M365Registry:
    existing_registry = load_m365_registry(program_id, programs_root)
    artifacts_by_id = {artifact.artifact_id: artifact for artifact in existing_registry.artifacts}
    # Drift-rebind matches only against artifacts that pre-existed this upsert batch:
    # a "drift" is a single source rediscovered under a new durable id, superseding a
    # PRIOR registration. Two distinct artifacts supplied together in the same batch
    # (e.g. "Pilot readiness sync" + "Pilot readiness review", different thread_ids)
    # must never be collapsed just because their display names overlap.
    prior_artifact_ids = set(artifacts_by_id)
    for artifact in artifacts:
        current = artifacts_by_id.get(artifact.artifact_id)
        if current is None:
            rebind_candidates = tuple(
                candidate for candidate_id, candidate in artifacts_by_id.items()
                if candidate_id in prior_artifact_ids
            )
            rebound = _rebind_drifted_artifact(rebind_candidates, incoming=artifact)
            if rebound is not None:
                prior_artifact_id, rebound_artifact = rebound
                if prior_artifact_id != rebound_artifact.artifact_id:
                    artifacts_by_id.pop(prior_artifact_id, None)
                    prior_artifact_ids.discard(prior_artifact_id)
                artifacts_by_id[rebound_artifact.artifact_id] = rebound_artifact
                continue
        artifacts_by_id[artifact.artifact_id] = artifact if current is None else _merge_artifact(current, artifact)

    registry = M365Registry(
        schema_version=existing_registry.schema_version or "1.0",
        program_id=program_id,
        last_updated=(as_of or datetime.now(timezone.utc)).astimezone(timezone.utc),
        artifacts=tuple(artifacts_by_id[artifact_id] for artifact_id in sorted(artifacts_by_id)),
    )
    save_m365_registry(registry, programs_root)
    return registry


def read_m365_routing_feedback_events(
    program_id: str,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[M365RoutingFeedbackEvent, ...]:
    path = get_m365_routing_feedback_path(program_id, programs_root)
    if not path.exists():
        return ()
    events: list[M365RoutingFeedbackEvent] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        payload = parse_jsonl_line(stripped)
        if not isinstance(payload, dict):
            raise ConfigError(f"M365 routing feedback entry in {path} must be a mapping.")
        events.append(_feedback_event_from_record(payload))
    return tuple(events)


def apply_m365_routing_feedback(
    program_id: str,
    *,
    event: M365RoutingFeedbackEvent,
    programs_root: Path = PROGRAMS_ROOT,
) -> M365Registry:
    registry = load_m365_registry(program_id, programs_root)
    updated = False
    persisted_event = event
    artifacts: list[M365RegistryArtifact] = []
    for artifact in registry.artifacts:
        if not _artifact_matches_id(artifact, event.artifact_id):
            artifacts.append(artifact)
            continue
        if event.action == "reassign" and event.prior_workstream_id is None:
            persisted_event = replace(event, prior_workstream_id=artifact.inferred_workstream)
        artifacts.append(_apply_feedback_event_to_artifact(artifact, persisted_event))
        updated = True
    if not updated:
        raise ConfigError(f"M365 registry artifact '{event.artifact_id}' not found for program '{program_id}'.")

    next_registry = M365Registry(
        schema_version=registry.schema_version,
        program_id=registry.program_id,
        last_updated=persisted_event.ts.astimezone(timezone.utc),
        artifacts=tuple(artifacts),
    )
    save_m365_registry(next_registry, programs_root)
    _append_m365_routing_feedback_event(program_id, persisted_event, programs_root)
    return next_registry


def promote_m365_registry_artifact(
    program_id: str,
    *,
    artifact_id: str,
    programs_root: Path = PROGRAMS_ROOT,
    as_of: datetime | None = None,
    pm_alias: str | None = None,
    reason: str | None = None,
) -> M365Registry:
    registry = load_m365_registry(program_id, programs_root)
    feedback_events = read_m365_routing_feedback_events(program_id, programs_root)
    artifact = next((entry for entry in registry.artifacts if _artifact_matches_id(entry, artifact_id)), None)
    if artifact is None:
        raise ConfigError(f"M365 registry artifact '{artifact_id}' not found for program '{program_id}'.")

    promoted_at = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
    promotion_feedback_event: M365RoutingFeedbackEvent | None = None
    if not artifact.pm_confirmed:
        if pm_alias is None or not pm_alias.strip():
            raise ConfigError(f"Artifact '{artifact.artifact_id}' requires pm_alias for confidence-based promotion.")
        if not _artifact_meets_auto_promotion_confidence_gate(artifact):
            raise ConfigError(
                f"Artifact '{artifact.artifact_id}' requires confidence >= {_M365_AUTO_PROMOTION_CONFIDENCE_THRESHOLD:.2f} "
                f"for {_M365_AUTO_PROMOTION_STREAK_THRESHOLD} consecutive gathers before confidence-based promotion."
            )
        promotion_feedback_event = M365RoutingFeedbackEvent(
            ts=promoted_at,
            artifact_id=artifact.artifact_id,
            action="confirm",
            pm_alias=pm_alias.strip(),
            workstream_id=artifact.inferred_workstream,
            topics=artifact.topics,
            reason=reason,
            series_id=artifact.series_id,
            thread_id=artifact.thread_id,
        )
        artifact = _apply_feedback_event_to_artifact(artifact, promotion_feedback_event)

    effective_feedback_events = (
        (*feedback_events, promotion_feedback_event) if promotion_feedback_event is not None else feedback_events
    )
    _validate_promotable_artifact(
        artifact,
        feedback_events=effective_feedback_events,
        as_of=promoted_at,
    )
    workstreams_path = get_program_workstreams_path(program_id, programs_root)
    document = _load_workstreams_document(workstreams_path)
    _promote_artifact_into_workstreams_document(document, artifact=artifact, path=workstreams_path)
    save_workstreams_document(program_id, document, programs_root=programs_root)

    next_registry = M365Registry(
        schema_version=registry.schema_version,
        program_id=registry.program_id,
        last_updated=promoted_at,
        artifacts=tuple(
            replace(artifact, promoted_to_workstreams_yaml=True) if entry.artifact_id == artifact_id else entry
            for entry in registry.artifacts
        ),
    )
    save_m365_registry(next_registry, programs_root)
    if promotion_feedback_event is not None:
        _append_m365_routing_feedback_event(program_id, promotion_feedback_event, programs_root)
    return next_registry


def rename_m365_registry_artifact(
    program_id: str,
    *,
    artifact_id: str,
    display_name: str,
    programs_root: Path = PROGRAMS_ROOT,
    as_of: datetime | None = None,
    pm_alias: str,
    reason: str | None = None,
) -> M365Registry:
    normalized_display_name = display_name.strip()
    if not normalized_display_name:
        raise ConfigError("display_name is required for M365 registry rename.")

    registry = load_m365_registry(program_id, programs_root)
    artifact = next((entry for entry in registry.artifacts if _artifact_matches_id(entry, artifact_id)), None)
    if artifact is None:
        raise ConfigError(f"M365 registry artifact '{artifact_id}' not found for program '{program_id}'.")
    if not artifact.artifact_id.startswith("thread:auto:"):
        raise ConfigError(
            f"Artifact '{artifact.artifact_id}' is not an auto-discovered thread artifact and cannot be renamed."
        )

    next_artifact_id = f"thread:named:{_slugify(normalized_display_name)}"
    if any(entry.artifact_id == next_artifact_id and entry.artifact_id != artifact.artifact_id for entry in registry.artifacts):
        raise ConfigError(f"M365 registry artifact '{next_artifact_id}' already exists for program '{program_id}'.")

    renamed_artifact = replace(
        artifact,
        artifact_id=next_artifact_id,
        display_name=normalized_display_name,
        legacy_artifact_ids=tuple(
            dict.fromkeys(
                candidate
                for candidate in (*artifact.legacy_artifact_ids, artifact.artifact_id)
                if candidate != next_artifact_id
            )
        ),
    )

    next_registry = M365Registry(
        schema_version=registry.schema_version,
        program_id=registry.program_id,
        last_updated=(as_of or datetime.now(timezone.utc)).astimezone(timezone.utc),
        artifacts=tuple(
            renamed_artifact if entry.artifact_id == artifact.artifact_id else entry
            for entry in registry.artifacts
        ),
    )
    save_m365_registry(next_registry, programs_root)
    _append_m365_routing_feedback_event(
        program_id,
        M365RoutingFeedbackEvent(
            ts=next_registry.last_updated or datetime.now(timezone.utc),
            artifact_id=artifact.artifact_id,
            action="rename_artifact",
            pm_alias=pm_alias.strip(),
            workstream_id=artifact.inferred_workstream,
            reason=reason,
            thread_id=artifact.thread_id,
            new_artifact_id=next_artifact_id,
        ),
        programs_root,
    )
    return next_registry


def is_current_m365_registry_promotion_candidate(
    artifact: M365RegistryArtifact,
    *,
    feedback_events: tuple[M365RoutingFeedbackEvent, ...] = (),
    as_of: datetime | None = None,
) -> bool:
    return not describe_current_m365_registry_promotion_blockers(
        artifact,
        feedback_events=feedback_events,
        as_of=as_of,
    )


def describe_current_m365_registry_promotion_blockers(
    artifact: M365RegistryArtifact,
    *,
    feedback_events: tuple[M365RoutingFeedbackEvent, ...] = (),
    as_of: datetime | None = None,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if artifact.promoted_to_workstreams_yaml:
        blockers.append("already_promoted")
    if sum(artifact.signal_yield_last_3) < _M365_PROMOTION_MIN_SIGNAL_YIELD:
        blockers.append("insufficient_signal_yield")
    if not artifact.pm_confirmed and not _artifact_meets_auto_promotion_confidence_gate(artifact):
        blockers.append("insufficient_confidence")
    if artifact.artifact_type == "meeting_series":
        has_required_id = artifact.series_id is not None
    elif artifact.artifact_type in {"teams_channel", "email_thread"}:
        has_required_id = artifact.thread_id is not None
    else:
        blockers.append("unsupported_artifact_type")
        has_required_id = False
    if not has_required_id:
        blockers.append("missing_required_id")
    if _has_active_recent_rejection(
        _artifact_ids_for_matching(artifact),
        feedback_events=feedback_events,
        as_of=as_of,
    ):
        blockers.append("recent_rejection")
    return tuple(blockers)


def tracked_registry_thread_ids(
    registry_artifacts: tuple[M365RegistryArtifact, ...],
    *,
    feedback_events: tuple[Any, ...] = (),
    as_of: datetime | None = None,
) -> set[str]:
    tracked_ids: set[str] = set()
    for artifact in registry_artifacts:
        if artifact.confidence_source == "pm_rejected":
            continue
        if "recent_rejection" in describe_current_m365_registry_promotion_blockers(
            artifact,
            feedback_events=feedback_events,
            as_of=as_of,
        ):
            continue
        normalized_thread = normalize_thread_id(artifact.thread_id)
        if normalized_thread:
            tracked_ids.add(normalized_thread)
        normalized_series = normalize_thread_id(artifact.series_id)
        if normalized_series:
            tracked_ids.add(normalized_series)
    return tracked_ids


def refresh_m365_registry_metrics(
    program_id: str,
    *,
    as_of: datetime,
    observed_artifact_ids: tuple[str, ...] = (),
    observed_thread_ids: tuple[str, ...] = (),
    observed_series_ids: tuple[str, ...] = (),
    programs_root: Path = PROGRAMS_ROOT,
) -> M365Registry:
    registry = load_m365_registry(program_id, programs_root)
    if not registry.artifacts:
        return registry

    observed_on = as_of.astimezone(timezone.utc).date()
    artifact_ids = {artifact_id.strip() for artifact_id in observed_artifact_ids if artifact_id.strip()}
    thread_ids = {thread_id.strip() for thread_id in observed_thread_ids if thread_id.strip()}
    series_ids = {series_id.strip() for series_id in observed_series_ids if series_id.strip()}
    refreshed_artifacts = tuple(
        _refresh_registry_artifact_metrics(
            artifact,
            observed=_artifact_was_observed(
                artifact,
                observed_artifact_ids=artifact_ids,
                observed_thread_ids=thread_ids,
                observed_series_ids=series_ids,
            ),
            observed_on=observed_on,
        )
        for artifact in registry.artifacts
    )
    next_registry = M365Registry(
        schema_version=registry.schema_version,
        program_id=registry.program_id,
        last_updated=as_of.astimezone(timezone.utc),
        artifacts=refreshed_artifacts,
    )
    save_m365_registry(next_registry, programs_root)
    return next_registry


def _bootstrap_artifacts(*, program_id: str, workstreams: tuple[Workstream, ...], observed_on: date) -> tuple[M365RegistryArtifact, ...]:
    artifacts: list[M365RegistryArtifact] = []
    for workstream in workstreams:
        signal_sources = workstream.signal_sources
        if signal_sources is None:
            continue
        topics = tuple(dict.fromkeys(keyword.strip() for keyword in signal_sources.workiq_keywords if keyword.strip()))
        for series in signal_sources.teams_meeting_series:
            display_name = series.display_name.strip()
            if not display_name:
                continue
            artifacts.append(
                M365RegistryArtifact(
                    artifact_id=f"meet:{program_id}-{_slugify(display_name)}",
                    artifact_type="meeting_series",
                    display_name=display_name,
                    series_id=series.series_id,
                    inferred_workstream=workstream.id,
                    confidence=1.0,
                    confidence_source="pm_confirmed",
                    pm_confirmed=True,
                    promoted_to_workstreams_yaml=True,
                    first_seen=observed_on,
                    last_seen=observed_on,
                    topics=topics,
                )
            )
        for chat in signal_sources.teams_chats:
            display_name = chat.display_name.strip()
            if not display_name:
                continue
            artifacts.append(
                M365RegistryArtifact(
                    artifact_id=f"chan:{program_id}-{_slugify(display_name)}",
                    artifact_type="teams_channel",
                    display_name=display_name,
                    thread_id=chat.thread_id,
                    inferred_workstream=workstream.id,
                    confidence=1.0,
                    confidence_source="pm_confirmed",
                    pm_confirmed=True,
                    promoted_to_workstreams_yaml=True,
                    first_seen=observed_on,
                    last_seen=observed_on,
                    topics=topics,
                )
            )
        for email_thread in signal_sources.email_threads:
            display_name = email_thread.display_name.strip()
            if not display_name:
                continue
            artifacts.append(
                M365RegistryArtifact(
                    artifact_id=f"thread:named:{program_id}-{_slugify(display_name)}",
                    artifact_type="email_thread",
                    display_name=display_name,
                    thread_id=email_thread.thread_id,
                    inferred_workstream=workstream.id,
                    confidence=1.0,
                    confidence_source="pm_confirmed",
                    pm_confirmed=True,
                    promoted_to_workstreams_yaml=True,
                    first_seen=observed_on,
                    last_seen=observed_on,
                    topics=topics,
                )
            )
    return tuple(artifacts)


def _parse_artifact(raw_entry: dict[str, Any]) -> M365RegistryArtifact:
    artifact_id = _required_string(raw_entry.get("artifact_id"), field_name="artifact_id").strip()
    artifact_type = _required_string(raw_entry.get("artifact_type"), field_name="artifact_type").strip()
    inferred_workstream = _required_string(raw_entry.get("inferred_workstream"), field_name="inferred_workstream").strip()
    if not artifact_id:
        raise ConfigError("missing artifact_id")
    if not artifact_type:
        raise ConfigError(f"artifact '{artifact_id}' is missing artifact_type")
    if not inferred_workstream:
        raise ConfigError(f"artifact '{artifact_id}' is missing inferred_workstream")
    return M365RegistryArtifact(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        inferred_workstream=inferred_workstream,
        confidence=_parse_confidence(raw_entry.get("confidence"), artifact_id=artifact_id),
        confidence_source=_optional_string(raw_entry.get("confidence_source"), field_name="confidence_source") or "discovered",
        pm_confirmed=_parse_bool(raw_entry.get("pm_confirmed"), field_name="pm_confirmed"),
        promoted_to_workstreams_yaml=_parse_bool(
            raw_entry.get("promoted_to_workstreams_yaml"),
            field_name="promoted_to_workstreams_yaml",
        ),
        first_seen=_parse_required_date(raw_entry.get("first_seen"), field_name=f"artifact '{artifact_id}' first_seen"),
        last_seen=_parse_required_date(raw_entry.get("last_seen"), field_name=f"artifact '{artifact_id}' last_seen"),
        signal_yield_last_3=_parse_signal_yield_last_3(raw_entry.get("signal_yield_last_3"), artifact_id=artifact_id),
        display_name=_optional_string(raw_entry.get("display_name"), field_name="display_name"),
        series_id=_optional_string(raw_entry.get("series_id"), field_name="series_id"),
        thread_id=_optional_string(raw_entry.get("thread_id"), field_name="thread_id"),
        topics=_parse_string_tuple(raw_entry.get("topics"), field_name="topics"),
        routing_reasoning=_optional_string(raw_entry.get("routing_reasoning"), field_name="routing_reasoning"),
        high_confidence_streak=_parse_non_negative_int(
            raw_entry.get("high_confidence_streak"),
            field_name=f"artifact '{artifact_id}' high_confidence_streak",
        ),
        legacy_artifact_ids=_parse_string_tuple(raw_entry.get("legacy_artifact_ids"), field_name="legacy_artifact_ids"),
    )


def _artifact_to_record(artifact: M365RegistryArtifact) -> dict[str, Any]:
    return {
        "artifact_id": artifact.artifact_id,
        "artifact_type": artifact.artifact_type,
        "display_name": artifact.display_name,
        "series_id": artifact.series_id,
        "thread_id": artifact.thread_id,
        "inferred_workstream": artifact.inferred_workstream,
        "confidence": artifact.confidence,
        "confidence_source": artifact.confidence_source,
        "pm_confirmed": artifact.pm_confirmed,
        "promoted_to_workstreams_yaml": artifact.promoted_to_workstreams_yaml,
        "first_seen": artifact.first_seen.isoformat(),
        "last_seen": artifact.last_seen.isoformat(),
        "signal_yield_last_3": list(artifact.signal_yield_last_3),
        "topics": list(artifact.topics),
        "routing_reasoning": artifact.routing_reasoning,
        "high_confidence_streak": artifact.high_confidence_streak,
        "legacy_artifact_ids": list(artifact.legacy_artifact_ids),
    }


def _parse_required_date(value: Any, *, field_name: str) -> date:
    if not isinstance(value, str):
        raise ConfigError(f"missing {field_name}")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ConfigError(f"invalid {field_name}") from error


def _parse_optional_datetime(value: Any, *, field_name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"invalid {field_name}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ConfigError(f"invalid {field_name}") from error
    if parsed.tzinfo is None:
        raise ConfigError(f"invalid {field_name}")
    return parsed.astimezone(timezone.utc)


def _format_optional_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def _parse_signal_yield_last_3(value: Any, *, artifact_id: str) -> tuple[int, int, int]:
    if value is None:
        return (0, 0, 0)
    if not isinstance(value, list) or len(value) != 3:
        raise ConfigError(f"artifact '{artifact_id}' signal_yield_last_3 must be a 3-item list")
    parsed: list[int] = []
    for entry in value:
        if not isinstance(entry, int) or isinstance(entry, bool):
            raise ConfigError(f"artifact '{artifact_id}' signal_yield_last_3 must contain integers")
        parsed.append(entry)
    return (parsed[0], parsed[1], parsed[2])


def _parse_confidence(value: Any, *, artifact_id: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigError(f"artifact '{artifact_id}' confidence must be numeric")
    confidence = float(value)
    if confidence < 0.0 or confidence > 1.0:
        raise ConfigError(f"artifact '{artifact_id}' confidence must be between 0.0 and 1.0")
    return confidence


def _parse_bool(value: Any, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{field_name} must be a boolean")
    return value


def _parse_non_negative_int(value: Any, *, field_name: str) -> int:
    if value is None:
        return 0
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"invalid {field_name}")
    parsed = value
    if parsed < 0:
        raise ConfigError(f"invalid {field_name}")
    return parsed


def _parse_string_tuple(value: Any, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"{field_name} must be a list of strings")
    parsed: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ConfigError(f"{field_name} must contain strings only")
        text = item.strip()
        if text:
            parsed.append(text)
    return tuple(parsed)


def _required_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{field_name} must be a string")
    return value


def _optional_string(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{field_name} must be a string")
    text = value.strip()
    return text or None


def _load_workstreams_document(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Expected workstreams config at {path}.")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigError(f"Invalid YAML in {path}.") from error
    if not isinstance(document, dict):
        raise ConfigError(f"Expected mapping in {path}.")
    return document


def _validate_promotable_artifact(
    artifact: M365RegistryArtifact,
    *,
    feedback_events: tuple[M365RoutingFeedbackEvent, ...] = (),
    as_of: datetime | None = None,
) -> None:
    if artifact.promoted_to_workstreams_yaml:
        return
    blockers = describe_current_m365_registry_promotion_blockers(
        artifact,
        feedback_events=feedback_events,
        as_of=as_of,
    )
    if not blockers:
        return
    if "recent_rejection" in blockers:
        raise ConfigError(f"Artifact '{artifact.artifact_id}' cannot be promoted because it has a recent rejection on record.")
    if "missing_required_id" in blockers:
        required_field = "series_id" if artifact.artifact_type == "meeting_series" else "thread_id"
        raise ConfigError(f"Artifact '{artifact.artifact_id}' requires {required_field} before promotion.")
    if "insufficient_signal_yield" in blockers:
        raise ConfigError(
            f"Artifact '{artifact.artifact_id}' requires signal_yield_last_3 sum >= {_M365_PROMOTION_MIN_SIGNAL_YIELD} before promotion."
        )
    if "insufficient_confidence" in blockers:
        raise ConfigError(f"Artifact '{artifact.artifact_id}' must be PM-confirmed before promotion.")
    raise ConfigError(
        f"Artifact '{artifact.artifact_id}' of type '{artifact.artifact_type}' cannot be promoted to workstreams.yaml yet."
    )


def _promote_artifact_into_workstreams_document(
    document: dict[str, Any],
    *,
    artifact: M365RegistryArtifact,
    path: Path,
) -> None:
    raw_workstreams = document.get("workstreams")
    if raw_workstreams is None:
        raise ConfigError(f"Expected 'workstreams' list in {path}.")
    if not isinstance(raw_workstreams, list):
        raise ConfigError(f"Expected 'workstreams' list in {path}.")

    for index, raw_workstream in enumerate(raw_workstreams, start=1):
        if not isinstance(raw_workstream, dict):
            raise ConfigError(f"Workstream entry #{index} in {path} must be a mapping.")
        workstream_id = _required_string(
            raw_workstream.get("id"),
            field_name=f"workstream entry #{index} id",
        ).strip()
        if workstream_id != artifact.inferred_workstream:
            continue
        signal_sources = raw_workstream.get("signal_sources")
        if signal_sources is None:
            signal_sources = {}
            raw_workstream["signal_sources"] = signal_sources
        if not isinstance(signal_sources, dict):
            raise ConfigError(f"Expected 'signal_sources' mapping for workstream '{workstream_id}' in {path}.")
        _upsert_promoted_signal_source(signal_sources, artifact=artifact, path=path)
        return

    raise ConfigError(
        f"Artifact '{artifact.artifact_id}' targets unknown workstream '{artifact.inferred_workstream}' in {path}."
    )


def _upsert_promoted_signal_source(
    signal_sources: dict[str, Any],
    *,
    artifact: M365RegistryArtifact,
    path: Path,
) -> None:
    if artifact.artifact_type == "meeting_series":
        raw_entries = signal_sources.get("teams_meeting_series")
        if raw_entries is None:
            raw_entries = []
            signal_sources["teams_meeting_series"] = raw_entries
        if not isinstance(raw_entries, list):
            raise ConfigError(f"Expected 'teams_meeting_series' list in {path}.")
        entry = _find_existing_signal_source_entry(
            raw_entries,
            id_key="series_id",
            id_value=artifact.series_id,
            display_name=artifact.display_name,
            path=path,
        )
        if entry is None:
            raw_entries.append(
                {
                    "display_name": artifact.display_name or artifact.artifact_id,
                    "series_id": artifact.series_id,
                    "include_transcripts": True,
                }
            )
            return
        entry["display_name"] = artifact.display_name or entry.get("display_name") or artifact.artifact_id
        entry["series_id"] = artifact.series_id
        entry.setdefault("include_transcripts", True)
        return

    if artifact.artifact_type == "email_thread":
        raw_entries = signal_sources.get("email_threads")
        if raw_entries is None:
            raw_entries = []
            signal_sources["email_threads"] = raw_entries
        if not isinstance(raw_entries, list):
            raise ConfigError(f"Expected 'email_threads' list in {path}.")
        entry = _find_existing_signal_source_entry(
            raw_entries,
            id_key="thread_id",
            id_value=artifact.thread_id,
            display_name=artifact.display_name,
            path=path,
        )
        if entry is None:
            raw_entries.append(
                {
                    "display_name": artifact.display_name or artifact.artifact_id,
                    "thread_id": artifact.thread_id,
                }
            )
            return
        entry["display_name"] = artifact.display_name or entry.get("display_name") or artifact.artifact_id
        entry["thread_id"] = artifact.thread_id
        return

    raw_entries = signal_sources.get("teams_chats")
    if raw_entries is None:
        raw_entries = []
        signal_sources["teams_chats"] = raw_entries
    if not isinstance(raw_entries, list):
        raise ConfigError(f"Expected 'teams_chats' list in {path}.")
    entry = _find_existing_signal_source_entry(
        raw_entries,
        id_key="thread_id",
        id_value=artifact.thread_id,
        display_name=artifact.display_name,
        path=path,
    )
    if entry is None:
        raw_entries.append(
            {
                "display_name": artifact.display_name or artifact.artifact_id,
                "thread_id": artifact.thread_id,
            }
        )
        return
    entry["display_name"] = artifact.display_name or entry.get("display_name") or artifact.artifact_id
    entry["thread_id"] = artifact.thread_id


def _find_existing_signal_source_entry(
    raw_entries: list[Any],
    *,
    id_key: str,
    id_value: str | None,
    display_name: str | None,
    path: Path,
) -> dict[str, Any] | None:
    normalized_display_name = (display_name or "").strip().lower()
    for index, raw_entry in enumerate(raw_entries, start=1):
        if not isinstance(raw_entry, dict):
            raise ConfigError(f"Signal source entry #{index} in {path} must be a mapping.")
        candidate_id = _optional_string(raw_entry.get(id_key), field_name=id_key)
        if id_value is not None and candidate_id == id_value:
            return raw_entry
        candidate_name = (_optional_string(raw_entry.get("display_name"), field_name="display_name") or "").lower()
        if normalized_display_name and candidate_name == normalized_display_name:
            return raw_entry
    return None


def _slugify(value: str) -> str:
    slug = _SLUG_PATTERN.sub("-", value.strip().lower()).strip("-")
    return slug or "artifact"


def _append_m365_routing_feedback_event(
    program_id: str,
    event: M365RoutingFeedbackEvent,
    programs_root: Path,
) -> None:
    path = get_m365_routing_feedback_path(program_id, programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_feedback_event_to_record(event), separators=(",", ":")) + os.linesep
    with path.open("a", encoding="utf-8") as handle:
        portalocker.lock(handle, portalocker.LockFlags.EXCLUSIVE)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        portalocker.unlock(handle)


def _feedback_event_to_record(event: M365RoutingFeedbackEvent) -> dict[str, Any]:
    return {
        "ts": event.ts.astimezone(timezone.utc).isoformat(),
        "artifact_id": event.artifact_id,
        "action": event.action,
        "pm_alias": event.pm_alias,
        "workstream_id": event.workstream_id,
        "prior_workstream_id": event.prior_workstream_id,
        "topics": list(event.topics),
        "reason": event.reason,
        "series_id": event.series_id,
        "thread_id": event.thread_id,
        "new_artifact_id": event.new_artifact_id,
    }


def _feedback_event_from_record(payload: dict[str, Any]) -> M365RoutingFeedbackEvent:
    return M365RoutingFeedbackEvent(
        ts=_parse_required_datetime(payload.get("ts"), field_name="ts"),
        artifact_id=_required_string(payload.get("artifact_id"), field_name="artifact_id").strip(),
        action=_required_string(payload.get("action"), field_name="action").strip(),
        pm_alias=_required_string(payload.get("pm_alias"), field_name="pm_alias").strip(),
        workstream_id=_optional_string(payload.get("workstream_id"), field_name="workstream_id"),
        prior_workstream_id=_optional_string(payload.get("prior_workstream_id"), field_name="prior_workstream_id"),
        topics=_parse_string_tuple(payload.get("topics"), field_name="topics"),
        reason=_optional_string(payload.get("reason"), field_name="reason"),
        series_id=_optional_string(payload.get("series_id"), field_name="series_id"),
        thread_id=_optional_string(payload.get("thread_id"), field_name="thread_id"),
        new_artifact_id=_optional_string(payload.get("new_artifact_id"), field_name="new_artifact_id"),
    )


def _parse_required_datetime(value: Any, *, field_name: str) -> datetime:
    parsed = _parse_optional_datetime(value, field_name=field_name)
    if parsed is None:
        raise ConfigError(f"invalid {field_name}")
    return parsed


def _apply_feedback_event_to_artifact(
    artifact: M365RegistryArtifact,
    event: M365RoutingFeedbackEvent,
) -> M365RegistryArtifact:
    topics = tuple(dict.fromkeys((*artifact.topics, *event.topics)))
    reasoning = event.reason or artifact.routing_reasoning
    if event.action == "confirm":
        return M365RegistryArtifact(
            artifact_id=artifact.artifact_id,
            artifact_type=artifact.artifact_type,
            inferred_workstream=event.workstream_id or artifact.inferred_workstream,
            confidence=max(artifact.confidence, 0.9),
            confidence_source="pm_confirmed",
            pm_confirmed=True,
            promoted_to_workstreams_yaml=artifact.promoted_to_workstreams_yaml,
            first_seen=artifact.first_seen,
            last_seen=max(artifact.last_seen, event.ts.date()),
            signal_yield_last_3=artifact.signal_yield_last_3,
            display_name=artifact.display_name,
            series_id=event.series_id or artifact.series_id,
            thread_id=event.thread_id or artifact.thread_id,
            topics=topics,
            routing_reasoning=reasoning,
            high_confidence_streak=artifact.high_confidence_streak,
        )
    if event.action == "reject":
        return M365RegistryArtifact(
            artifact_id=artifact.artifact_id,
            artifact_type=artifact.artifact_type,
            inferred_workstream=artifact.inferred_workstream,
            confidence=0.05,
            confidence_source="pm_rejected",
            pm_confirmed=False,
            promoted_to_workstreams_yaml=artifact.promoted_to_workstreams_yaml,
            first_seen=artifact.first_seen,
            last_seen=max(artifact.last_seen, event.ts.date()),
            signal_yield_last_3=artifact.signal_yield_last_3,
            display_name=artifact.display_name,
            series_id=event.series_id or artifact.series_id,
            thread_id=event.thread_id or artifact.thread_id,
            topics=topics,
            routing_reasoning=reasoning,
            high_confidence_streak=0,
        )
    if event.action == "reassign":
        next_workstream = event.workstream_id or artifact.inferred_workstream
        return M365RegistryArtifact(
            artifact_id=artifact.artifact_id,
            artifact_type=artifact.artifact_type,
            inferred_workstream=next_workstream,
            confidence=max(artifact.confidence, 0.9),
            confidence_source="pm_confirmed",
            pm_confirmed=True,
            promoted_to_workstreams_yaml=artifact.promoted_to_workstreams_yaml,
            first_seen=artifact.first_seen,
            last_seen=max(artifact.last_seen, event.ts.date()),
            signal_yield_last_3=artifact.signal_yield_last_3,
            display_name=artifact.display_name,
            series_id=event.series_id or artifact.series_id,
            thread_id=event.thread_id or artifact.thread_id,
            topics=topics,
            routing_reasoning=reasoning,
            high_confidence_streak=artifact.high_confidence_streak,
        )
    if event.action == "set_series_id":
        if event.series_id is None:
            raise ConfigError("M365 routing feedback action 'set_series_id' requires series_id.")
        return M365RegistryArtifact(
            artifact_id=artifact.artifact_id,
            artifact_type=artifact.artifact_type,
            inferred_workstream=artifact.inferred_workstream,
            confidence=artifact.confidence,
            confidence_source=artifact.confidence_source,
            pm_confirmed=artifact.pm_confirmed,
            promoted_to_workstreams_yaml=artifact.promoted_to_workstreams_yaml,
            first_seen=artifact.first_seen,
            last_seen=max(artifact.last_seen, event.ts.date()),
            signal_yield_last_3=artifact.signal_yield_last_3,
            display_name=artifact.display_name,
            series_id=event.series_id,
            thread_id=artifact.thread_id,
            topics=topics,
            routing_reasoning=reasoning,
            high_confidence_streak=artifact.high_confidence_streak,
        )
    if event.action == "set_thread_id":
        if event.thread_id is None:
            raise ConfigError("M365 routing feedback action 'set_thread_id' requires thread_id.")
        return M365RegistryArtifact(
            artifact_id=artifact.artifact_id,
            artifact_type=artifact.artifact_type,
            inferred_workstream=artifact.inferred_workstream,
            confidence=artifact.confidence,
            confidence_source=artifact.confidence_source,
            pm_confirmed=artifact.pm_confirmed,
            promoted_to_workstreams_yaml=artifact.promoted_to_workstreams_yaml,
            first_seen=artifact.first_seen,
            last_seen=max(artifact.last_seen, event.ts.date()),
            signal_yield_last_3=artifact.signal_yield_last_3,
            display_name=artifact.display_name,
            series_id=artifact.series_id,
            thread_id=event.thread_id,
            topics=topics,
            routing_reasoning=reasoning,
            high_confidence_streak=artifact.high_confidence_streak,
        )
    raise ConfigError(f"Unsupported M365 routing feedback action '{event.action}'.")


def _refresh_registry_artifact_metrics(
    artifact: M365RegistryArtifact,
    *,
    observed: bool,
    observed_on: date,
) -> M365RegistryArtifact:
    next_signal_yield = _roll_signal_yield_window(artifact.signal_yield_last_3, 1 if observed else 0)
    next_last_seen = max(artifact.last_seen, observed_on) if observed else artifact.last_seen
    next_confidence = artifact.confidence
    if not observed and not artifact.pm_confirmed and artifact.confidence_source != "pm_rejected":
        next_confidence = max(_M365_CONFIDENCE_DECAY_FLOOR, round(artifact.confidence - _M365_CONFIDENCE_DECAY_STEP, 2))
    next_high_confidence_streak = (
        artifact.high_confidence_streak + 1
        if next_confidence >= _M365_AUTO_PROMOTION_CONFIDENCE_THRESHOLD
        else 0
    )
    return replace(
        artifact,
        confidence=next_confidence,
        last_seen=next_last_seen,
        signal_yield_last_3=next_signal_yield,
        high_confidence_streak=next_high_confidence_streak,
    )


def _artifact_meets_auto_promotion_confidence_gate(artifact: M365RegistryArtifact) -> bool:
    return (
        artifact.confidence >= _M365_AUTO_PROMOTION_CONFIDENCE_THRESHOLD
        and artifact.high_confidence_streak >= _M365_AUTO_PROMOTION_STREAK_THRESHOLD
    )


def _artifact_was_observed(
    artifact: M365RegistryArtifact,
    *,
    observed_artifact_ids: set[str],
    observed_thread_ids: set[str],
    observed_series_ids: set[str],
) -> bool:
    if artifact.artifact_id in observed_artifact_ids:
        return True
    if artifact.thread_id is not None and artifact.thread_id in observed_thread_ids:
        return True
    if artifact.series_id is not None and artifact.series_id in observed_series_ids:
        return True
    return False


def _roll_signal_yield_window(window: tuple[int, int, int], current_yield: int) -> tuple[int, int, int]:
    return (window[1], window[2], current_yield)


def _has_active_recent_rejection(
    artifact_ids: tuple[str, ...],
    *,
    feedback_events: tuple[M365RoutingFeedbackEvent, ...],
    as_of: datetime | None,
) -> bool:
    effective_as_of = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = effective_as_of - timedelta(days=_M365_ACTIVE_REJECTION_LOOKBACK_DAYS)
    latest_reject_at: datetime | None = None
    latest_clear_at: datetime | None = None
    artifact_id_set = set(artifact_ids)

    for event in feedback_events:
        if event.artifact_id not in artifact_id_set:
            continue
        event_ts = event.ts.astimezone(timezone.utc)
        if event_ts < cutoff:
            continue
        if event.action == "reject" and (latest_reject_at is None or event_ts > latest_reject_at):
            latest_reject_at = event_ts
            continue
        if event.action in {"confirm", "reassign"} and (latest_clear_at is None or event_ts > latest_clear_at):
            latest_clear_at = event_ts

    return latest_reject_at is not None and (latest_clear_at is None or latest_clear_at <= latest_reject_at)


def _merge_artifact(current: M365RegistryArtifact, incoming: M365RegistryArtifact) -> M365RegistryArtifact:
    incoming_is_stronger = incoming.pm_confirmed or incoming.confidence >= current.confidence
    return M365RegistryArtifact(
        artifact_id=current.artifact_id,
        artifact_type=incoming.artifact_type or current.artifact_type,
        inferred_workstream=incoming.inferred_workstream if incoming_is_stronger else current.inferred_workstream,
        confidence=max(current.confidence, incoming.confidence),
        confidence_source=incoming.confidence_source if incoming_is_stronger else current.confidence_source,
        pm_confirmed=current.pm_confirmed or incoming.pm_confirmed,
        promoted_to_workstreams_yaml=current.promoted_to_workstreams_yaml or incoming.promoted_to_workstreams_yaml,
        first_seen=min(current.first_seen, incoming.first_seen),
        last_seen=max(current.last_seen, incoming.last_seen),
        signal_yield_last_3=incoming.signal_yield_last_3 if any(incoming.signal_yield_last_3) else current.signal_yield_last_3,
        display_name=incoming.display_name or current.display_name,
        series_id=incoming.series_id or current.series_id,
        thread_id=incoming.thread_id or current.thread_id,
        topics=tuple(dict.fromkeys((*current.topics, *incoming.topics))),
        routing_reasoning=incoming.routing_reasoning or current.routing_reasoning,
        high_confidence_streak=max(current.high_confidence_streak, incoming.high_confidence_streak),
        legacy_artifact_ids=tuple(dict.fromkeys((*current.legacy_artifact_ids, *incoming.legacy_artifact_ids))),
    )


def _rebind_drifted_artifact(
    existing_artifacts: tuple[M365RegistryArtifact, ...],
    *,
    incoming: M365RegistryArtifact,
) -> tuple[str, M365RegistryArtifact] | None:
    incoming_ref_id = incoming.series_id if incoming.artifact_type == "meeting_series" else incoming.thread_id
    if incoming_ref_id is None:
        return None

    scored_candidates: list[tuple[float, M365RegistryArtifact]] = []
    for current in existing_artifacts:
        if current.artifact_type != incoming.artifact_type:
            continue
        if current.inferred_workstream != incoming.inferred_workstream:
            continue
        if _artifact_matches_id(current, incoming.artifact_id):
            continue
        current_ref_id = current.series_id if current.artifact_type == "meeting_series" else current.thread_id
        if current_ref_id is None or current_ref_id == incoming_ref_id:
            continue
        score = _drift_rebind_score(current, incoming)
        if score < _M365_DRIFT_REBIND_MIN_SCORE:
            continue
        scored_candidates.append((score, current))

    if not scored_candidates:
        return None

    scored_candidates.sort(key=lambda item: (-item[0], item[1].artifact_id))
    best_score, best_match = scored_candidates[0]
    if len(scored_candidates) > 1 and best_score - scored_candidates[1][0] < _M365_DRIFT_REBIND_AMBIGUITY_GAP:
        return None
    return best_match.artifact_id, _apply_drift_rebind(best_match, incoming)


def _apply_drift_rebind(current: M365RegistryArtifact, incoming: M365RegistryArtifact) -> M365RegistryArtifact:
    merged = _merge_artifact(current, incoming)
    next_artifact_id = incoming.artifact_id if current.artifact_id.startswith("thread:auto:") else current.artifact_id
    legacy_ids: list[str] = []
    for candidate in (*current.legacy_artifact_ids, *incoming.legacy_artifact_ids, current.artifact_id, incoming.artifact_id):
        if candidate and candidate != next_artifact_id and candidate not in legacy_ids:
            legacy_ids.append(candidate)
    return replace(
        merged,
        artifact_id=next_artifact_id,
        series_id=incoming.series_id or current.series_id,
        thread_id=incoming.thread_id or current.thread_id,
        display_name=incoming.display_name or current.display_name,
        legacy_artifact_ids=tuple(legacy_ids),
    )


def _drift_rebind_score(current: M365RegistryArtifact, incoming: M365RegistryArtifact) -> float:
    title_score = max(
        candidate_match_score(current.display_name, incoming.display_name),
        candidate_match_score(incoming.display_name, current.display_name),
    )
    normalized_current = normalize_match_text(current.display_name)
    normalized_incoming = normalize_match_text(incoming.display_name)
    if normalized_current and normalized_current == normalized_incoming:
        title_score = max(title_score, 0.98)
    topic_score = _artifact_topic_overlap(current, incoming)
    return min(0.98, title_score * 0.84 + topic_score * 0.16)


def _artifact_topic_overlap(current: M365RegistryArtifact, incoming: M365RegistryArtifact) -> float:
    current_tokens = {
        token
        for value in (*current.topics, current.display_name or "")
        for token in tokenize_match_text(value, drop_generic=True)
        if token
    }
    incoming_tokens = {
        token
        for value in (*incoming.topics, incoming.display_name or "")
        for token in tokenize_match_text(value, drop_generic=True)
        if token
    }
    if not current_tokens or not incoming_tokens:
        return 0.0
    overlap = current_tokens & incoming_tokens
    if not overlap:
        return 0.0
    return len(overlap) / max(len(current_tokens), len(incoming_tokens))


def _artifact_matches_id(artifact: M365RegistryArtifact, artifact_id: str) -> bool:
    return artifact_id in _artifact_ids_for_matching(artifact)


def _artifact_ids_for_matching(artifact: M365RegistryArtifact) -> tuple[str, ...]:
    return (artifact.artifact_id, *artifact.legacy_artifact_ids)
