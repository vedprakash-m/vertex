from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from src.ai.discovery._structured_event_markers import (
    StructuredEventMarkerError,
    entity_resolution_from_payload,
    marker_occurrence_or_default,
    parse_structured_event_marker_result,
)
from src.core.circuit_breaker import CircuitBreaker
from src.core.config_loader import PROGRAMS_ROOT
from src.core.edition_resolver import get_program_output_dir, load_program
from src.core.knowledge_store import load_program_knowledge, select_engms_pages
from src.core.ledger.candidate_store import CandidateEntityResolution, CandidateEvent, derive_candidate_dedupe_key
from src.core.ledger.discovery_run_recorder import GapDetail
from src.core.ledger.event_log import ConfidenceTier, compute_dedupe_core_hash
from src.core.ledger.event_types import get_event_schema, validate_event_payload
from src.core.ledger.evidence_vault import store_evidence_vault_bytes
from src.core.ledger.source_refs import SharePointDocRef, source_document_key
from src.core.ledger.ulid import new_ulid
from src.core.models_v2 import EngMsPage
from src.m365.agency_bridge import AgencyBridge


class SharePointPipelineError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SharePointPipelineBatch:
    candidates: tuple[CandidateEvent, ...]
    gaps: tuple[GapDetail, ...]


def run_sharepoint_pipeline(
    *,
    program_id: str,
    batch_id: str,
    pipeline: str = "sharepoint",
    from_year: int | None = None,
    programs_root: Path = PROGRAMS_ROOT,
    doc_states: dict[str, dict[str, Any]] | None = None,
    force_refresh: bool = False,
    as_of: datetime | None = None,
) -> SharePointPipelineBatch:
    """Run SharePoint discovery pipeline for all registered engms_pages.yaml entries.

    Args:
        doc_states: Runtime extraction state from gather_state.json
                    m365_discovery["sharepoint"]["doc_states"]. Keyed by page id.
                    NOT read or written directly — caller passes in and updates state.
        force_refresh: If True, bypass change-detection and re-extract all docs.
        as_of: Reference time for change detection (defaults to now UTC).
    """
    program = load_program(program_id, programs_root=programs_root)
    if program is None:
        raise SharePointPipelineError(f"Program '{program_id}' is missing program.yaml.")
    if program.m365 is None or not program.m365.enabled:
        return SharePointPipelineBatch(
            candidates=(),
            gaps=(
                GapDetail(
                    gap_kind="misconfigured_source",
                    window_start=None,
                    window_end=None,
                    detail=f"Program '{program_id}' has m365.enabled=false; SharePoint discovery is disabled.",
                ),
            ),
        )

    knowledge = load_program_knowledge(program_id, programs_root=programs_root)
    pages = tuple(
        page
        for page in select_engms_pages(knowledge, program_id=program_id)
        if _is_sharepoint_page(page)
    )
    if not pages:
        return SharePointPipelineBatch(
            candidates=(),
            gaps=(
                GapDetail(
                    gap_kind="misconfigured_source",
                    window_start=None,
                    window_end=None,
                    detail=(
                        f"Program '{program_id}' has no SharePoint document URLs registered in knowledge/engms_pages.yaml."
                    ),
                ),
            ),
        )

    bridge = AgencyBridge(workiq_breaker=_build_workiq_breaker(program_id=program_id, programs_root=programs_root))
    capabilities = bridge.probe()
    if not capabilities.available and not capabilities.has_workiq_cli:
        return SharePointPipelineBatch(
            candidates=(),
            gaps=(
                GapDetail(
                    gap_kind="auth_failure",
                    window_start=None,
                    window_end=None,
                    detail="Agency CLI is unavailable, so SharePoint discovery could not run.",
                ),
            ),
        )
    if not capabilities.has_workiq and not capabilities.has_workiq_cli:
        return SharePointPipelineBatch(
            candidates=(),
            gaps=(
                GapDetail(
                    gap_kind="auth_failure",
                    window_start=None,
                    window_end=None,
                    detail="WorkIQ MCP access is unavailable, so SharePoint discovery could not run.",
                ),
            ),
        )

    resolved_doc_states = doc_states or {}
    resolved_as_of = as_of or datetime.now(timezone.utc)
    candidates: list[CandidateEvent] = []
    gaps: list[GapDetail] = []
    for page in pages:
        # SP2-4: Change-detection — skip if doc was recently extracted and not force-refreshed
        if not force_refresh and _should_skip_page(page, resolved_doc_states, resolved_as_of):
            continue
        retrieved_at = datetime.now(timezone.utc)
        if page.source_subtype == "lt_deck":
            question = build_lt_deck_evidence_question_nl(
                title=page.title,
                site_url=page.url,
                year=resolved_as_of.year,
            )
        else:
            question = _build_sharepoint_marker_question(page=page, from_year=from_year)
        payload = bridge.ask_workiq(question)
        if payload is None:
            detail = bridge.last_mcp_error() or f"SharePoint page '{page.id}' returned no payload."
            gaps.append(
                GapDetail(
                    gap_kind="runtime_blocked",
                    window_start=None,
                    window_end=None,
                    detail=f"SharePoint page '{page.id}' failed: {detail}",
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
                    detail=f"SharePoint page '{page.id}' returned no text response.",
                )
            )
            continue
        if response_text.strip().upper() == "NO_EVENTS":
            continue
        try:
            candidates.extend(
                _extract_candidates_from_response(
                    program_id=program_id,
                    page=page,
                    response_text=response_text,
                    retrieved_at=retrieved_at,
                    batch_id=batch_id,
                    pipeline=pipeline,
                    programs_root=programs_root,
                )
            )
        except SharePointPipelineError as error:
            gaps.append(
                GapDetail(
                    gap_kind="parse_failure",
                    window_start=None,
                    window_end=None,
                    detail=f"SharePoint page '{page.id}' failed to parse: {error}",
                )
            )
    return SharePointPipelineBatch(
        candidates=_collapse_sharepoint_candidates(candidates),
        gaps=tuple(gaps),
    )


def _build_workiq_breaker(*, program_id: str, programs_root: Path) -> CircuitBreaker:
    """Shared circuit breaker gating repeated WorkIQ subprocess failures.

    Uses the same state file (``.workiq_breaker.json``) as ``commands/enrich.py`` so a
    broken WorkIQ/Agency bridge trips once and all M365 discovery for this program backs
    off together, instead of every SharePoint page in engms_pages.yaml independently
    paying the full subprocess retry cost (up to ~360s per failed call: 3 attempts x
    120s WORKIQ_TIMEOUT) before this pipeline gives up.
    """
    state_path = get_program_output_dir(program_id, programs_root=programs_root) / ".workiq_breaker.json"
    return CircuitBreaker(state_path=state_path)


def _is_sharepoint_page(page: EngMsPage) -> bool:
    parsed = urlparse(page.url)
    host = parsed.netloc.lower()
    return parsed.scheme in {"http", "https"} and "sharepoint.com" in host


def _should_skip_page(
    page: EngMsPage,
    doc_states: dict[str, dict[str, Any]],
    as_of: datetime,
) -> bool:
    """SP2-4: Change-detection — return True if page should be skipped (not re-extracted).

    Rules:
    - lt_deck: skip if time_since(last_extracted) <= cadence_days * 0.9
    - ref_doc / None: skip if time_since(last_extracted) <= 7 days
    Always returns False (re-extract) if page has never been extracted.
    """
    state = doc_states.get(page.id)
    if state is None:
        return False  # never extracted — must run
    last_extracted_str = state.get("last_extracted")
    if not last_extracted_str:
        return False
    try:
        last_extracted = datetime.fromisoformat(last_extracted_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False

    cadence = page.cadence_days if page.cadence_days and page.cadence_days > 0 else 30
    if page.source_subtype == "lt_deck":
        threshold_days = cadence * 0.9
    else:
        threshold_days = 7.0

    elapsed = (as_of - last_extracted).total_seconds() / 86400.0
    return elapsed <= threshold_days


def build_sharepoint_evidence_question(*, page: EngMsPage, since_date: str | None) -> str:
    """SP1: Structured marker extraction query for reference documents."""
    since_hint = f" Focus on content added or changed since {since_date}." if since_date else ""
    return (
        "Use my Microsoft 365 files and SharePoint documents to answer. "
        f"Review the SharePoint document titled '{page.title}' at URL: {page.url}. "
        "Extract the following as structured markers, one per line: "
        "Decision: <what was decided, by whom, when>. "
        "Risk: <risk description, owner, status (open/mitigated/closed)>. "
        "Milestone: <milestone name, target date, current status>. "
        "Metric: <metric name, value, date>. "
        f"Return only markers in this format, no prose.{since_hint} "
        "If nothing relevant, return NO_EVENTS."
    )


def build_lt_deck_evidence_question_nl(*, title: str, site_url: str, year: int | None = None) -> str:
    """SP1: NL topic query for LT decks stored in a SharePoint folder.

    C-WIQ-1: URL-fetch form is unreliable for .pptx; use NL topic query instead.
    C-WIQ-2: LT deck slides have no prefixed labels — WorkIQ must be instructed to add them.
    C-WIQ-3: Always validate returned doc title matches expected deck before using content.
    C-WIQ-4: Folder-based — always target the most recent file in the current year's subfolder.
    """
    year_hint = f" Look specifically in the {year}/ subfolder for the most recent file." if year else ""
    return (
        f"From the most recent '{title}' presentation in SharePoint "
        f"(folder: {site_url}).{year_hint} "
        "Format your response with EXACTLY these labels:\n"
        "Decision: <one decision per line>\n"
        "Risk: <one risk per line>\n"
        "Milestone: <one milestone with date per line>\n"
        "Metric: <one metric per line>\n"
        "You MUST output the exact prefixes Decision:, Risk:, Milestone:, Metric: — "
        "do not use any other format or prose. The deterministic marker parser will fail "
        "if you deviate. Only include items explicitly stated in the deck. "
        "If nothing found, return NO_EVENTS."
    )


def _build_sharepoint_marker_question(*, page: EngMsPage, from_year: int | None) -> str:
    year_hint = (
        f"Focus only on events that occurred on or after January 1, {from_year}. "
        if from_year is not None
        else ""
    )
    tag_hint = f" Tags: {', '.join(page.tags)}." if page.tags else ""
    description_hint = f" Description: {page.description}." if page.description else ""
    return (
        "Use my Microsoft 365 files and SharePoint documents to answer. "
        "Review the specific SharePoint document at this exact URL and return only structured event markers, "
        "one per line, with no prose before or after. "
        "Supported markers are exactly: `Decision:`, `Risk:`, `Milestone:`, and `Metric:`. "
        "If you find nothing, return exactly `NO_EVENTS`. "
        "Use the existing marker field syntax Vertex expects. "
        f"{year_hint}"
        f"Document title: {page.title}. "
        f"Document URL: {page.url}. "
        f"{description_hint}"
        f"{tag_hint}"
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
    page: EngMsPage,
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
        original_filename=f"{page.id}.txt",
        origin_path=page.url,
        programs_root=programs_root,
    )
    source_ref = _sharepoint_ref_for_page(page, retrieved_at=retrieved_at, vault_hash=vault_entry.vault_hash)
    document_key = source_document_key(source_ref)
    candidates: list[CandidateEvent] = []
    for raw_line in response_text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.upper() == "NO_EVENTS":
            continue
        try:
            parsed = parse_structured_event_marker_result(stripped)
        except StructuredEventMarkerError as error:
            raise SharePointPipelineError(str(error)) from error
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
                extraction_confidence=0.75,
                entity_resolution=_entity_resolution(event_type, payload),
                dedupe_key=derive_candidate_dedupe_key(document_key, dedupe_core_hash),
                dedupe_core_hash=dedupe_core_hash,
                source_document_key=document_key,
                corroborating_refs=(),
                batch_id=batch_id,
            )
        )
    return tuple(candidates)


def _sharepoint_ref_for_page(page: EngMsPage, *, retrieved_at: datetime, vault_hash: str) -> SharePointDocRef:
    parsed = urlparse(page.url)
    query = parse_qs(parsed.query)
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) >= 2 and path_parts[0].lower() in {"sites", "teams", "personal"}:
        site = f"{parsed.scheme}://{parsed.netloc}/{'/'.join(path_parts[:2])}"
        path_tail = path_parts[2:]
    else:
        site = f"{parsed.scheme}://{parsed.netloc}"
        path_tail = path_parts
    doc_path = _first_query_value(query, "file")
    if doc_path is None:
        doc_path = "/".join(path_tail) if path_tail else unquote(parsed.path.lstrip("/"))
    version = _first_query_value(query, "version")
    return SharePointDocRef(
        site=site,
        doc_path=unquote(doc_path or page.id),
        version=version,
        modified_at=retrieved_at,
        vault_hash=vault_hash,
    )


def _first_query_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    value = values[0].strip()
    return value or None


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


def _collapse_sharepoint_candidates(candidates: list[CandidateEvent]) -> tuple[CandidateEvent, ...]:
    grouped: dict[tuple[str, str], list[CandidateEvent]] = {}
    for candidate in sorted(candidates, key=_sharepoint_candidate_sort_key):
        grouped.setdefault((candidate.proposed_event_type, candidate.dedupe_core_hash), []).append(candidate)
    collapsed: list[CandidateEvent] = []
    for key in sorted(grouped):
        collapsed.append(_merge_sharepoint_candidate_group(grouped[key]))
    return tuple(collapsed)


def _merge_sharepoint_candidate_group(group: list[CandidateEvent]) -> CandidateEvent:
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


def _sharepoint_candidate_sort_key(candidate: CandidateEvent) -> tuple[datetime, str, str]:
    doc_path = getattr(candidate.source_ref, "doc_path", "") or ""
    return (candidate.proposed_occurred_at, doc_path, candidate.candidate_id)


def _source_ref_identity(source_ref: object) -> str:
    return repr(source_ref)
