from __future__ import annotations

from datetime import datetime, time, timezone
from time import perf_counter
from typing import Any, Callable
from uuid import NAMESPACE_URL, uuid5

from src.commands.gather_pipeline.ado_pipeline_stage import _parse_datetime
from src.commands.gather_pipeline.support import (
    coerce_datetime_or_none,
    confidence_from_string,
    kusto_event_timestamp,
    kusto_kpi_value,
    parse_date,
    roll_query_value_history,
)
from src.commands.gather_workiq_helpers import _truncate_signal_text
from src.core.kusto_ref_utils import extract_kusto_entity_refs as _extract_kusto_entity_refs
from src.core.models_v2 import KustoQuery, Signal
from src.core.signal_ref_utils import merge_entity_refs

KustoQueryExecutor = Callable[[KustoQuery], list[dict[str, Any]]]

_DEFAULT_KUSTO_EXPECTED_MAX_AGE_HOURS = 24


def build_kusto_signals(
    *,
    queries: tuple[KustoQuery, ...],
    program_id: str,
    as_of: datetime,
    executor: KustoQueryExecutor,
    query_state_sink: dict[str, dict[str, Any]] | None = None,
    previous_query_states: dict[str, dict[str, Any]] | None = None,
) -> tuple[Signal, ...]:
    signals: list[Signal] = []
    for query in queries:
        started_at = perf_counter()
        rows = executor(query)
        record_kusto_query_state(
            query_state_sink,
            query,
            rows=rows,
            as_of=as_of,
            duration_ms=int(round((perf_counter() - started_at) * 1000)),
            previous_state=(previous_query_states or {}).get(query.id),
        )
        signal = build_kusto_signal(query=query, rows=rows, program_id=program_id, as_of=as_of)
        if signal is not None:
            signals.append(signal)
    return tuple(signals)


def record_kusto_query_state(
    query_state_sink: dict[str, dict[str, Any]] | None,
    query: KustoQuery,
    *,
    rows: list[dict[str, Any]],
    as_of: datetime,
    duration_ms: int,
    error: str | None = None,
    previous_state: dict[str, Any] | None = None,
) -> None:
    if query_state_sink is None:
        return
    timestamp = as_of.astimezone(timezone.utc)
    max_data_timestamp = extract_kusto_max_data_timestamp(rows)
    numeric_value = extract_kusto_query_numeric_value(query, rows)
    value_last_4 = roll_query_value_history(previous_state, numeric_value)
    state: dict[str, Any] = {
        "last_attempted_at": timestamp,
        "last_succeeded_at": timestamp if error is None else coerce_datetime_or_none((previous_state or {}).get("last_succeeded_at")),
        "row_count": len(rows),
        "duration_ms": duration_ms,
        "last_cycle_succeeded": error is None,
        "zero_rows_ok": query.expected_cardinality == "zero_ok",
        "last_error": error,
        "expected_max_age_hours": _DEFAULT_KUSTO_EXPECTED_MAX_AGE_HOURS,
        # WS-1 PB-4: a query-state sink that records an error MUST also
        # set is_degraded=True so downstream readers (report, doctor,
        # scorecard) can render the data as degraded rather than as
        # `row_count=0` (which is misleadingly normal-looking).
        "is_degraded": error is not None,
    }
    if max_data_timestamp is not None:
        data_age_hours = round((timestamp - max_data_timestamp).total_seconds() / 3600.0, 2)
        state["max_data_timestamp"] = max_data_timestamp
        state["data_age_hours"] = data_age_hours
        state["data_freshness_ok"] = data_age_hours <= _DEFAULT_KUSTO_EXPECTED_MAX_AGE_HOURS
    if value_last_4:
        state["value_last_4"] = value_last_4
        state["value_frozen_warning"] = bool(
            numeric_value is not None
            and len(value_last_4) == 4
            and len({float(value) for value in value_last_4}) == 1
        )
    query_state_sink[query.id] = state


def extract_kusto_max_data_timestamp(rows: list[dict[str, Any]]) -> datetime | None:
    candidates: list[datetime] = []
    for row in rows:
        for key, value in row.items():
            normalized = key.strip().lower().replace("_", "")
            if normalized not in {"timestamp", "date", "eventtimestamp", "createdate", "resolveddate", "snapshot", "maxts", "maxdatatimestamp"}:
                continue
            parsed = _parse_datetime(value)
            if parsed is not None:
                candidates.append(parsed)
                continue
            parsed_date = parse_date(value)
            if parsed_date is not None:
                candidates.append(datetime.combine(parsed_date, time.min, tzinfo=timezone.utc))
    if not candidates:
        return None
    return max(candidates)


def extract_kusto_query_numeric_value(query: KustoQuery, rows: list[dict[str, Any]]) -> float | None:
    if not rows:
        return None
    value = kusto_kpi_value(query, rows[0])
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_kusto_signal(
    *,
    query: KustoQuery,
    rows: list[dict[str, Any]],
    program_id: str,
    as_of: datetime,
) -> Signal | None:
    if not rows:
        return None

    timestamp = kusto_event_timestamp(rows, as_of=as_of)
    workstream_id = query.workstream_ids[0] if len(query.workstream_ids) == 1 else None
    entity_refs = merge_entity_refs(
        provider_refs=_extract_kusto_entity_refs(rows),
        workstream_id=workstream_id,
    )
    event_timestamp = timestamp.isoformat()
    raw_ref = f"kusto:{query.id}:{event_timestamp}"
    return Signal(
        id=str(uuid5(NAMESPACE_URL, f"{program_id}|{raw_ref}|{query.id}")),
        timestamp=timestamp,
        source="kusto",
        program_id=program_id,
        workstream_id=workstream_id,
        entity_refs=entity_refs,
        text=summarize_kusto_rows(query, rows),
        raw_ref=raw_ref,
        confidence=confidence_from_string(query.confidence),
        metadata={
            "query_id": query.id,
            "cluster": query.cluster,
            "database": query.database,
            "validated": query.validated,
            "event_timestamp": event_timestamp,
            "row_count": len(rows),
        },
    )


def summarize_kusto_rows(query: KustoQuery, rows: list[dict[str, Any]]) -> str:
    first_row = rows[0]
    preview_parts: list[str] = []
    for key, value in first_row.items():
        if value in (None, ""):
            continue
        preview_parts.append(f"{key}={value}")
        if len(preview_parts) == 3:
            break
    preview = ", ".join(preview_parts) if preview_parts else "rows available"
    return _truncate_signal_text(f"Kusto {query.id} returned {len(rows)} row(s): {preview}")
