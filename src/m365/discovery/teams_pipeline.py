from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from src.ai.discovery._structured_event_markers import (
    StructuredEventMarkerError,
    entity_resolution_from_payload,
    marker_occurrence_or_default,
    parse_structured_event_marker_result,
)
from src.core.config_loader import PROGRAMS_ROOT
from src.core.edition_resolver import load_program
from src.core.ledger.candidate_store import CandidateEntityResolution, CandidateEvent, derive_candidate_dedupe_key
from src.core.ledger.discovery_run_recorder import GapDetail
from src.core.ledger.event_log import ConfidenceTier, compute_dedupe_core_hash
from src.core.ledger.event_types import get_event_schema, validate_event_payload
from src.core.ledger.evidence_vault import store_evidence_vault_bytes
from src.core.ledger.source_refs import TeamsMessageRef, source_document_key
from src.core.ledger.ulid import new_ulid
from src.core.m365_identifiers import normalize_thread_id
from src.core.models_v2 import Program, Workstream
from src.core.workstream_documents import get_workstreams_path, load_workstreams_document
from src.core.yaml_utils import load_yaml_mapping
from src.m365.agency_bridge import AgencyBridge
from src.m365.teams_reader import TeamsMessageRecord, TeamsReader


class TeamsPipelineError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TeamsPipelineBatch:
    candidates: tuple[CandidateEvent, ...]
    gaps: tuple[GapDetail, ...]


def run_teams_pipeline(
    *,
    program_id: str,
    batch_id: str,
    pipeline: str = "teams",
    from_year: int | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> TeamsPipelineBatch:
    program = load_program(program_id, programs_root=programs_root)
    if program is None:
        raise TeamsPipelineError(f"Program '{program_id}' is missing program.yaml.")
    if program.m365 is None or not program.m365.enabled:
        return TeamsPipelineBatch(
            candidates=(),
            gaps=(
                GapDetail(
                    gap_kind="misconfigured_source",
                    window_start=None,
                    window_end=None,
                    detail=f"Program '{program_id}' has m365.enabled=false; Teams discovery is disabled.",
                ),
            ),
        )

    workstreams = _load_workstreams(program_id, programs_root=programs_root)
    queries = _configured_teams_queries(workstreams)
    if not queries:
        return TeamsPipelineBatch(
            candidates=(),
            gaps=(
                GapDetail(
                    gap_kind="misconfigured_source",
                    window_start=None,
                    window_end=None,
                    detail=(
                        f"Program '{program_id}' has no Teams discovery seeds in workstreams.yaml "
                        "(expected workiq_keywords or teams_chats display names)."
                    ),
                ),
            ),
        )

    bridge = AgencyBridge()
    capabilities = bridge.probe()
    if not capabilities.available and not capabilities.has_workiq_cli:
        return TeamsPipelineBatch(
            candidates=(),
            gaps=(
                GapDetail(
                    gap_kind="auth_failure",
                    window_start=None,
                    window_end=None,
                    detail="Agency CLI is unavailable, so Teams discovery could not run.",
                ),
            ),
        )
    if not capabilities.has_workiq and not capabilities.has_workiq_cli:
        return TeamsPipelineBatch(
            candidates=(),
            gaps=(
                GapDetail(
                    gap_kind="auth_failure",
                    window_start=None,
                    window_end=None,
                    detail="WorkIQ MCP access is unavailable, so Teams discovery could not run.",
                ),
            ),
        )

    reader = TeamsReader(bridge)
    since = f"{from_year:04d}-01-01" if from_year is not None else None
    candidates: list[CandidateEvent] = []
    gaps: list[GapDetail] = []
    for query in queries:
        page = reader.search_messages(
            channel="all",
            query=query,
            since=since,
            limit=25,
        )
        if not page.records:
            runtime_error = bridge.last_mcp_error()
            if runtime_error:
                gaps.append(
                    GapDetail(
                        gap_kind="runtime_blocked",
                        window_start=None,
                        window_end=None,
                        detail=f"Teams discovery query '{query}' failed: {runtime_error}",
                    ),
                )
            continue
        for record in page.records:
            try:
                candidates.extend(
                    _extract_candidates_from_record(
                        program_id=program_id,
                        record=record,
                        batch_id=batch_id,
                        pipeline=pipeline,
                        programs_root=programs_root,
                    )
                )
            except TeamsPipelineError as error:
                gaps.append(
                    GapDetail(
                        gap_kind="parse_failure",
                        window_start=None,
                        window_end=None,
                        detail=f"Teams discovery record could not be parsed: {error}",
                    ),
                )
    return TeamsPipelineBatch(
        candidates=_collapse_teams_candidates(candidates),
        gaps=tuple(gaps),
    )


def _load_workstreams(program_id: str, *, programs_root: Path) -> tuple[Workstream, ...]:
    workstreams_path = get_workstreams_path(program_id, programs_root)
    raw = load_yaml_mapping(workstreams_path)
    return load_workstreams_document(raw, workstreams_path)


def _configured_teams_queries(workstreams: tuple[Workstream, ...]) -> tuple[str, ...]:
    queries: list[str] = []
    seen: set[str] = set()

    def add_query(value: str | None) -> None:
        if value is None:
            return
        text = " ".join(value.split())
        if not text:
            return
        lowered = text.lower()
        if lowered in seen:
            return
        seen.add(lowered)
        queries.append(text)

    for workstream in workstreams:
        signal_sources = workstream.signal_sources
        if signal_sources is None:
            continue
        for keyword in signal_sources.workiq_keywords:
            add_query(keyword)
        for chat in signal_sources.teams_chats:
            add_query(chat.display_name)
    return tuple(queries)


def _extract_candidates_from_record(
    *,
    program_id: str,
    record: TeamsMessageRecord,
    batch_id: str,
    pipeline: str,
    programs_root: Path,
) -> tuple[CandidateEvent, ...]:
    payload_bytes = json.dumps(asdict(record), sort_keys=True).encode("utf-8")
    identity = record.source_id or record.thread_id or record.conversation_id or "teams-message"
    vault_entry = store_evidence_vault_bytes(
        program_id=program_id,
        content_bytes=payload_bytes,
        content_type="application/json",
        original_filename=f"{identity}.json",
        origin_path=record.web_url or f"teams://{identity}",
        programs_root=programs_root,
    )
    posted_at, temporal_confidence = _posted_at_for_record(record)
    source_ref = TeamsMessageRef(
        posted_at=posted_at,
        team=None,
        channel=record.channel,
        message_id=record.source_id,
        thread_id=normalize_thread_id(record.thread_id or record.conversation_id or record.web_url),
        vault_hash=vault_entry.vault_hash,
    )
    document_key = source_document_key(source_ref)
    candidates: list[CandidateEvent] = []
    for raw_line in _record_marker_lines(record):
        try:
            parsed = parse_structured_event_marker_result(raw_line)
        except StructuredEventMarkerError as error:
            raise TeamsPipelineError(str(error)) from error
        if parsed is None:
            continue
        event_type = parsed.event_type
        payload = parsed.payload
        validate_event_payload(event_type, payload)
        dedupe_payload = _dedupe_payload_for(event_type, payload)
        dedupe_core_hash = compute_dedupe_core_hash(event_type, dedupe_payload)
        occurred_at, temporal_confidence = marker_occurrence_or_default(
            parsed,
            default_occurred_at=posted_at,
            default_temporal_confidence=temporal_confidence,
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
    return tuple(candidates)


def _record_marker_lines(record: TeamsMessageRecord) -> tuple[str, ...]:
    lines: list[str] = []
    for value in (record.title, record.preview):
        if value is None:
            continue
        for raw_line in value.splitlines():
            stripped = raw_line.strip()
            if stripped:
                lines.append(stripped)
    return tuple(lines)


def _posted_at_for_record(record: TeamsMessageRecord) -> tuple[datetime, str]:
    raw = (record.sent_at or "").strip()
    if not raw:
        return datetime.now(timezone.utc), "reconstructed"
    normalized = raw.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc), "approximate"
    return parsed.astimezone(timezone.utc), "exact"


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


def _collapse_teams_candidates(candidates: list[CandidateEvent]) -> tuple[CandidateEvent, ...]:
    grouped: dict[tuple[str, str], list[CandidateEvent]] = {}
    for candidate in sorted(candidates, key=_teams_candidate_sort_key):
        grouped.setdefault((candidate.proposed_event_type, candidate.dedupe_core_hash), []).append(candidate)
    collapsed: list[CandidateEvent] = []
    for key in sorted(grouped):
        collapsed.append(_merge_teams_candidate_group(grouped[key]))
    return tuple(collapsed)


def _merge_teams_candidate_group(group: list[CandidateEvent]) -> CandidateEvent:
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


def _teams_candidate_sort_key(candidate: CandidateEvent) -> tuple[datetime, str, str]:
    message_id = getattr(candidate.source_ref, "message_id", None) or ""
    thread_id = getattr(candidate.source_ref, "thread_id", None) or ""
    return (candidate.proposed_occurred_at, message_id or thread_id, candidate.candidate_id)


def _source_ref_identity(source_ref: object) -> str:
    return repr(source_ref)
