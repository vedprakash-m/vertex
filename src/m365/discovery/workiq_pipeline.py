from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.ai.discovery._structured_event_markers import (
    StructuredEventMarkerError,
    entity_resolution_from_payload,
    marker_occurrence_or_default,
    parse_structured_event_marker,
    parse_structured_event_marker_result,
)
from src.core.config_loader import PROGRAMS_ROOT
from src.core.edition_resolver import load_program
from src.core.ledger.candidate_store import CandidateEntityResolution, CandidateEvent, derive_candidate_dedupe_key
from src.core.ledger.discovery_run_recorder import GapDetail
from src.core.ledger.event_log import ConfidenceTier, compute_dedupe_core_hash
from src.core.ledger.event_types import get_event_schema, validate_event_payload
from src.core.ledger.evidence_vault import store_evidence_vault_bytes
from src.core.ledger.source_refs import WorkIQRef, source_document_key
from src.core.ledger.ulid import new_ulid
from src.core.models_v2 import Program
from src.m365.agency_bridge import AgencyBridge


class WorkIQPipelineError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WorkIQPipelineBatch:
    candidates: tuple[CandidateEvent, ...]
    gaps: tuple[GapDetail, ...]


def run_workiq_pipeline(
    *,
    program_id: str,
    batch_id: str,
    pipeline: str = "workiq",
    from_year: int | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> WorkIQPipelineBatch:
    program = load_program(program_id, programs_root=programs_root)
    if program is None:
        raise WorkIQPipelineError(f"Program '{program_id}' is missing program.yaml.")

    queries = _configured_workiq_queries(program)
    if not queries:
        return WorkIQPipelineBatch(
            candidates=(),
            gaps=(
                GapDetail(
                    gap_kind="misconfigured_source",
                    window_start=None,
                    window_end=None,
                    detail=(
                        f"Program '{program_id}' does not define any enabled WorkIQ discovery queries under "
                        "program.m365.workiq_queries."
                    ),
                ),
            ),
        )

    bridge = AgencyBridge()
    capabilities = bridge.probe()
    if not capabilities.available and not capabilities.has_workiq_cli:
        return WorkIQPipelineBatch(
            candidates=(),
            gaps=(
                GapDetail(
                    gap_kind="auth_failure",
                    window_start=None,
                    window_end=None,
                    detail="Agency CLI is unavailable, so WorkIQ discovery could not run.",
                ),
            ),
        )
    if not capabilities.has_workiq and not capabilities.has_workiq_cli:
        return WorkIQPipelineBatch(
            candidates=(),
            gaps=(
                GapDetail(
                    gap_kind="auth_failure",
                    window_start=None,
                    window_end=None,
                    detail="WorkIQ MCP access is unavailable, so WorkIQ discovery could not run.",
                ),
            ),
        )

    candidates: list[CandidateEvent] = []
    gaps: list[GapDetail] = []
    for query_name, query_text in queries:
        retrieved_at = datetime.now(timezone.utc)
        question = _build_workiq_marker_question(query_name=query_name, query_text=query_text, from_year=from_year)
        payload = bridge.ask_workiq(question)
        if payload is None:
            detail = bridge.last_mcp_error() or f"WorkIQ query '{query_name}' returned no payload."
            gaps.append(
                GapDetail(
                    gap_kind="runtime_blocked",
                    window_start=None,
                    window_end=None,
                    detail=f"WorkIQ query '{query_name}' failed: {detail}",
                )
            )
            continue

        response_text = _extract_response_text(payload)
        if response_text is None:
            gaps.append(
                GapDetail(
                    gap_kind="runtime_blocked",
                    window_start=None,
                    window_end=None,
                    detail=f"WorkIQ query '{query_name}' returned no text response.",
                )
            )
            continue
        if response_text.strip().upper() == "NO_EVENTS":
            continue

        try:
            candidates.extend(
                _extract_candidates_from_response(
                    program_id=program_id,
                    query_name=query_name,
                    response_text=response_text,
                    retrieved_at=retrieved_at,
                    batch_id=batch_id,
                    pipeline=pipeline,
                    programs_root=programs_root,
                )
            )
        except WorkIQPipelineError as error:
            gaps.append(
                GapDetail(
                    gap_kind="parse_failure",
                    window_start=None,
                    window_end=None,
                    detail=f"WorkIQ query '{query_name}' failed to parse: {error}",
                )
            )

    return WorkIQPipelineBatch(
        candidates=_collapse_workiq_candidates(candidates),
        gaps=tuple(gaps),
    )


def _configured_workiq_queries(program: Program) -> tuple[tuple[str, str], ...]:
    if program.m365 is None or not program.m365.enabled or not program.m365.workiq_queries:
        return ()
    return tuple(
        (name.strip(), query.strip())
        for name, query in sorted(program.m365.workiq_queries.items())
        if name.strip() and query.strip()
    )


def _build_workiq_marker_question(*, query_name: str, query_text: str, from_year: int | None) -> str:
    year_hint = (
        f"Focus only on events that occurred on or after January 1, {from_year}. "
        if from_year is not None
        else ""
    )
    return (
        "Return only structured event markers, one per line, with no prose before or after. "
        "Supported markers are exactly: "
        "`Decision:`, `Risk:`, `Milestone:`, and `Metric:`. "
        "If you find nothing, return exactly `NO_EVENTS`. "
        "Use the existing marker field syntax Vertex expects. "
        f"{year_hint}"
        f"Query name: {query_name}. "
        f"Question: {query_text}"
    )


def _extract_response_text(payload: dict[str, Any]) -> str | None:
    for key in ("response", "summary", "content", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_candidates_from_response(
    *,
    program_id: str,
    query_name: str,
    response_text: str,
    retrieved_at: datetime,
    batch_id: str,
    pipeline: str,
    programs_root: Path,
) -> tuple[CandidateEvent, ...]:
    vault_entry = store_evidence_vault_bytes(
        program_id=program_id,
        content_bytes=response_text.encode("utf-8"),
        content_type="text/plain",
        original_filename=f"{query_name}.txt",
        origin_path=f"workiq://query/{query_name}",
        programs_root=programs_root,
    )
    source_ref = WorkIQRef(
        artifact_id=query_name,
        artifact_kind="workiq_query",
        retrieved_at=retrieved_at,
        vault_hash=vault_entry.vault_hash,
    )
    document_key = source_document_key(source_ref)
    candidates: list[CandidateEvent] = []
    for raw_line in response_text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.upper() == "NO_EVENTS":
            continue
        try:
            parsed = parse_structured_event_marker_result(stripped)
        except StructuredEventMarkerError as error:
            raise WorkIQPipelineError(str(error)) from error
        if parsed is None:
            continue
        event_type = parsed.event_type
        payload = parsed.payload
        validate_event_payload(event_type, payload)
        dedupe_payload = _dedupe_payload_for(event_type, payload)
        dedupe_core_hash = compute_dedupe_core_hash(event_type, dedupe_payload)
        occurred_at, temporal_confidence = marker_occurrence_or_default(
            parsed,
            default_occurred_at=retrieved_at,
            default_temporal_confidence="reconstructed",
        )
        candidates.append(
            CandidateEvent(
                candidate_id=new_ulid(datetime.now(timezone.utc)),
                program_id=program_id,
                proposed_event_type=event_type,
                proposed_payload=payload,
                proposed_occurred_at=occurred_at,
                proposed_temporal_confidence=temporal_confidence,
                proposed_confidence=ConfidenceTier.AI_EXTRACTED.value,
                source_ref=source_ref,
                pipeline=pipeline,
                extraction_confidence=0.7,
                entity_resolution=_entity_resolution(event_type, payload),
                dedupe_key=derive_candidate_dedupe_key(document_key, dedupe_core_hash),
                dedupe_core_hash=dedupe_core_hash,
                source_document_key=document_key,
                corroborating_refs=(),
                batch_id=batch_id,
            )
        )
    return tuple(candidates)


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


def _collapse_workiq_candidates(candidates: list[CandidateEvent]) -> tuple[CandidateEvent, ...]:
    grouped: dict[tuple[str, str], list[CandidateEvent]] = {}
    for candidate in sorted(candidates, key=_workiq_candidate_sort_key):
        grouped.setdefault((candidate.proposed_event_type, candidate.dedupe_core_hash), []).append(candidate)
    collapsed: list[CandidateEvent] = []
    for key in sorted(grouped):
        collapsed.append(_merge_workiq_candidate_group(grouped[key]))
    return tuple(collapsed)


def _merge_workiq_candidate_group(group: list[CandidateEvent]) -> CandidateEvent:
    primary = group[0]
    corroborating_refs = list(primary.corroborating_refs)
    seen_refs = {_source_ref_identity(primary.source_ref)}
    for candidate in group[1:]:
        for ref in (candidate.source_ref, *candidate.corroborating_refs):
            identity = _source_ref_identity(ref)
            if identity in seen_refs:
                continue
            corroborating_refs.append(ref)
            seen_refs.add(identity)
    if len(corroborating_refs) == len(primary.corroborating_refs):
        return primary
    return CandidateEvent(
        candidate_id=primary.candidate_id,
        program_id=primary.program_id,
        proposed_event_type=primary.proposed_event_type,
        proposed_payload=primary.proposed_payload,
        proposed_occurred_at=primary.proposed_occurred_at,
        proposed_temporal_confidence=primary.proposed_temporal_confidence,
        proposed_confidence=primary.proposed_confidence,
        source_ref=primary.source_ref,
        pipeline=primary.pipeline,
        extraction_confidence=primary.extraction_confidence,
        entity_resolution=primary.entity_resolution,
        dedupe_key=primary.dedupe_key,
        dedupe_core_hash=primary.dedupe_core_hash,
        source_document_key=primary.source_document_key,
        corroborating_refs=tuple(corroborating_refs),
        batch_id=primary.batch_id,
        staged_at=primary.staged_at,
    )


def _workiq_candidate_sort_key(candidate: CandidateEvent) -> tuple[datetime, str, str]:
    artifact_id = getattr(candidate.source_ref, "artifact_id", "") or ""
    return (candidate.proposed_occurred_at, artifact_id, candidate.candidate_id)


def _source_ref_identity(source_ref: object) -> str:
    return repr(source_ref)
