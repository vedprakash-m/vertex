from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.ai.discovery._mime_text import MIMETextError, parse_eml_message
from src.ai.discovery._structured_event_markers import (
    StructuredEventMarkerError,
    entity_resolution_from_payload,
    marker_occurrence_or_default,
    parse_structured_event_marker_result,
)
from src.core.ledger.candidate_store import CandidateEntityResolution, CandidateEvent, derive_candidate_dedupe_key
from src.core.ledger.event_log import ConfidenceTier, compute_dedupe_core_hash
from src.core.ledger.event_types import get_event_schema, validate_event_payload
from src.core.ledger.evidence_vault import store_evidence_vault_bytes
from src.core.ledger.source_refs import EmailRef, source_document_key
from src.core.ledger.ulid import new_ulid


class EmailExtractorError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ExtractedEmailCandidateBatch:
    batch_id: str
    candidates: tuple[CandidateEvent, ...]


def extract_email_candidates(
    *,
    program_id: str,
    source_path: Path,
    batch_id: str,
    pipeline: str = "email",
    programs_root: Path,
) -> ExtractedEmailCandidateBatch:
    if not source_path.exists() or not source_path.is_file():
        raise EmailExtractorError(f"Email source not found: {source_path}")
    try:
        message = parse_eml_message(source_path)
    except MIMETextError as error:
        raise EmailExtractorError(str(error)) from error
    vault_entry = store_evidence_vault_bytes(
        program_id=program_id,
        content_bytes=source_path.read_bytes(),
        content_type="message/rfc822",
        original_filename=source_path.name,
        origin_path=str(source_path),
        programs_root=programs_root,
    )
    sent_at = message.sent_at
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    sent_at = sent_at.astimezone(timezone.utc)
    folder = source_path.parent.name if source_path.parent.name else None
    candidates: list[CandidateEvent] = []
    for line in message.body_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = parse_structured_event_marker_result(stripped)
        except StructuredEventMarkerError as error:
            raise EmailExtractorError(str(error)) from error
        if parsed is None:
            continue
        event_type = parsed.event_type
        payload = parsed.payload
        validate_event_payload(event_type, payload)
        source_ref = EmailRef(
            subject=message.subject,
            sent_at=sent_at,
            sender=message.sender,
            message_id=message.message_id,
            folder=folder,
            vault_hash=vault_entry.vault_hash,
        )
        dedupe_payload = _dedupe_payload_for(event_type, payload)
        dedupe_core_hash = compute_dedupe_core_hash(event_type, dedupe_payload)
        document_key = source_document_key(source_ref)
        occurred_at, temporal_confidence = marker_occurrence_or_default(
            parsed,
            default_occurred_at=sent_at,
            default_temporal_confidence="exact",
        )
        candidates.append(
            CandidateEvent(
                candidate_id=new_ulid(datetime.now(timezone.utc)),
                program_id=program_id,
                proposed_event_type=event_type,
                proposed_payload=payload,
                proposed_occurred_at=occurred_at,
                proposed_temporal_confidence=temporal_confidence,
                proposed_confidence=ConfidenceTier.SOURCE_AUTHORITATIVE.value,
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
    return ExtractedEmailCandidateBatch(batch_id=batch_id, candidates=tuple(candidates))


def _entity_resolution(event_type: str, payload: dict[str, Any]) -> tuple[CandidateEntityResolution, ...]:
    schema = get_event_schema(event_type)
    return tuple(
        CandidateEntityResolution(**item)
        for item in entity_resolution_from_payload(schema.entity_ref_fields, payload)
    )


def _dedupe_payload_for(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    schema = get_event_schema(event_type)
    if not schema.dedupe_core_fields:
        return dict(payload)
    return {field_name: payload[field_name] for field_name in schema.dedupe_core_fields if field_name in payload}
