from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, cast

from src.core.ledger.candidate_store import CandidateEntityResolution, CandidateEvent, derive_candidate_dedupe_key
from src.core.ledger.event_log import ConfidenceTier, EventEnvelope, TemporalConfidence, build_event_envelope, compute_dedupe_core_hash
from src.core.ledger.event_types import get_event_schema
from src.core.ledger.source_refs import LTDeckRef, source_document_key, source_ref_from_dict


class DiscoveryCandidateBuildError(ValueError):
    pass


def fresh_discovery_batch_id() -> str:
    now = datetime.now(timezone.utc)
    return build_event_envelope(
        program_id="batch",
        event_type="discovery.candidate_proposed.v1",
        occurred_at=now,
        recorded_at=now,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="batch",
        payload={"batch_id": "seed", "pipeline": "backfill_import", "candidate_count": 0},
        source_ref=LTDeckRef(file_path="batch", deck_date=now.date()),
        dedupe_payload={"batch_id": "seed"},
    ).event_id


def candidate_from_import_line(
    line: str,
    *,
    program: str,
    batch_id: str,
    pipeline: str = "backfill_import",
) -> CandidateEvent:
    payload = _parse_json_value(line)
    if not isinstance(payload, dict):
        raise DiscoveryCandidateBuildError("Import source must contain JSON object rows.")
    if _looks_like_event_envelope(payload):
        return _candidate_from_event_envelope_payload(payload, program=program, batch_id=batch_id, pipeline=pipeline)
    if _looks_like_candidate_record(payload):
        return _candidate_from_candidate_payload(payload, program=program, batch_id=batch_id, pipeline=pipeline)
    raise DiscoveryCandidateBuildError("Import row must be either an event envelope or a candidate record.")


def build_lt_deck_artifact_candidates(
    program: str,
    *,
    source_dir: Path,
    from_year: int | None,
    batch_id: str,
    pipeline: str,
) -> tuple[CandidateEvent, ...]:
    candidates: list[CandidateEvent] = []
    for path in sorted(source_dir.rglob("*.pptx"), key=lambda item: item.as_posix().lower()):
        if path.parent == source_dir:
            continue
        year = _extract_lt_deck_year(path)
        deck_date = _extract_lt_deck_date(path)
        if year is None or deck_date is None:
            continue
        if from_year is not None and year < from_year:
            continue
        relative_path = path.relative_to(source_dir).as_posix()
        title = path.stem.strip()
        artifact_id = f"published_artifact:lt-deck:{deck_date.isoformat()}:{path.stem.strip().lower().replace(' ', '-')}"
        payload = {
            "artifact_id": artifact_id,
            "artifact_kind": "lt_deck",
            "title": title,
            "location": relative_path,
            "period_start": deck_date.isoformat(),
            "period_end": deck_date.isoformat(),
        }
        source_ref = LTDeckRef(file_path=relative_path, deck_date=deck_date)
        dedupe_payload = _dedupe_payload_for("artifact.published.v1", payload)
        dedupe_core_hash = compute_dedupe_core_hash("artifact.published.v1", dedupe_payload)
        document_key = source_document_key(source_ref)
        candidates.append(
            CandidateEvent(
                candidate_id=fresh_discovery_batch_id(),
                program_id=program,
                proposed_event_type="artifact.published.v1",
                proposed_payload=payload,
                proposed_occurred_at=datetime.combine(deck_date, datetime.min.time(), tzinfo=timezone.utc),
                proposed_temporal_confidence="approximate",
                proposed_confidence="source_authoritative",
                source_ref=source_ref,
                pipeline=pipeline,
                extraction_confidence=0.95,
                entity_resolution=(
                    CandidateEntityResolution(
                        raw_name=artifact_id,
                        resolved_entity_id=artifact_id,
                        match_kind="imported",
                        score=1.0,
                    ),
                ),
                dedupe_key=derive_candidate_dedupe_key(document_key, dedupe_core_hash),
                dedupe_core_hash=dedupe_core_hash,
                source_document_key=document_key,
                corroborating_refs=(),
                batch_id=batch_id,
            )
        )
    return tuple(candidates)


def _parse_json_value(raw: str) -> object:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise DiscoveryCandidateBuildError(f"Import row must be valid JSON: {error}") from error


def _looks_like_event_envelope(payload: dict[str, object]) -> bool:
    return all(key in payload for key in ("event_id", "event_type", "occurred_at", "recorded_at", "payload", "source_ref"))


def _looks_like_candidate_record(payload: dict[str, object]) -> bool:
    return all(
        key in payload
        for key in (
            "proposed_event_type",
            "proposed_payload",
            "proposed_occurred_at",
            "proposed_temporal_confidence",
            "proposed_confidence",
            "source_ref",
        )
    )


def _candidate_from_event_envelope_payload(
    payload: dict[str, object],
    *,
    program: str,
    batch_id: str,
    pipeline: str,
) -> CandidateEvent:
    envelope = EventEnvelope.from_dict(payload)
    event_payload = dict(envelope.payload)
    dedupe_payload = _dedupe_payload_for(envelope.event_type, event_payload)
    dedupe_core_hash = envelope.dedupe_core_hash or compute_dedupe_core_hash(envelope.event_type, dedupe_payload)
    document_key = source_document_key(envelope.source_ref)
    return CandidateEvent(
        candidate_id=fresh_discovery_batch_id(),
        program_id=program,
        proposed_event_type=envelope.event_type,
        proposed_payload=event_payload,
        proposed_occurred_at=envelope.occurred_at,
        proposed_temporal_confidence=envelope.temporal_confidence.value,
        proposed_confidence=envelope.confidence.value,
        source_ref=envelope.source_ref,
        pipeline=pipeline,
        extraction_confidence=_import_confidence_score(envelope.confidence.value),
        entity_resolution=_import_entity_resolution(envelope.event_type, event_payload),
        dedupe_key=derive_candidate_dedupe_key(document_key, dedupe_core_hash),
        dedupe_core_hash=dedupe_core_hash,
        source_document_key=document_key,
        corroborating_refs=envelope.corroborating_refs,
        batch_id=batch_id,
    )


def _candidate_from_candidate_payload(
    payload: dict[str, object],
    *,
    program: str,
    batch_id: str,
    pipeline: str,
) -> CandidateEvent:
    proposed_payload = dict(_require_mapping(payload, "proposed_payload"))
    proposed_event_type = _require_str(payload, "proposed_event_type")
    dedupe_payload = _dedupe_payload_for(proposed_event_type, proposed_payload)
    dedupe_core_hash = str(payload.get("dedupe_core_hash") or compute_dedupe_core_hash(proposed_event_type, dedupe_payload))
    source_ref = source_ref_from_dict(_require_mapping(payload, "source_ref"))
    document_key = str(payload.get("source_document_key") or source_document_key(source_ref))
    entity_resolution = tuple(
        CandidateEntityResolution(
            raw_name=str(item.get("raw_name", "")),
            resolved_entity_id=str(item["resolved_entity_id"]) if isinstance(item.get("resolved_entity_id"), str) else None,
            match_kind=str(item.get("match_kind", "imported")),
            score=float(item.get("score", 1.0)),
        )
        for item in _require_list(payload, "entity_resolution", default=[])
        if isinstance(item, dict)
    )
    corroborating_refs = tuple(
        source_ref_from_dict(item)
        for item in _require_list(payload, "corroborating_refs", default=[])
        if isinstance(item, dict)
    )
    return CandidateEvent(
        candidate_id=str(payload.get("candidate_id") or fresh_discovery_batch_id()),
        program_id=program,
        proposed_event_type=proposed_event_type,
        proposed_payload=proposed_payload,
        proposed_occurred_at=_require_datetime(payload, "proposed_occurred_at"),
        proposed_temporal_confidence=_require_str(payload, "proposed_temporal_confidence"),
        proposed_confidence=_require_str(payload, "proposed_confidence"),
        source_ref=source_ref,
        pipeline=pipeline,
        extraction_confidence=float(cast(float, payload.get("extraction_confidence", 1.0))),
        entity_resolution=entity_resolution,
        dedupe_key=str(payload.get("dedupe_key") or derive_candidate_dedupe_key(document_key, dedupe_core_hash)),
        dedupe_core_hash=dedupe_core_hash,
        source_document_key=document_key,
        corroborating_refs=corroborating_refs,
        batch_id=batch_id,
    )


def _import_confidence_score(confidence: str) -> float:
    return {
        ConfidenceTier.OPERATOR_CONFIRMED.value: 1.0,
        ConfidenceTier.SOURCE_AUTHORITATIVE.value: 0.95,
        ConfidenceTier.AI_EXTRACTED.value: 0.8,
        ConfidenceTier.INFERRED.value: 0.6,
    }.get(confidence, 0.5)


def _import_entity_resolution(event_type: str, payload: dict[str, object]) -> tuple[CandidateEntityResolution, ...]:
    schema = get_event_schema(event_type)
    resolutions: list[CandidateEntityResolution] = []
    for field_name in schema.entity_ref_fields:
        raw_value = payload.get(field_name)
        if isinstance(raw_value, str):
            resolutions.append(CandidateEntityResolution(raw_name=raw_value, resolved_entity_id=raw_value, match_kind="imported", score=1.0))
        elif isinstance(raw_value, list):
            for item in raw_value:
                if isinstance(item, str):
                    resolutions.append(CandidateEntityResolution(raw_name=item, resolved_entity_id=item, match_kind="imported", score=1.0))
        elif isinstance(raw_value, dict) and field_name == "milestone_dates":
            for item in raw_value.keys():
                if isinstance(item, str):
                    resolutions.append(CandidateEntityResolution(raw_name=item, resolved_entity_id=item, match_kind="imported", score=1.0))
    return tuple(resolutions)


def _require_mapping(payload: dict[str, object], field_name: str) -> dict[str, object]:
    value = payload.get(field_name)
    if not isinstance(value, dict):
        raise DiscoveryCandidateBuildError(f"Imported row field '{field_name}' must be a JSON object.")
    return value


def _require_list(payload: dict[str, object], field_name: str, *, default: list[object] | None = None) -> list[object]:
    value = payload.get(field_name, default if default is not None else None)
    if value is None:
        return [] if default is not None else []
    if not isinstance(value, list):
        raise DiscoveryCandidateBuildError(f"Imported row field '{field_name}' must be a JSON array.")
    return value


def _require_str(payload: dict[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        raise DiscoveryCandidateBuildError(f"Imported row field '{field_name}' must be a non-empty string.")
    return value


def _require_datetime(payload: dict[str, object], field_name: str) -> datetime:
    value = payload.get(field_name)
    if not isinstance(value, str):
        raise DiscoveryCandidateBuildError(f"Imported row field '{field_name}' must be an ISO-8601 string.")
    return _parse_datetime(value, field_name)


def _parse_datetime(value: str, field_name: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise DiscoveryCandidateBuildError(f"Imported row field '{field_name}' must be an ISO-8601 string.") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _dedupe_payload_for(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    mapping: dict[str, dict[str, str]] = {
        "phase.transitioned.v1": {"to_phase": "to_phase"},
        "charter.created.v1": {"charter_id": "charter_id"},
        "charter.scope_changed.v1": {"change_summary": "change_summary"},
        "milestone.created.v1": {"milestone_id": "milestone_id", "name": "name"},
        "milestone.date_revised.v1": {"milestone_id": "milestone_id", "new_target_date": "new_target_date"},
        "milestone.completed.v1": {"completed_on": "completed_on"},
        "decision.revised.v1": {"decision_text": "revision_text"},
        "decision.made.v1": {"title": "title", "decision_text": "decision_text", "forum": "forum"},
        "deliverable.status_changed.v1": {"status": "new_status"},
        "dependency.status_changed.v1": {"status": "new_status"},
        "workstream.status_changed.v1": {"status": "new_status"},
        "commitment.slipped.v1": {"due_date": "new_due_date"},
        "commitment.fulfilled.v1": {"fulfilled_on": "fulfilled_on"},
        "incident.resolved.v1": {"resolved_on": "resolved_on", "mttr_minutes": "mttr_minutes", "root_cause": "root_cause"},
        "knowledge.article_revised.v1": {"location": "location"},
    }
    if event_type in mapping:
        return {
            field_name: payload[payload_field]
            for field_name, payload_field in mapping[event_type].items()
            if payload_field in payload
        }
    if event_type == "metric.observed.v1":
        return {}
    return dict(payload)


def _extract_lt_deck_year(path: Path) -> int | None:
    try:
        return int(path.parent.name)
    except ValueError:
        return None


def _extract_lt_deck_date(path: Path):
    stem = path.stem
    for token in stem.replace('_', ' ').split():
        normalized = token.strip().rstrip('-')
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            parsed = None
        if parsed is not None:
            return parsed.date()
        for pattern in ("%Y%m%d", "%Y%m", "%Y%m-%d"):
            try:
                return datetime.strptime(normalized, pattern).date()
            except ValueError:
                continue
    return None