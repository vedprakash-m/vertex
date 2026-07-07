from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timezone
import json
from pathlib import Path
from typing import Any, Callable, Literal
from uuid import NAMESPACE_URL, uuid5

from src.commands.gather_pipeline.ado_pipeline_stage import _parse_datetime
from src.commands.gather_pipeline.support import (
    build_captured_window,
    coerce_datetime,
    coerce_datetime_or_none,
    hash_ingestion_query_text,
    parse_date,
)
from src.core.m365_payload_support import optional_string as _optional_string
from src.core.metric_models import MetricObservation, MetricQualityState, MetricSourceBinding
from src.core.models_v2 import KustoQuery, Signal
from src.core.reality_store import RealityStore
from src.core.source_models import IngestionRun, SourceKind

RefreshKpiQueryLoader = Callable[..., tuple[KustoQuery, ...]]
KpiQueryDeduper = Callable[[tuple[KustoQuery, ...]], tuple[KustoQuery, ...]]


def project_refresh_kpi_signals_to_observations(
    program_id: str,
    *,
    programs_root: Path,
    as_of: datetime,
    kpi_signals: tuple[Signal, ...],
    query_states: dict[str, dict[str, Any]],
    include_unvalidated: bool = False,
    store: RealityStore,
    load_refresh_kpi_queries_fn: RefreshKpiQueryLoader,
    dedupe_queries_fn: KpiQueryDeduper,
) -> tuple[MetricObservation, ...]:
    queries = dedupe_queries_fn(
        load_refresh_kpi_queries_fn(
            program_id,
            programs_root=programs_root,
            include_unvalidated=include_unvalidated,
        )
    )
    if not queries:
        return ()
    return project_kpi_signals_to_observations(
        program_id,
        queries=queries,
        as_of=as_of,
        kpi_signals=kpi_signals,
        query_states=query_states,
        store=store,
    )


def project_kpi_signals_to_observations(
    program_id: str,
    *,
    queries: tuple[KustoQuery, ...],
    as_of: datetime,
    kpi_signals: tuple[Signal, ...],
    query_states: dict[str, dict[str, Any]],
    store: RealityStore,
) -> tuple[MetricObservation, ...]:
    store.initialize()
    signals_by_query_id = {
        str(signal.metadata.get("query_id")): signal
        for signal in kpi_signals
        if signal.source == "kusto_kpi" and signal.metadata is not None and signal.metadata.get("query_id")
    }
    observations: list[MetricObservation] = []

    for query in queries:
        query_state = query_states.get(query.id, {})
        signal = signals_by_query_id.get(query.id)
        binding, binding_error = resolve_metric_binding_for_kpi_query(store, query)
        run_id = build_kpi_ingestion_run_id(program_id, query.id, as_of)
        started_at = coerce_datetime(query_state.get("last_attempted_at"), fallback=as_of)
        completed_at = coerce_datetime(query_state.get("last_succeeded_at"), fallback=as_of)
        store.record_ingestion_run(
            IngestionRun(
                id=run_id,
                program_id=program_id,
                source_kind=SourceKind.KPI_QUERY.value,
                source_ref=query.id,
                binding_id=binding.binding_id if binding is not None else None,
                started_at=started_at,
                heartbeat_at=completed_at,
                completed_at=None,
                status="running",
                expected_rows=None,
                metrics_observed=0,
                signals_written=1 if signal is not None else 0,
                query_hash=hash_ingestion_query_text(kpi_query_text(query)),
                captured_window=build_kpi_ingestion_captured_window(signal=signal, query_state=query_state),
                error_message=None,
            )
        )
        observation_error: str | None = None
        observation: MetricObservation | None = None
        if signal is not None and binding is not None:
            observation = build_metric_observation_from_kpi_signal(
                program_id=program_id,
                query=query,
                signal=signal,
                binding=binding,
                observed_at=as_of,
                ingestion_run_id=run_id,
            )
            if observation is None:
                observation_error = (
                    f"KPI query {query.id} returned a non-numeric result; skipping MetricObservation projection."
                )
            else:
                persisted_id = store.write_metric_observation(observation)
                if persisted_id != observation.observation_id:
                    observation = replace(observation, observation_id=persisted_id)
                observations.append(observation)

        error_message = binding_error or observation_error
        run_status = classify_kpi_ingestion_run_status(
            signal=signal,
            observation=observation,
            query_state=query_state,
            error_message=error_message,
        )
        store.record_ingestion_run(
            IngestionRun(
                id=run_id,
                program_id=program_id,
                source_kind=SourceKind.KPI_QUERY.value,
                source_ref=query.id,
                binding_id=binding.binding_id if binding is not None else None,
                started_at=started_at,
                heartbeat_at=completed_at,
                completed_at=completed_at,
                status=run_status,
                expected_rows=None,
                metrics_observed=1 if observation is not None else 0,
                signals_written=1 if signal is not None else 0,
                query_hash=hash_ingestion_query_text(kpi_query_text(query)),
                captured_window=build_kpi_ingestion_captured_window(signal=signal, query_state=query_state),
                error_message=error_message or _optional_string(query_state.get("last_error")),
            )
        )

    return tuple(observations)


def resolve_metric_binding_for_kpi_query(
    store: RealityStore,
    query: KustoQuery,
) -> tuple[MetricSourceBinding | None, str | None]:
    metric_id = _optional_string(query.metric_id)
    if metric_id is None:
        return None, f"KPI query {query.id} has no metric_id; skipping MetricObservation projection."

    matching_bindings = tuple(
        binding
        for binding in store.list_active_metric_source_bindings(metric_id=metric_id)
        if metric_binding_matches_query(binding, query)
    )
    if not matching_bindings:
        return None, f"No active metric binding matches KPI query {query.id} for metric {metric_id}."
    if len(matching_bindings) > 1:
        return None, f"Multiple active metric bindings match KPI query {query.id} for metric {metric_id}."
    return matching_bindings[0], None


def metric_binding_matches_query(binding: MetricSourceBinding, query: KustoQuery) -> bool:
    if binding.source_kind != query.engine:
        return False
    if normalize_binding_text(binding.cluster) != normalize_binding_text(query.cluster):
        return False
    if normalize_binding_text(binding.database) != normalize_binding_text(query.database):
        return False
    if normalize_binding_text(binding.result_column) != normalize_binding_text(query.result_column):
        return False
    binding_kql = _optional_string(binding.kql_template)
    query_text = _optional_string(kpi_query_text(query))
    if binding_kql is not None and query_text is not None and binding_kql != query_text:
        return False
    return True


def normalize_binding_text(value: str | None) -> str:
    return (value or "").strip().lower()


def build_metric_observation_from_kpi_signal(
    *,
    program_id: str,
    query: KustoQuery,
    signal: Signal,
    binding: MetricSourceBinding,
    observed_at: datetime,
    ingestion_run_id: str,
) -> MetricObservation | None:
    if signal.metadata is None:
        return None
    row = load_kpi_result_row(signal.metadata)
    value_num = coerce_metric_value(row.get(query.result_column) if query.result_column is not None else row)
    if value_num is None:
        value_num = coerce_metric_value(signal.metadata.get("result_value"))
    if value_num is None:
        return None

    measurement_period_end = resolve_kpi_measurement_period_end(row, fallback=signal.timestamp)
    measurement_period_start = resolve_kpi_measurement_period_start(row, fallback=measurement_period_end)
    dimensions_json = json.dumps(dict(binding.dimension_defaults), sort_keys=True, separators=(",", ":"))
    sample_count = coerce_sample_count(row)
    return MetricObservation(
        observation_id=str(
            uuid5(NAMESPACE_URL, f"{program_id}|{query.id}|{binding.binding_id}|{measurement_period_end.isoformat()}")
        ),
        program_id=program_id,
        metric_id=binding.metric_id,
        dimensions_json=dimensions_json,
        measurement_period_start=measurement_period_start,
        measurement_period_end=measurement_period_end,
        observed_at=observed_at,
        value_num=value_num,
        value_text=_optional_string(signal.metadata.get("result_value")),
        sample_count=sample_count,
        quality_state=MetricQualityState.OK,
        source_binding_id=binding.binding_id,
        binding_version=binding.binding_version,
        ingestion_run_id=ingestion_run_id,
    )


def load_kpi_result_row(metadata: dict[str, Any]) -> dict[str, Any]:
    payload = metadata.get("result_json")
    if not isinstance(payload, str) or not payload.strip():
        return {}
    try:
        loaded = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


def coerce_metric_value(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, dict):
        return None
    text = _optional_string(value)
    if text is None:
        return None
    normalized = text.replace(",", "").rstrip("%")
    try:
        return float(normalized)
    except ValueError:
        return None


def resolve_kpi_measurement_period_start(row: dict[str, Any], *, fallback: datetime) -> datetime:
    for key, value in row.items():
        normalized = key.strip().lower()
        if normalized not in {
            "measurement_period_start",
            "period_start",
            "window_start",
            "start_time",
            "start",
            "min_ts",
            "min_timestamp",
            "first_timestamp",
        }:
            continue
        parsed = _parse_datetime(value)
        if parsed is not None:
            return parsed
        parsed_date = parse_date(value)
        if parsed_date is not None:
            return datetime.combine(parsed_date, time.min, tzinfo=timezone.utc)
    return fallback


def resolve_kpi_measurement_period_end(row: dict[str, Any], *, fallback: datetime) -> datetime:
    for key, value in row.items():
        normalized = key.strip().lower()
        if normalized not in {
            "measurement_period_end",
            "period_end",
            "window_end",
            "end_time",
            "end",
            "max_ts",
            "max_timestamp",
            "timestamp",
            "date",
            "event_timestamp",
            "snapshot",
        }:
            continue
        parsed = _parse_datetime(value)
        if parsed is not None:
            return parsed
        parsed_date = parse_date(value)
        if parsed_date is not None:
            return datetime.combine(parsed_date, time.min, tzinfo=timezone.utc)
    return fallback


def coerce_sample_count(row: dict[str, Any]) -> int | None:
    for key, value in row.items():
        if key.strip().lower() not in {"sample_count", "samplecount", "count"}:
            continue
        if isinstance(value, int) and value >= 0:
            return value
        text = _optional_string(value)
        if text is not None and text.isdigit():
            return int(text)
    return None


def build_kpi_ingestion_run_id(program_id: str, query_id: str, as_of: datetime) -> str:
    return str(uuid5(NAMESPACE_URL, f"{program_id}|kpi_query|{query_id}|{as_of.isoformat()}"))


def kpi_query_text(query: KustoQuery) -> str | None:
    if query.engine == "wiql":
        return query.wiql
    if query.engine == "ado_pr":
        workstream_scope = ",".join(query.workstream_ids) if query.workstream_ids else "all_workstreams"
        return f"ado_pr:{query.id}:{workstream_scope}"
    return query.kql


def build_kpi_ingestion_captured_window(
    *,
    signal: Signal | None,
    query_state: dict[str, Any],
) -> str | None:
    if signal is not None and signal.metadata is not None:
        row = load_kpi_result_row(signal.metadata)
        end = resolve_kpi_measurement_period_end(row, fallback=signal.timestamp)
        start = resolve_kpi_measurement_period_start(row, fallback=end)
        return build_captured_window(start, end)
    max_data_timestamp = coerce_datetime_or_none(query_state.get("max_data_timestamp"))
    return build_captured_window(max_data_timestamp, max_data_timestamp)


def classify_kpi_ingestion_run_status(
    *,
    signal: Signal | None,
    observation: MetricObservation | None,
    query_state: dict[str, Any],
    error_message: str | None,
) -> Literal["running", "success", "partial", "failed"]:
    if error_message is not None:
        return "failed" if query_state.get("last_cycle_succeeded") is False else "partial"
    if signal is None or observation is None:
        return "partial"
    return "success"
