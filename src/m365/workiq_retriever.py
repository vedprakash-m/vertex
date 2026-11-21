"""Zone-C WorkIQ per-thread retrieval; orchestration and persistence stay outside."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import ceil
from pathlib import Path
from time import monotonic
from typing import Any

from src.core.models import Enrichment
from src.core.workiq_freshness import (
    load_workiq_freshness_cache,
    workiq_thread_freshness_hash,
    write_workiq_freshness_cache,
)
from src.m365.workiq_ask_support import (
    DiscoveryRequest,
    _is_native_workiq_identity,
    _normalize_discovery_identity,
    _parse_iso_datetime,
    build_structured_discovery_question,
    validate_structured_discovery_payload_with_diagnostics,
)

_CALL_NOT_ATTEMPTED = object()


@dataclass(frozen=True, slots=True)
class WorkIQRetrievalResult:
    enrichments: tuple[Enrichment, ...]
    enumeration_count: int
    calls_made: int
    skipped_unchanged: int
    rejected_identity_count: int = 0
    rejected_source_time_count: int = 0
    response_usable: bool = True
    rejected_invalid_record_count: int = 0
    budget_exhausted: bool = False


@dataclass(slots=True)
class WorkIQRunBudget:
    """One mutable call and wall-clock ceiling shared across WorkIQ threads."""

    max_calls: int
    max_wall_clock_seconds: int
    calls_made: int = 0
    started_at: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        if self.max_calls < 1 or self.max_wall_clock_seconds < 1:
            raise ValueError("Invalid WorkIQ per-run retrieval budget")
        self.started_at = monotonic()

    def remaining_seconds(self) -> float:
        return self.max_wall_clock_seconds - (monotonic() - self.started_at)

    def reserve_call(self) -> float | None:
        if self.calls_made >= self.max_calls:
            return None
        remaining = (
            float(self.max_wall_clock_seconds)
            if self.calls_made == 0
            else self.remaining_seconds()
        )
        if remaining <= 0:
            return None
        self.calls_made += 1
        return remaining


def retrieve_workiq_threads(
    *,
    bridge: Any,
    request: DiscoveryRequest,
    top_k: int,
    max_calls: int,
    max_wall_clock_seconds: int,
    cache_path: Path | None = None,
    one_hop: bool = False,
    write_cache: bool = True,
    budget: WorkIQRunBudget | None = None,
) -> WorkIQRetrievalResult:
    """Enumerate then extract one bounded, source-keyed record per thread."""

    if not 1 <= top_k <= 10 or max_calls < 1 or max_wall_clock_seconds < 1:
        raise ValueError("Invalid WorkIQ per-thread retrieval budget")
    run_budget = budget or WorkIQRunBudget(max_calls, max_wall_clock_seconds)
    calls = 0

    def _ask(question: str) -> Any:
        nonlocal calls
        remaining = run_budget.reserve_call()
        if remaining is None:
            return _CALL_NOT_ATTEMPTED
        calls += 1
        timeout_seconds = max(1, int(ceil(remaining)))
        try:
            return bridge.ask_workiq(
                question, use_cache=False, timeout_seconds=timeout_seconds,
            )
        except TypeError:
            return bridge.ask_workiq(question, use_cache=False)

    payload = _ask(build_structured_discovery_question(request))
    if payload is _CALL_NOT_ATTEMPTED:
        return WorkIQRetrievalResult(
            enrichments=(),
            enumeration_count=0,
            calls_made=calls,
            skipped_unchanged=0,
            budget_exhausted=True,
        )
    validated = validate_structured_discovery_payload_with_diagnostics(
        payload,
        window_start=request.window_start,
        window_end=request.window_end,
        limit=request.limit,
    )
    admitted_records = tuple(validated.payload.get("emails") or [])
    records = admitted_records
    cache = load_workiq_freshness_cache(cache_path) if cache_path is not None else {}
    enrichments: list[Enrichment] = []
    skipped = 0
    rejected = validated.rejected_identity_count
    budget_exhausted = False
    seen_source_ids: set[str] = set()
    for record in records[:top_k]:
        if run_budget.calls_made >= run_budget.max_calls or run_budget.remaining_seconds() <= 0:
            budget_exhausted = True
            break
        source_id = _canonical_source_id(record)
        if source_id is None:
            rejected += 1
            continue
        if source_id in seen_source_ids:
            continue
        seen_source_ids.add(source_id)
        newest_identity = _freshness_identity(record)
        message_count = _bounded_message_count(record.get("messageCount"))
        freshness = workiq_thread_freshness_hash(
            conversation_id=source_id,
            message_count=message_count,
            newest_message_identity=newest_identity,
        )
        cached = cache.get(source_id) or {}
        if cached.get("freshness_hash") == freshness and cached.get("status") == "success":
            skipped += 1
            continue
        timestamp = _parse_iso_datetime(record.get("receivedDateTime"))
        if timestamp is None:
            raise ValueError("Invalid validated WorkIQ source time")
        question = _thread_extraction_question(record, request=request, one_hop=one_hop)
        raw = _ask(question)
        if raw is _CALL_NOT_ATTEMPTED:
            budget_exhausted = True
            break
        body = _response_text(raw)
        if not body:
            continue
        enrichments.append(
            Enrichment(
                source="mail",
                source_id=source_id,
                author=_sender_text(record.get("from")),
                timestamp=timestamp,
                excerpt=str(record.get("subject") or "")[:120],
                permalink=str(record.get("webUrl") or "") or None,
                body_text=body,
            )
        )
        cache[source_id] = {
            "freshness_hash": freshness,
            # Command-stage orchestration promotes this to ``success`` only
            # after parsing, privacy checks, and persistence all succeed.
            "status": "retrieved",
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "algorithm": "workiq-thread-v1",
        }
    if cache_path is not None and write_cache:
        write_workiq_freshness_cache(cache_path, cache)
    return WorkIQRetrievalResult(
        tuple(enrichments), len(admitted_records), calls, skipped, rejected,
        validated.rejected_source_time_count,
        response_usable=validated.response_usable,
        rejected_invalid_record_count=validated.rejected_invalid_record_count,
        budget_exhausted=budget_exhausted,
    )


def _thread_extraction_question(record: dict[str, Any], *, request: DiscoveryRequest, one_hop: bool) -> str:
    identity = json.dumps(_canonical_source_id(record), ensure_ascii=False)
    subject = json.dumps(str(record.get("subject") or "")[:300], ensure_ascii=False)
    method = "single-pass" if one_hop else "source-grounded"
    return (
        f"For mailbox thread {identity} titled {subject}, perform {method} extraction for "
        f"workstream {json.dumps(request.lane_name)}. Return JSON only with keys: "
        '{"risk_level":"blocked|high|medium|low|done|unknown","etas":['
        '{"label":"","eta_date":"YYYY-MM-DD","owner":null,"status":"open|closed|missed","ado_id":null}],'
        '"blocking_items":[],"owners":[],"raw_excerpts":["verbatim source quote"],'
        '"narrative_summary":"","confidence":0.0}. '
        "Use only this thread; quotes must be verbatim. Use empty arrays when unsupported."
    )


def _canonical_source_id(record: dict[str, Any]) -> str | None:
    for key in ("conversationId", "threadId", "id"):
        value = _normalize_discovery_identity(record.get(key))
        if value is not None and _is_native_workiq_identity(value):
            return value
    return None


def _freshness_identity(record: dict[str, Any]) -> str:
    provider_revision = {
        key: record.get(key)
        for key in (
        "changeKey",
        "lastModifiedDateTime",
        "modifiedDateTime",
        "changeType",
        "deleted",
        )
        if record.get(key) is not None
    }
    identity = str(record.get("id") or record.get("receivedDateTime") or "").strip()
    if not provider_revision:
        return identity
    return f"{identity}:{json.dumps(provider_revision, ensure_ascii=False, sort_keys=True)}"


def _response_text(payload: Any) -> str | None:
    if isinstance(payload, str):
        return payload.strip() or None
    if not isinstance(payload, dict):
        return None
    for key in ("response", "answer", "text", "content"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return json.dumps(payload, ensure_ascii=False) if payload else None


def _sender_text(value: Any) -> str:
    if isinstance(value, str):
        return value[:120]
    if isinstance(value, dict):
        return str(value.get("emailAddress", {}).get("address") or value.get("address") or value.get("name") or "workiq")[:120]
    return "workiq"


def _bounded_message_count(value: Any) -> int:
    return value if isinstance(value, int) and 0 <= value <= 1_000_000 else 1
