"""OSD-7 Wave 1/2/3: AI-powered prose-to-event extraction.

Extracts governed CandidateEvents from arbitrary prose text (newsletters, LT
decks, KB articles) using the LLM frontier tier.  Marker-based extractors yield
~0 candidates from real Acme prose because authors don't prefix lines with
``Decision:`` / ``Risk:``  etc.  This extractor bridges that gap by sending
prose through the structured-output pipeline and mapping LLM output to the
same CandidateEvent dataclass used by all other Zone B extractors.

Wave 1 event types: decision.made.v1, risk.raised.v1, milestone.created/completed.v1,
metric.observed.v1.
Wave 2 event types: program.phase_entered/exited.v1, program.scope_changed.v1,
program.charter_revised.v1, workstream.created/owner_changed/status_changed.v1.
Wave 3 event types: commitment.made/slipped/fulfilled.v1, assumption.stated/validated/invalidated.v1,
dependency.declared/status_changed.v1, incident.opened/resolved.v1.
Wave 4 event types: knowledge.article_added.v1, sku_generation.added.v1, kpi.defined/decommissioned/threshold_crossed.v1.

Safety contract:
  - All text fields run through process_generated_text() (PII scrub + injection check).
  - ConfidenceTier.AI_EXTRACTED is used (never SOURCE_AUTHORITATIVE).
  - extraction_confidence is capped at 0.85.
  - Invalid event types or payloads are skipped with a warning, never raised.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.ai._pipeline import AIPipelineError, process_generated_text
from src.ai.discovery._structured_event_markers import entity_resolution_from_payload
from src.ai.prompt_registry import load_prompt
from src.ai.provider import LLMProvider
from src.ai.tiered_router import route_through_tiers
from src.core.ledger.candidate_store import CandidateEntityResolution, CandidateEvent, derive_candidate_dedupe_key
from src.core.ledger.event_log import ConfidenceTier, TemporalConfidence, compute_dedupe_core_hash
from src.core.ledger.event_types import get_event_schema, validate_event_payload
from src.core.ledger.source_refs import SourceRef, source_document_key
from src.core.ledger.ulid import new_ulid
from src.core.policy_loader import AIFeaturePolicy, load_ai_feature_policy


PROMPT_VERSION = "prose_event_extractor.v1"
PROMPT_VERSION_V2 = "prose_event_extractor.v2"
PROMPT_VERSION_V3 = "prose_event_extractor.v3"
PROMPT_VERSION_V4 = "prose_event_extractor.v4"
_FEATURE = "prose_event_extractor"
_MAX_PROSE_CHARS = 6000
_EXTRACTION_CONFIDENCE_CAP = 0.85

_WAVE1_EVENT_TYPES: frozenset[str] = frozenset({
    "decision.made.v1",
    "risk.raised.v1",
    "milestone.created.v1",
    "milestone.completed.v1",
    "metric.observed.v1",
})

_WAVE2_EVENT_TYPES: frozenset[str] = frozenset({
    "program.phase_entered.v1",
    "program.phase_exited.v1",
    "program.scope_changed.v1",
    "program.charter_revised.v1",
    "workstream.created.v1",
    "workstream.owner_changed.v1",
    "workstream.status_changed.v1",
})

_WAVE3_EVENT_TYPES: frozenset[str] = frozenset({
    "commitment.made.v1",
    "commitment.slipped.v1",
    "commitment.fulfilled.v1",
    "assumption.stated.v1",
    "assumption.validated.v1",
    "assumption.invalidated.v1",
    "dependency.declared.v1",
    "dependency.status_changed.v1",
    "incident.opened.v1",
    "incident.resolved.v1",
})

_WAVE4_EVENT_TYPES: frozenset[str] = frozenset({
    "knowledge.article_added.v1",
    "sku_generation.added.v1",
    "kpi.defined.v1",
    "kpi.decommissioned.v1",
    "kpi.threshold_crossed.v1",
})

_WAVE_EVENT_TYPES: dict[int, frozenset[str]] = {
    1: _WAVE1_EVENT_TYPES,
    2: _WAVE2_EVENT_TYPES,
    3: _WAVE3_EVENT_TYPES,
    4: _WAVE4_EVENT_TYPES,
}
_WAVE_PROMPT_VERSIONS: dict[int, str] = {
    1: PROMPT_VERSION,
    2: PROMPT_VERSION_V2,
    3: PROMPT_VERSION_V3,
    4: PROMPT_VERSION_V4,
}

_VALID_TEMPORAL_CONFIDENCES: frozenset[str] = frozenset({
    TemporalConfidence.EXACT,
    TemporalConfidence.APPROXIMATE,
    TemporalConfidence.ESTIMATED,
    TemporalConfidence.RECONSTRUCTED,
})


class ProseEventExtractorError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ExtractedProseCandidateBatch:
    batch_id: str
    candidates: tuple[CandidateEvent, ...]
    warnings: tuple[str, ...]


def extract_prose_event_candidates(
    *,
    prose_text: str,
    program_id: str,
    source_ref: SourceRef,
    batch_id: str,
    default_occurred_at: datetime,
    pipeline: str = "prose_extract",
    wave: int = 1,
    client: LLMProvider | None = None,
    policy: AIFeaturePolicy | None = None,
) -> ExtractedProseCandidateBatch:
    """Extract CandidateEvents from arbitrary prose via the LLM frontier tier.

    Args:
        wave: Extraction wave (1 = decision/risk/milestone/metric; 2 = phase/scope/workstream; 3 = commitment/assumption/dependency/incident; 4 = knowledge/sku_generation/kpi).

    Returns an empty batch (not an error) when:
      - prose_text is blank
      - client is None (AI unavailable)
      - frontier is blocked by policy/mode

    Individual bad events are recorded in warnings and skipped.
    """
    if wave not in _WAVE_EVENT_TYPES:
        raise ProseEventExtractorError(f"Unsupported extraction wave: {wave!r}. Must be one of {sorted(_WAVE_EVENT_TYPES)}")

    if not prose_text.strip():
        return ExtractedProseCandidateBatch(batch_id=batch_id, candidates=(), warnings=())

    if client is None:
        return ExtractedProseCandidateBatch(
            batch_id=batch_id,
            candidates=(),
            warnings=("No AI client provided; prose extraction skipped.",),
        )

    effective_policy = policy or load_ai_feature_policy(_FEATURE)
    prompt_version = _WAVE_PROMPT_VERSIONS[wave]
    raw_events, llm_warning = _extract_via_llm(client, effective_policy, prose_text, default_occurred_at, prompt_version)

    document_key = source_document_key(source_ref)
    candidates: list[CandidateEvent] = []
    warnings: list[str] = []
    if llm_warning:
        warnings.append(llm_warning)

    valid_event_types = _WAVE_EVENT_TYPES[wave]
    for idx, raw_event in enumerate(raw_events):
        try:
            candidate = _build_candidate(
                raw_event=raw_event,
                program_id=program_id,
                source_ref=source_ref,
                document_key=document_key,
                batch_id=batch_id,
                pipeline=pipeline,
                default_occurred_at=default_occurred_at,
                valid_event_types=valid_event_types,
            )
        except (ProseEventExtractorError, ValueError) as error:
            warnings.append(f"event[{idx}] skipped: {error}")
            continue
        candidates.append(candidate)

    return ExtractedProseCandidateBatch(
        batch_id=batch_id,
        candidates=tuple(candidates),
        warnings=tuple(warnings),
    )


def _extract_via_llm(
    client: LLMProvider,
    policy: AIFeaturePolicy,
    prose_text: str,
    default_occurred_at: datetime,
    prompt_version: str = PROMPT_VERSION,
) -> tuple[list[dict[str, Any]], str | None]:
    """Return (raw_event_list, optional_warning_string)."""
    prompt_template = load_prompt(prompt_version, error_factory=ProseEventExtractorError)
    user_prompt = _build_user_prompt(prose_text, default_occurred_at)

    result = route_through_tiers(
        _FEATURE,
        deterministic_fn=lambda: None,
        frontier_fn=lambda: client.structured(
            prompt_template,
            user_prompt,
            parser=_parse_llm_response,
            max_tokens=policy.max_tokens,
            prompt_version=prompt_version,
        ),
        policy=policy,
    )

    if result.value is None:
        outcome = result.decision.outcome
        return [], f"Prose extraction frontier not reached (outcome={outcome})."
    return result.value, None


def _build_user_prompt(prose_text: str, default_occurred_at: datetime) -> str:
    date_str = default_occurred_at.strftime("%Y-%m-%d")
    truncated = prose_text[:_MAX_PROSE_CHARS]
    if len(prose_text) > _MAX_PROSE_CHARS:
        truncated += "\n[... text truncated ...]"
    return f"SOURCE DATE: {date_str}\n\nSOURCE TEXT:\n{truncated}"


def _parse_llm_response(payload: dict[str, Any]) -> list[dict[str, Any]]:
    events = payload.get("events")
    if not isinstance(events, list):
        return []
    return [item for item in events if isinstance(item, dict)]


def _build_candidate(
    *,
    raw_event: dict[str, Any],
    program_id: str,
    source_ref: SourceRef,
    document_key: str,
    batch_id: str,
    pipeline: str,
    default_occurred_at: datetime,
    valid_event_types: frozenset[str] = _WAVE1_EVENT_TYPES,
) -> CandidateEvent:
    event_type = _require_str(raw_event, "event_type")
    if event_type not in valid_event_types:
        raise ProseEventExtractorError(f"Unsupported event type: {event_type!r}")

    raw_payload = raw_event.get("payload")
    if not isinstance(raw_payload, dict):
        raise ProseEventExtractorError(f"Missing payload dict for {event_type}")

    safe_payload = _scrub_payload(event_type, raw_payload)
    validate_event_payload(event_type, safe_payload)

    occurred_at, temporal_confidence = _parse_temporal(
        raw_event.get("occurred_at"),
        raw_event.get("temporal_confidence", "estimated"),
        default_occurred_at,
    )

    raw_conf = raw_event.get("extraction_confidence", 0.75)
    extraction_confidence = min(_EXTRACTION_CONFIDENCE_CAP, max(0.0, float(raw_conf)))

    schema = get_event_schema(event_type)
    dedupe_payload = {k: safe_payload[k] for k in schema.dedupe_core_fields if k in safe_payload}
    dedupe_core_hash = compute_dedupe_core_hash(event_type, dedupe_payload)
    entity_resolution = tuple(
        CandidateEntityResolution(**item)
        for item in entity_resolution_from_payload(schema.entity_ref_fields, safe_payload)
    )

    return CandidateEvent(
        candidate_id=new_ulid(datetime.now(timezone.utc)),
        program_id=program_id,
        proposed_event_type=event_type,
        proposed_payload=safe_payload,
        proposed_occurred_at=occurred_at,
        proposed_temporal_confidence=temporal_confidence,
        proposed_confidence=ConfidenceTier.AI_EXTRACTED.value,
        source_ref=source_ref,
        pipeline=pipeline,
        extraction_confidence=extraction_confidence,
        entity_resolution=entity_resolution,
        dedupe_key=derive_candidate_dedupe_key(document_key, dedupe_core_hash),
        dedupe_core_hash=dedupe_core_hash,
        source_document_key=document_key,
        corroborating_refs=(),
        batch_id=batch_id,
    )


def _scrub_payload(event_type: str, raw_payload: dict[str, Any]) -> dict[str, Any]:
    schema = get_event_schema(event_type)
    all_fields = schema.required_fields | schema.optional_fields
    scrubbed: dict[str, Any] = {}
    for key, value in raw_payload.items():
        if key not in all_fields:
            continue
        field_type = schema.field_types.get(key)
        if isinstance(value, str):
            try:
                scrubbed[key] = process_generated_text(value).text
            except AIPipelineError as error:
                raise ProseEventExtractorError(f"Unsafe field {key!r}: {error}") from error
        elif isinstance(value, list) and field_type == "list":
            safe_items: list[Any] = []
            for item in value:
                if isinstance(item, str):
                    try:
                        safe_items.append(process_generated_text(item).text)
                    except AIPipelineError as error:
                        raise ProseEventExtractorError(f"Unsafe list item in {key!r}: {error}") from error
                else:
                    safe_items.append(item)
            scrubbed[key] = safe_items
        else:
            scrubbed[key] = value
    return scrubbed


def _parse_temporal(
    raw_occurred_at: Any,
    temporal_confidence_raw: Any,
    default_occurred_at: datetime,
) -> tuple[datetime, str]:
    temporal_confidence = (
        str(temporal_confidence_raw)
        if isinstance(temporal_confidence_raw, str) and temporal_confidence_raw in _VALID_TEMPORAL_CONFIDENCES
        else "estimated"
    )
    if isinstance(raw_occurred_at, str) and raw_occurred_at.strip():
        try:
            normalized = raw_occurred_at.strip().replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc), temporal_confidence
        except ValueError:
            pass
    return default_occurred_at, "estimated"


def _require_str(payload: dict[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ProseEventExtractorError(f"Missing required string field: {field_name!r}")
    return value.strip()
