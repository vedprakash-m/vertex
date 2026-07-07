from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from src.ai.safety.pii_scrubber import filter_text
from src.core.m365_payload_support import optional_string as _optional_string
from src.core.signal_fragment_utils import fragment_resource_id, split_signal_fragments
from src.core.signal_ref_utils import extract_work_item_refs as _extract_work_item_refs
from src.core.models_v2 import WORKIQ_DISCOVERY_MODE_STRUCTURED, Program, WorkIQRetrievalConfig, Workstream
from src.m365.workiq_ask_support import DiscoveryRequest, build_structured_discovery_question


_WORKIQ_SIGNAL_TEXT_LIMIT = 4000


@dataclass(frozen=True, slots=True)
class WorkIQQueryPlan:
    query_name: str
    question: str | None = None
    workstream_id: str | None = None
    exclude_keywords: tuple[str, ...] = ()
    mcp_tool: str | None = None
    tool_args: dict[str, Any] | None = None
    allowed_thread_ids: tuple[str, ...] = ()
    include_transcripts: bool = False
    discovery_terms: tuple[str, ...] = ()
    structured_window_start: date | None = None
    structured_window_end: date | None = None
    structured_result_limit: int | None = None
    bypass_ask_cache: bool = False


def apply_structured_workiq_discovery(
    *,
    plans: tuple[WorkIQQueryPlan, ...],
    program: Program,
    workstreams: tuple[Workstream, ...],
    as_of: datetime | None,
) -> tuple[WorkIQQueryPlan, ...]:
    """Render and expand FQ-01 broad plans; targeted plans remain untouched."""

    program_config = program.m365.retrieval if program.m365 and program.m365.retrieval else WorkIQRetrievalConfig()
    workstreams_by_id = {workstream.id: workstream for workstream in workstreams}
    end_date = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    expanded: list[WorkIQQueryPlan] = []
    emitted_structured_keys: set[tuple[str, str]] = set()
    for plan in plans:
        workstream = workstreams_by_id.get(plan.workstream_id or "")
        signal_sources = workstream.signal_sources if workstream is not None else None
        mode = signal_sources.workiq_discovery_mode if signal_sources and signal_sources.workiq_discovery_mode else program_config.discovery_mode
        if plan.mcp_tool is not None or workstream is None or mode != WORKIQ_DISCOVERY_MODE_STRUCTURED:
            expanded.append(plan)
            continue
        union_runs = (
            signal_sources.workiq_discovery_union_runs
            if signal_sources and signal_sources.workiq_discovery_union_runs is not None
            else program_config.discovery_union_runs
        )
        lookback_days = (
            signal_sources.workiq_discovery_lookback_days
            if signal_sources and signal_sources.workiq_discovery_lookback_days is not None
            else program_config.discovery_lookback_days
        )
        start_date = end_date - timedelta(days=lookback_days)
        question = build_structured_discovery_question(
            DiscoveryRequest(
                lane_name=workstream.name,
                terms=plan.discovery_terms or (workstream.name, *workstream.aliases),
                window_start=start_date,
                window_end=end_date,
                limit=8,
            )
        )
        structured_key = (workstream.id, question)
        if structured_key in emitted_structured_keys:
            continue
        emitted_structured_keys.add(structured_key)
        structured_plan = replace(
            plan,
            question=question,
            structured_window_start=start_date,
            structured_window_end=end_date,
            structured_result_limit=8,
        )
        expanded.extend(
            replace(structured_plan, bypass_ask_cache=repetition > 0)
            for repetition in range(union_runs)
        )
    return tuple(expanded)


def workiq_source_type(query_name: str) -> str:
    normalized = query_name.strip().lower()
    if "team" in normalized:
        return "teams"
    if "transcript" in normalized or "meeting" in normalized:
        return "transcript"
    return "email"


def workiq_timestamp(record: dict[str, Any], *, as_of: datetime) -> datetime:
    return _parse_datetime(
        record.get("receivedDateTime")
        or record.get("sentDateTime")
        or record.get("createdDateTime")
        or record.get("timestamp")
    ) or as_of


def workiq_message_id(
    record: dict[str, Any],
    *,
    program_id: str,
    query_name: str,
    question: str,
    index: int,
    fallback_text: str,
    timestamp: datetime,
) -> str:
    existing = _optional_string(record.get("id") or record.get("messageId") or record.get("emailId") or record.get("conversationId") or record.get("meetingId"))
    if existing is not None:
        return existing
    return str(uuid5(NAMESPACE_URL, f"{program_id}|{query_name}|{question}|{index}|{fallback_text}|{timestamp.isoformat()}"))


def build_workiq_signal_text(*, source_type: str, subject: str | None, preview: str | None) -> str:
    label = f"WorkIQ {source_type.title()}"
    summary = subject or preview or "M365 activity requires review"
    if preview and subject and preview != subject:
        summary = f"{subject} - {preview}"
    return _truncate_signal_text(filter_text(f"{label}: {summary}"), limit=_WORKIQ_SIGNAL_TEXT_LIMIT)


def build_workiq_fragment_signal_text(*, source_type: str, fragment_text: str) -> str:
    return _truncate_signal_text(filter_text(f"WorkIQ {source_type.title()}: {fragment_text}"), limit=_WORKIQ_SIGNAL_TEXT_LIMIT)


def workiq_signal_fragments(*, subject: str | None, preview: str | None) -> tuple[str, ...]:
    preview_text = _optional_string(preview)
    if preview_text is None:
        fallback = _optional_string(subject)
        return (fallback,) if fallback is not None else ("M365 activity requires review",)
    fragments = split_signal_fragments(preview_text)
    if fragments:
        return fragments
    fallback = " ".join(preview_text.split())
    return (fallback,) if fallback else ("M365 activity requires review",)


def workiq_fragment_message_id(*, message_id: str, segment_index: int, segment_count: int) -> str:
    return fragment_resource_id(resource_id=message_id, segment_index=segment_index, segment_count=segment_count)


def extract_work_item_refs(value: str) -> tuple[str, ...]:
    return _extract_work_item_refs(value)


def _truncate_signal_text(value: str, *, limit: int = 240) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
