from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Callable

from src.core.exceptions import QueryError
from src.core.integration_types import (
    ExtractionResult,
    KustoHydrationOutput,
    KustoResultKind,
    KustoResultSchema,
    KustoResultSet,
)
from src.core.kusto_result_contract import validate_kusto_result_schema
from src.core.kusto_ref_utils import extract_kusto_entity_refs
from src.core.metric_binding_validator import coerce_metric_value
from src.core.models import Confidence
from src.core.models_v2 import Signal
from src.core.signal_ref_utils import merge_entity_refs

#: ADF-W2.3 (Section 8.5.3): injected waiver lookup -- given a query_id,
#: returns the active ``SourceWaiver`` for that query's telemetry source
#: (or None). Dependency-injected so the extractor stays testable without
#: file I/O; the caller (gather pipeline) wires it to
#: ``source_waiver_store.find_waiver_for_query`` with the loaded waivers
#: and slice contracts.
WaiverLookup = Callable[[str], "object | None"]

_MAX_KUSTO_FACT_ROWS = 3
_TIME_COLUMN_NAMES = {
    "timestamp",
    "date",
    "eventtimestamp",
    "createdate",
    "resolveddate",
    "snapshot",
    "maxts",
    "maxdatatimestamp",
}


@dataclass(frozen=True, slots=True)
class _UnboundKustoInterpretation:
    kind: str
    qualification: str
    columns: tuple[str, ...] = ()
    facts: tuple[str, ...] = ()
    omitted_rows: int = 0
    data_as_of: datetime | None = None
    refusal: str | None = None
    trend: str | None = None


def kusto_source_event_time(rows: Iterable[Mapping[str, object]]) -> datetime | None:
    candidates: list[datetime] = []
    for row in rows:
        for key, value in row.items():
            if key.strip().lower() not in {"timestamp", "date", "event_timestamp", "createdate", "resolveddate", "snapshot"}:
                continue
            if value in (None, ""):
                continue
            parsed = _parse_kusto_time_value(value)
            if parsed is not None:
                candidates.append(parsed)
    return max(candidates) if candidates else None


class KustoSignalExtractor:
    def __init__(self, *, waiver_lookup: WaiverLookup | None = None) -> None:
        # ADF-W2.3: optional. Existing construction (channel_wiring registry)
        # passes no arguments, so waiver annotation is opt-in -- a caller that
        # does not inject a lookup gets exactly the pre-existing behavior.
        self._waiver_lookup = waiver_lookup

    @property
    def channel(self) -> str:
        return "kusto"

    def extract(self, resources: KustoHydrationOutput, program_id: str) -> ExtractionResult:
        signals: list[Signal] = []
        for result_set in resources.result_sets:
            waiver = self._lookup_waiver(result_set.query_id) if self._waiver_lookup is not None else None
            if not result_set.rows:
                text = _empty_result_text(result_set)
                if text is None:
                    # ADF-W2.3 (Section 8.5.2) "expected quiet": zero rows and
                    # the query declared expected_cardinality=zero_ok. Not an
                    # actionable finding -- preserves the pre-existing
                    # behavior of emitting nothing for this specific case.
                    continue
                signals.extend(_build_signals(result_set, program_id, text=text, confidence=Confidence.MEDIUM, waiver=waiver))
                continue
            text = format_kusto_result_text(result_set, waiver=waiver)
            signals.extend(_build_signals(result_set, program_id, text=text, confidence=Confidence.HIGH, waiver=waiver))
        return ExtractionResult(channel="kusto", signals=tuple(signals), trajectory_points=(), side_artifacts={}, errors=())

    def _lookup_waiver(self, query_id: str) -> object | None:
        assert self._waiver_lookup is not None
        try:
            return self._waiver_lookup(query_id)
        except Exception:
            # A waiver-store failure (missing file, parse error) must never
            # break signal extraction -- the breach signal is still emitted
            # without the waiver annotation.
            return None


def _empty_result_text(result_set: KustoResultSet) -> str | None:
    """ADF-W2.3 (Section 8.5.2): a zero-row result is one of two explicit
    states here (the other two -- "unavailable" from an execution error, and
    the query/data-gap-vs-invalid-schema split for non-empty results -- are
    handled elsewhere: execution errors become IntegrationErrors before a
    KustoResultSet even exists, and "invalid schema" is a non-empty-rows
    case handled by ``format_kusto_result_text``).

    Returns None for "expected quiet" (nothing to surface); a message for
    "query/data gap" (rows were expected but none came back).
    """
    # KustoResultSet does not carry expected_cardinality directly (it is a
    # per-query config field, not a per-result-set fact); the caller wires
    # is_partial=False and no rows through unconditionally today, so the
    # "expected quiet" vs "data gap" split lives in whether metric_id/
    # result_column semantic config is present -- an unconfigured legacy
    # query with zero rows keeps the pre-existing silent-skip behavior.
    if result_set.metric_id is None and result_set.result_column is None:
        return None
    return (
        f"Kusto query {result_set.query_id}: no rows returned for metric "
        f"'{result_set.metric_id or result_set.query_id}' -- query or data gap."
    )


def format_kusto_result_text(result_set: KustoResultSet, *, waiver: object | None = None) -> str:
    result_kind = (
        result_set.schema.kind.value
        if result_set.schema is not None
        else KustoResultKind.TABLE.value
    )
    if result_set.is_partial:
        return _format_unbound_refusal(result_set, "partial result", result_kind)
    if result_set.row_count is not None and result_set.row_count != len(result_set.rows):
        return _format_unbound_refusal(result_set, "row count mismatch", result_kind)
    if result_set.schema is not None:
        try:
            validate_kusto_result_schema(result_set.rows, result_set.schema)
        except QueryError:
            return _format_unbound_refusal(result_set, "schema drift", result_kind)

    if result_set.metric_id is None:
        interpretation = _interpret_unbound_kusto_result(
            result_set.rows,
            declared_measure=result_set.result_column,
            schema=result_set.schema,
        )
        return _format_unbound_kusto_result(result_set.query_id, len(result_set.rows), interpretation)

    if result_set.observed_value is None:
        # metric_id/result_column configured but the column wasn't found (or
        # wasn't numeric) in the actual result rows -- Section 8.5.2's
        # "invalid schema" state, made explicit rather than silently
        # producing a metric-less signal.
        return (
            f"Kusto query {result_set.query_id}: configured result_column "
            f"'{result_set.result_column}' not found or not numeric in results "
            f"-- metric '{result_set.metric_id}' could not be computed (invalid schema)."
        )

    value_text = _format_value(result_set.observed_value, result_set.unit)
    if result_set.is_breach is None:
        # Measured, but no SLO configured to judge it against.
        return _with_waiver_suffix(f"{result_set.metric_id} = {value_text}.", waiver)

    target_text = _format_value(result_set.slo_target, result_set.unit)
    verdict = "BREACH" if result_set.is_breach else "OK"
    text = f"{result_set.metric_id} = {value_text} against SLO {result_set.comparison}{target_text}: {verdict}."
    return _with_waiver_suffix(text, waiver)


def _format_unbound_refusal(
    result_set: KustoResultSet,
    refusal: str,
    result_kind: str | None = None,
) -> str:
    kind = result_kind or (
        result_set.schema.kind.value
        if result_set.schema is not None
        else KustoResultKind.TABLE.value
    )
    return (
        f"Kusto query {result_set.query_id} "
        f"[kind={_display_result_kind(kind)}, interpretation=ambiguous]: "
        f"admitted_rows={len(result_set.rows)}; refusal={refusal}."
    )


def _interpret_unbound_kusto_result(
    rows: tuple[dict[str, str | int | float | bool | None], ...],
    *,
    declared_measure: str | None = None,
    schema: KustoResultSchema | None = None,
) -> _UnboundKustoInterpretation:
    if schema is not None:
        interpretation = _interpret_typed_kusto_result(
            rows,
            schema=schema,
            declared_measure=declared_measure,
        )
        if interpretation is not None:
            return interpretation
    columns = _stable_kusto_columns(rows)
    if columns is None:
        return _UnboundKustoInterpretation(
            kind="table", qualification="ambiguous", refusal="schema drift",
        )

    serialized_rows = tuple(_serialize_kusto_row(row, columns) for row in rows)
    if any(row is None for row in serialized_rows):
        return _UnboundKustoInterpretation(
            kind="table",
            qualification="ambiguous",
            columns=columns,
            refusal="unsupported row value",
        )
    exact_rows = tuple(row for row in serialized_rows if row is not None)

    time_columns = tuple(column for column in columns if _normalize_kusto_column(column) in _TIME_COLUMN_NAMES)
    numeric_columns = tuple(
        column
        for column in columns
        if all(coerce_metric_value(row.get(column)) is not None for row in rows)
    )
    if len(time_columns) > 1:
        return _ambiguous_table(columns, exact_rows, "multiple time columns")
    if len(time_columns) == 1:
        if declared_measure is not None:
            if declared_measure not in numeric_columns:
                return _ambiguous_table(columns, exact_rows, "declared numeric measure unavailable")
            numeric_column = declared_measure
        else:
            if len(numeric_columns) > 1:
                return _ambiguous_table(columns, exact_rows, "multiple numeric measures")
            if not numeric_columns:
                return _ambiguous_table(columns, exact_rows, "missing numeric measure")
            numeric_column = numeric_columns[0]
        point_columns = tuple(
            column for column in columns if column in {time_columns[0], numeric_column}
        )
        if any(
            column not in point_columns and column not in numeric_columns
            for column in columns
        ):
            return _ambiguous_table(columns, exact_rows, "multiple series dimensions")
        ordered_point_rows = tuple(_serialize_kusto_row(row, point_columns) for row in rows)
        exact_point_rows = tuple(row for row in ordered_point_rows if row is not None)
        if len(exact_point_rows) != len(rows):
            return _ambiguous_table(columns, exact_rows, "unsupported point value")
        parsed_points: list[tuple[datetime, str]] = []
        for row, exact_row in zip(rows, exact_point_rows):
            parsed = _parse_kusto_time_value(row.get(time_columns[0]))
            if parsed is None:
                return _ambiguous_table(columns, exact_rows, "invalid time point")
            parsed_points.append((parsed, exact_row))
        if len({point[0] for point in parsed_points}) != len(parsed_points):
            return _ambiguous_table(columns, exact_rows, "duplicate time points")
        ordered_rows = tuple(row for _, row in sorted(parsed_points, key=lambda point: point[0]))
        return _UnboundKustoInterpretation(
            kind="timeseries",
            qualification="unqualified",
            columns=columns,
            facts=ordered_rows[:_MAX_KUSTO_FACT_ROWS],
            omitted_rows=max(0, len(ordered_rows) - _MAX_KUSTO_FACT_ROWS),
            data_as_of=max(point[0] for point in parsed_points),
        )

    return _UnboundKustoInterpretation(
        kind="table",
        qualification="unqualified",
        columns=columns,
        facts=exact_rows[:_MAX_KUSTO_FACT_ROWS],
        omitted_rows=max(0, len(exact_rows) - _MAX_KUSTO_FACT_ROWS),
    )


def _interpret_typed_kusto_result(
    rows: tuple[dict[str, str | int | float | bool | None], ...],
    *,
    schema: KustoResultSchema,
    declared_measure: str | None,
) -> _UnboundKustoInterpretation | None:
    columns = tuple(column.name for column in schema.columns)
    serialized_rows = tuple(_serialize_kusto_row(row, columns) for row in rows)
    if any(row is None for row in serialized_rows):
        return _ambiguous_table(
            columns,
            (),
            "unsupported row value",
            kind=schema.kind.value,
        )
    exact_rows = tuple(row for row in serialized_rows if row is not None)
    if schema.kind is not KustoResultKind.TIME_SERIES:
        return _UnboundKustoInterpretation(
            kind=schema.kind.value,
            qualification="unqualified",
            columns=columns,
            facts=exact_rows[:_MAX_KUSTO_FACT_ROWS],
            omitted_rows=max(0, len(exact_rows) - _MAX_KUSTO_FACT_ROWS),
        )

    time_column = schema.time_column
    if time_column is None or not schema.value_columns:
        return _ambiguous_table(columns, (), "invalid time-series schema", kind=schema.kind.value)
    value_columns = schema.value_columns
    if declared_measure is not None:
        if declared_measure not in value_columns:
            return _ambiguous_table(
                columns,
                exact_rows,
                "declared numeric measure unavailable",
                kind=schema.kind.value,
            )
        value_columns = (declared_measure,)
    point_columns = (time_column, *value_columns)
    parsed_points: list[tuple[datetime, str]] = []
    for row in rows:
        parsed = _parse_kusto_time_value(row.get(time_column))
        exact_point = _serialize_kusto_row(row, point_columns)
        if parsed is None:
            return _ambiguous_table(columns, exact_rows, "invalid time point", kind=schema.kind.value)
        if exact_point is None:
            return _ambiguous_table(columns, exact_rows, "unsupported point value", kind=schema.kind.value)
        if any(coerce_metric_value(row.get(column)) is None for column in value_columns):
            return _ambiguous_table(columns, exact_rows, "non-numeric series value", kind=schema.kind.value)
        parsed_points.append((parsed, exact_point))
    if len({point[0] for point in parsed_points}) != len(parsed_points):
        return _ambiguous_table(columns, exact_rows, "duplicate time points", kind=schema.kind.value)
    ordered_points = sorted(parsed_points, key=lambda point: point[0])
    trend = _typed_series_trend(rows, time_column, value_columns)
    return _UnboundKustoInterpretation(
        kind=schema.kind.value,
        qualification="unqualified",
        columns=columns,
        facts=tuple(point for _, point in ordered_points[:_MAX_KUSTO_FACT_ROWS]),
        omitted_rows=max(0, len(ordered_points) - _MAX_KUSTO_FACT_ROWS),
        data_as_of=max((point[0] for point in parsed_points), default=None),
        trend=trend,
    )


def _typed_series_trend(
    rows: tuple[dict[str, str | int | float | bool | None], ...],
    time_column: str,
    value_columns: tuple[str, ...],
) -> str:
    if len(rows) < 2:
        return "insufficient_data"
    ordered = sorted(
        rows,
        key=lambda row: _parse_kusto_time_value(row.get(time_column))
        or datetime.min.replace(tzinfo=timezone.utc),
    )
    trends: list[str] = []
    for column in value_columns:
        first = coerce_metric_value(ordered[0].get(column))
        last = coerce_metric_value(ordered[-1].get(column))
        if first is None or last is None:
            trends.append(f"{column}:unknown")
        elif last > first:
            trends.append(f"{column}:increasing")
        elif last < first:
            trends.append(f"{column}:decreasing")
        else:
            trends.append(f"{column}:flat")
    if len(trends) == 1:
        return trends[0].split(":", 1)[1]
    return "[" + ",".join(trends) + "]"


def _ambiguous_table(
    columns: tuple[str, ...],
    exact_rows: tuple[str, ...],
    refusal: str,
    *,
    kind: str = "table",
) -> _UnboundKustoInterpretation:
    return _UnboundKustoInterpretation(
        kind=kind,
        qualification="ambiguous",
        columns=columns,
        facts=exact_rows[:_MAX_KUSTO_FACT_ROWS],
        omitted_rows=max(0, len(exact_rows) - _MAX_KUSTO_FACT_ROWS),
        refusal=refusal,
    )


def _stable_kusto_columns(
    rows: tuple[dict[str, str | int | float | bool | None], ...],
) -> tuple[str, ...] | None:
    if not rows:
        return ()
    columns = tuple(rows[0])
    if not columns or any(not isinstance(column, str) or not column.strip() for column in columns):
        return None
    if any(tuple(row) != columns for row in rows[1:]):
        return None
    return columns


def _serialize_kusto_row(
    row: dict[str, str | int | float | bool | None],
    columns: tuple[str, ...],
) -> str | None:
    pairs: list[str] = []
    for column in columns:
        value = row.get(column)
        serialized = _serialize_kusto_scalar(value)
        if serialized is None and value is not None:
            return None
        pairs.append(f"{json.dumps(column, ensure_ascii=True)}:{serialized if serialized is not None else 'null'}")
    return "{" + ",".join(pairs) + "}"


def _serialize_kusto_scalar(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)):
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    if isinstance(value, datetime):
        normalized = (
            value.astimezone(timezone.utc)
            if value.tzinfo is not None
            else value.replace(tzinfo=timezone.utc)
        )
        return json.dumps(normalized.isoformat(), ensure_ascii=True)
    if isinstance(value, date):
        return json.dumps(value.isoformat(), ensure_ascii=True)
    return None


def _format_unbound_kusto_result(
    query_id: str,
    admitted_rows: int,
    interpretation: _UnboundKustoInterpretation,
) -> str:
    prefix = (
        f"Kusto query {query_id} "
        f"[kind={_display_result_kind(interpretation.kind)}, "
        f"interpretation={interpretation.qualification}]: "
    )
    parts = [f"admitted_rows={admitted_rows}"]
    if interpretation.columns:
        parts.append(f"columns=[{','.join(interpretation.columns)}]")
    if interpretation.facts:
        label = (
            "points"
            if _display_result_kind(interpretation.kind) == "timeseries"
            else "facts"
        )
        parts.append(f"{label}=[{','.join(interpretation.facts)}]")
    if interpretation.omitted_rows:
        parts.append(f"omitted_rows={interpretation.omitted_rows}")
    if interpretation.data_as_of is not None:
        parts.append(f"data_as_of={interpretation.data_as_of.isoformat()}")
    if interpretation.trend is not None:
        parts.append(f"trend={interpretation.trend}")
    if interpretation.refusal is not None:
        parts.append(f"refusal={interpretation.refusal}")
    else:
        parts.append("no qualified trend or SLO verdict")
    return prefix + "; ".join(parts) + "."


def _display_result_kind(kind: str) -> str:
    return "timeseries" if kind == KustoResultKind.TIME_SERIES.value else kind


def _normalize_kusto_column(column: str) -> str:
    return column.strip().lower().replace("_", "")


def _parse_kusto_time_value(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        if not isinstance(value, date):
            return None
        parsed = datetime.combine(value, time.min, tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _format_value(value: float | None, unit: str | None) -> str:
    if value is None:
        return ""
    formatted = f"{value:g}"
    return f"{formatted}{unit}" if unit else formatted


def _waiver_suffix(waiver: object | None) -> str:
    """ADF-W2.3 (Section 8.5.3): an active source waiver annotates the signal
    text so a known-bad metric that is formally waived does not present as an
    un-contextualized alarm. The waiver's reason and expiry are surfaced so
    the operator knows the gate is covered and when coverage ends."""
    if waiver is None:
        return ""
    reason = getattr(waiver, "reason", None)
    expires = getattr(waiver, "expires", None)
    parts = ["(waived"]
    if reason:
        parts.append(f": {reason}")
    if expires is not None:
        parts.append(f", expires {expires}")
    parts.append(")")
    return "".join(parts)


def _with_waiver_suffix(text: str, waiver: object | None) -> str:
    suffix = _waiver_suffix(waiver)
    if not suffix:
        return text
    return f"{text} {suffix}"


def _build_signals(
    result_set: KustoResultSet, program_id: str, *, text: str, confidence: Confidence, waiver: object | None = None
) -> list[Signal]:
    signals: list[Signal] = []
    result_hash = hashlib.sha256(json.dumps(result_set.rows, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]
    source_event_time = kusto_source_event_time(result_set.rows)
    for workstream_id, suffix in _workstream_suffixes(result_set.workstream_ids):
        raw_ref = f"kusto/{result_set.query_id}/{result_hash}/{suffix}"
        metadata: dict[str, object] = {
            "query_id": result_set.query_id,
            "row_count": len(result_set.rows),
            "result_hash": result_hash,
            "metric_id": result_set.metric_id,
            "observed_value": result_set.observed_value,
            "unit": result_set.unit,
            "slo_target": result_set.slo_target,
            "comparison": result_set.comparison,
            "is_breach": result_set.is_breach,
            "result_kind": (
                result_set.schema.kind.value
                if result_set.schema is not None
                else "unknown"
            ),
            "result_columns": (
                tuple(column.name for column in result_set.schema.columns)
                if result_set.schema is not None
                else ()
            ),
            # ADF-W2.3 (Section 8.5.3): waiver annotation for downstream
            # consumers (report rendering, cockpit). False (not None)
            # when no waiver was looked up, so a reader can distinguish
            # "actively waived" from "not waived" without a None check.
            "waiver_active": waiver is not None,
            "waiver_contract_id": getattr(waiver, "contract_id", None) if waiver is not None else None,
        }
        if source_event_time is not None:
            metadata["event_timestamp"] = source_event_time.isoformat()
        entity_refs = merge_entity_refs(
            provider_refs=(f"kusto:{result_set.query_id}",),
            workstream_id=workstream_id,
            additional_refs=extract_kusto_entity_refs(result_set.rows),
        )
        signals.append(
            Signal(
                id=raw_ref,
                timestamp=result_set.observed_at,
                source="kusto",
                program_id=program_id,
                workstream_id=workstream_id,
                entity_refs=entity_refs,
                text=text,
                raw_ref=raw_ref,
                confidence=confidence,
                review_policy=None,
                metadata=metadata,
            )
        )
    return signals


def _workstream_suffixes(workstream_ids: tuple[str, ...]) -> tuple[tuple[str | None, str], ...]:
    if not workstream_ids:
        return ((None, "_unassigned"),)
    return tuple((workstream_id, workstream_id) for workstream_id in dict.fromkeys(workstream_ids))
