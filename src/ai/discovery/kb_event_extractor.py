from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.ai.discovery._structured_event_markers import (
    StructuredEventMarkerError,
    entity_resolution_from_payload,
    marker_occurrence_or_default,
    parse_structured_event_marker_result,
)
from src.core.ledger.candidate_store import CandidateEntityResolution, CandidateEvent, derive_candidate_dedupe_key
from src.core.ledger.event_log import ConfidenceTier, compute_dedupe_core_hash
from src.core.ledger.event_types import get_event_schema, validate_event_payload
from src.core.ledger.source_refs import KnowledgeDocumentRef, source_document_key
from src.core.ledger.ulid import new_ulid


class KBEventExtractorError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ExtractedKBEventBatch:
    batch_id: str
    candidates: tuple[CandidateEvent, ...]


def extract_event_candidates_from_markdown(
    *,
    markdown_text: str,
    program_id: str,
    vault_hash: str,
    original_filename: str,
    origin_path: str | None,
    ingested_at: datetime,
    batch_id: str,
    pipeline: str = "kb_extract",
) -> ExtractedKBEventBatch:
    candidates: list[CandidateEvent] = []
    occurred_at = ingested_at.astimezone(timezone.utc) if ingested_at.tzinfo is not None else ingested_at.replace(tzinfo=timezone.utc)
    for line_number, raw_line in enumerate(markdown_text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            parsed = parse_structured_event_marker_result(stripped)
        except StructuredEventMarkerError as error:
            raise KBEventExtractorError(str(error)) from error
        if parsed is None:
            continue
        event_type = parsed.event_type
        payload = parsed.payload
        validate_event_payload(event_type, payload)
        source_ref = KnowledgeDocumentRef(
            vault_hash=vault_hash,
            original_filename=original_filename,
            origin_kind="local_path",
            origin_path=origin_path,
            origin_url=None,
            ingested_at=ingested_at,
            section=f"line:{line_number}",
        )
        dedupe_payload = _dedupe_payload_for(event_type, payload)
        dedupe_core_hash = compute_dedupe_core_hash(event_type, dedupe_payload)
        document_key = source_document_key(source_ref)
        event_occurred_at, temporal_confidence = marker_occurrence_or_default(
            parsed,
            default_occurred_at=occurred_at,
            default_temporal_confidence="approximate",
        )
        candidates.append(
            CandidateEvent(
                candidate_id=new_ulid(datetime.now(timezone.utc)),
                program_id=program_id,
                proposed_event_type=event_type,
                proposed_payload=payload,
                proposed_occurred_at=event_occurred_at,
                proposed_temporal_confidence=temporal_confidence,
                proposed_confidence=ConfidenceTier.AI_EXTRACTED.value,
                source_ref=source_ref,
                pipeline=pipeline,
                extraction_confidence=0.95,
                entity_resolution=_entity_resolution(event_type, payload),
                dedupe_key=derive_candidate_dedupe_key(document_key, dedupe_core_hash),
                dedupe_core_hash=dedupe_core_hash,
                source_document_key=document_key,
                corroborating_refs=(),
                batch_id=batch_id,
            )
        )
    return ExtractedKBEventBatch(batch_id=batch_id, candidates=tuple(candidates))


def _entity_resolution(event_type: str, payload: dict[str, Any]) -> tuple[CandidateEntityResolution, ...]:
    schema = get_event_schema(event_type)
    return tuple(CandidateEntityResolution(**item) for item in entity_resolution_from_payload(schema.entity_ref_fields, payload))


def _dedupe_payload_for(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    schema = get_event_schema(event_type)
    if not schema.dedupe_core_fields:
        return dict(payload)
    return {field_name: payload[field_name] for field_name in schema.dedupe_core_fields if field_name in payload}
