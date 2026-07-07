from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from src.core.ledger.event_log import EventEnvelope


def apply_supersession(events: Iterable[EventEnvelope]) -> tuple[EventEnvelope, ...]:
    ordered = tuple(sorted(events, key=lambda event: (event.recorded_at, event.event_id)))
    events_by_id = {event.event_id: event for event in ordered}
    correction_targets: dict[str, list[str]] = {}
    for event in ordered:
        if event.event_type != "operator.correction.v1":
            continue
        target_id = event.payload["corrects_event_id"]
        if target_id not in events_by_id:
            raise ValueError(f"Dangling supersession target: {target_id}")
        correction_targets.setdefault(target_id, []).append(event.event_id)

    resolved: list[EventEnvelope] = []
    for event in ordered:
        if event.event_type == "operator.correction.v1":
            continue
        effective = resolve_effective_event(event.event_id, events_by_id, correction_targets)
        if effective is not None:
            resolved.append(effective)
    return tuple(resolved)


def find_tombstoned_targets(events: Iterable[EventEnvelope]) -> dict[str, str]:
    ordered = tuple(sorted(events, key=lambda event: (event.recorded_at, event.event_id)))
    events_by_id = {event.event_id: event for event in ordered}
    correction_targets: dict[str, list[str]] = {}
    for event in ordered:
        if event.event_type != "operator.correction.v1":
            continue
        target_id = event.payload["corrects_event_id"]
        if target_id not in events_by_id:
            raise ValueError(f"Dangling supersession target: {target_id}")
        correction_targets.setdefault(target_id, []).append(event.event_id)

    tombstoned: dict[str, str] = {}
    for event in ordered:
        if event.event_type == "operator.correction.v1":
            continue
        terminal_correction_id = _resolve_terminal_correction_id(event.event_id, events_by_id, correction_targets)
        if terminal_correction_id is None:
            continue
        if events_by_id[terminal_correction_id].payload.get("corrected_payload") is None:
            tombstoned[event.event_id] = terminal_correction_id
    return tombstoned


def resolve_effective_event(
    event_id: str,
    events_by_id: dict[str, EventEnvelope],
    correction_targets: dict[str, list[str]],
    *,
    _stack: tuple[str, ...] = (),
) -> EventEnvelope | None:
    if event_id in _stack:
        cycle = " -> ".join((*_stack, event_id))
        raise ValueError(f"Supersession cycle detected: {cycle}")
    try:
        base_event = events_by_id[event_id]
    except KeyError as error:
        raise ValueError(f"Dangling supersession target: {event_id}") from error

    correction_ids = correction_targets.get(event_id, [])
    if not correction_ids:
        return base_event

    latest_correction_id = max(
        correction_ids,
        key=lambda correction_id: (events_by_id[correction_id].recorded_at, correction_id),
    )
    effective_correction = resolve_effective_event(
        latest_correction_id,
        events_by_id,
        correction_targets,
        _stack=(*_stack, event_id),
    )
    if effective_correction is None:
        return base_event
    if effective_correction.event_type != "operator.correction.v1":
        raise ValueError(f"Correction target {latest_correction_id} resolved to non-correction event")
    corrected_payload = effective_correction.payload.get("corrected_payload")
    if corrected_payload is None:
        return None
    return replace(
        base_event,
        event_id=effective_correction.event_id,
        recorded_at=effective_correction.recorded_at,
        confidence=effective_correction.confidence,
        temporal_confidence=effective_correction.temporal_confidence,
        actor=effective_correction.actor,
        payload=corrected_payload,
        source_ref=effective_correction.source_ref,
        corroborating_refs=effective_correction.corroborating_refs,
        prev_event_hash=effective_correction.prev_event_hash,
        content_hash=effective_correction.content_hash,
        dedupe_core_hash=effective_correction.dedupe_core_hash,
    )


def _resolve_terminal_correction_id(
    event_id: str,
    events_by_id: dict[str, EventEnvelope],
    correction_targets: dict[str, list[str]],
    *,
    _stack: tuple[str, ...] = (),
) -> str | None:
    if event_id in _stack:
        cycle = " -> ".join((*_stack, event_id))
        raise ValueError(f"Supersession cycle detected: {cycle}")

    correction_ids = correction_targets.get(event_id, [])
    if not correction_ids:
        return None

    latest_correction_id = max(
        correction_ids,
        key=lambda correction_id: (events_by_id[correction_id].recorded_at, correction_id),
    )
    descendant = _resolve_terminal_correction_id(
        latest_correction_id,
        events_by_id,
        correction_targets,
        _stack=(*_stack, event_id),
    )
    return descendant or latest_correction_id