from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
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
from src.core.ledger.evidence_vault import store_evidence_vault_bytes
from src.core.ledger.source_refs import SharePointDocRef, source_document_key
from src.core.ledger.ulid import new_ulid


class SharePointDocExtractorError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ExtractedSharePointDocCandidateBatch:
    batch_id: str
    candidates: tuple[CandidateEvent, ...]


def extract_sharepoint_doc_candidates(
    *,
    program_id: str,
    source_path: Path,
    relative_path: str,
    site: str,
    batch_id: str,
    pipeline: str = "sharepoint_doc",
    programs_root: Path,
) -> ExtractedSharePointDocCandidateBatch:
    if not source_path.exists() or not source_path.is_file():
        raise SharePointDocExtractorError(f"SharePoint document source not found: {source_path}")

    raw_bytes = source_path.read_bytes()
    try:
        body_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        try:
            body_text = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise SharePointDocExtractorError(
                f"SharePoint document must be UTF-8 decodable text: {source_path}"
            ) from error

    content_type = "text/markdown" if source_path.suffix.lower() == ".md" else "text/plain"
    vault_entry = store_evidence_vault_bytes(
        program_id=program_id,
        content_bytes=raw_bytes,
        content_type=content_type,
        original_filename=source_path.name,
        origin_path=str(source_path),
        programs_root=programs_root,
    )
    modified_at = datetime.fromtimestamp(source_path.stat().st_mtime, tz=timezone.utc)

    candidates: list[CandidateEvent] = []
    for line in body_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = parse_structured_event_marker_result(stripped)
        except StructuredEventMarkerError as error:
            raise SharePointDocExtractorError(str(error)) from error
        if parsed is None:
            continue
        event_type = parsed.event_type
        payload = parsed.payload
        validate_event_payload(event_type, payload)
        source_ref = SharePointDocRef(
            site=site,
            doc_path=relative_path,
            version=None,
            modified_at=modified_at,
            vault_hash=vault_entry.vault_hash,
        )
        dedupe_payload = _dedupe_payload_for(event_type, payload)
        dedupe_core_hash = compute_dedupe_core_hash(event_type, dedupe_payload)
        document_key = source_document_key(source_ref)
        occurred_at, temporal_confidence = marker_occurrence_or_default(
            parsed,
            default_occurred_at=modified_at,
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
                extraction_confidence=0.9,
                entity_resolution=_entity_resolution(event_type, payload),
                dedupe_key=derive_candidate_dedupe_key(document_key, dedupe_core_hash),
                dedupe_core_hash=dedupe_core_hash,
                source_document_key=document_key,
                corroborating_refs=(),
                batch_id=batch_id,
            )
        )
    return ExtractedSharePointDocCandidateBatch(batch_id=batch_id, candidates=tuple(candidates))


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
