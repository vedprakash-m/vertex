"""Zone-C WorkIQ per-thread retrieval; orchestration and persistence stay outside."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
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
    build_structured_discovery_question,
    validate_structured_discovery_payload,
)


@dataclass(frozen=True, slots=True)
class WorkIQRetrievalResult:
    enrichments: tuple[Enrichment, ...]
    enumeration_count: int
    calls_made: int
    skipped_unchanged: int


def retrieve_workiq_threads(
    *,
    bridge: Any,
    request: DiscoveryRequest,
    top_k: int,
    max_calls: int,
    max_wall_clock_seconds: int,
    cache_path: Path | None = None,
    one_hop: bool = False,
) -> WorkIQRetrievalResult:
    """Enumerate then extract one bounded, source-keyed record per thread."""

    if not 1 <= top_k <= 10 or max_calls < 1 or max_wall_clock_seconds < 1:
        raise ValueError("Invalid WorkIQ per-thread retrieval budget")
    started = monotonic()
    calls = 1
    payload = bridge.ask_workiq(build_structured_discovery_question(request), use_cache=False)
    validated = validate_structured_discovery_payload(
        payload,
        window_start=request.window_start,
        window_end=request.window_end,
        limit=request.limit,
    )
    records = tuple(validated.get("emails") or [])
    cache = load_workiq_freshness_cache(cache_path) if cache_path is not None else {}
    enrichments: list[Enrichment] = []
    skipped = 0
    for record in records[:top_k]:
        if calls >= max_calls or monotonic() - started >= max_wall_clock_seconds:
            break
        source_id = _canonical_source_id(record)
        if source_id is None:
            continue
        newest_identity = str(record.get("id") or record.get("receivedDateTime") or "").strip()
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
        question = _thread_extraction_question(record, request=request, one_hop=one_hop)
        raw = bridge.ask_workiq(question, use_cache=False)
        calls += 1
        body = _response_text(raw)
        if not body:
            continue
        timestamp = _parse_timestamp(record.get("receivedDateTime"))
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
    if cache_path is not None:
        write_workiq_freshness_cache(cache_path, cache)
    return WorkIQRetrievalResult(tuple(enrichments), len(records), calls, skipped)


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
        value = str(record.get(key) or "").strip()
        if value:
            return value
    return None


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


def _parse_timestamp(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


def _bounded_message_count(value: Any) -> int:
    return value if isinstance(value, int) and 0 <= value <= 1_000_000 else 1
