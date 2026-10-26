from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import runpy
import sqlite3
from typing import Any

import pytest

from src.commands import gather
from src.commands.gather_pipeline import ado_kpi_stage, kusto_query_helpers
from src.core.integration_types import (
    ChannelRegistration,
    ExtractionResult,
    HydrationResult,
    KustoHydrationOutput,
    RegistrationStatus,
)
from src.core.kusto_client import KustoColumn
from src.core.kusto_hydration import KustoHydrationConfig, KustoHydrationProvider
from src.core.kusto_signal_extractor import KustoSignalExtractor
from src.core.metric_binding_validator import kusto_scalar_cardinality_error, validate_metric_source_binding
from src.core.metric_models import MetricSourceBinding
from src.core.metric_registry import _parse_metric_definition
from src.core.models_v2 import KustoQuery, Program
from src.core.reality_store import RealityStore


NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)
PREVIOUS = {"last_succeeded_at": "2026-10-24T00:00:00+00:00", "value_last_4": [7.0, 8.0]}


def _query() -> KustoQuery:
    authored = runpy.run_path(str(Path(__file__).with_name("test_config_metric_bindings.py")))
    return KustoQuery(
        **authored["query"](), section="Synthetic", render_as="metric_highlight",
        confidence="high", validated=True,
    )


def _hydrate(
    rows: list[dict[str, Any]], root: Path, cardinality: str = "zero_ok", *, diagnostic: bool = False,
) -> tuple[HydrationResult[KustoHydrationOutput], ExtractionResult]:
    authored = runpy.run_path(str(Path(__file__).with_name("test_config_metric_bindings.py")))
    definition = _parse_metric_definition(authored["metric"](), Path("synthetic") / "catalog.yaml")
    entry = authored["query"]()
    binding = validate_metric_source_binding(
        MetricSourceBinding(
            binding_id=entry["id"], metric_id=definition.id, program_id="demo",
            source_kind="kusto", cluster=entry["cluster"], database=entry["database"],
            kql_template=entry["kql"], result_column=entry["result_column"],
        ),
        metric_definitions={definition.id: definition},
        probe=lambda _: (rows, (KustoColumn("Count", "long"),)),
        validated_at=NOW,
    )
    query = KustoQuery(
        **entry, section="Synthetic", render_as="metric_highlight", confidence="high",
        validated=binding.validated, unit=definition.unit, expected_cardinality=cardinality,
    )
    if diagnostic:
        query = replace(query, metric_id=None, result_column=None)
    registration = ChannelRegistration(
        channel="kusto", program_id="demo", provider_instance_id="default",
        ref_id=query.id, ref_kind="kusto_query", status=RegistrationStatus.ACTIVE,
        first_discovered_at=NOW, last_seen_at=NOW,
    )
    provider = KustoHydrationProvider(
        executor=lambda _: rows, query_loader=lambda *args, **kwargs: (query,),
    )
    result = provider.hydrate(
        (registration,), NOW, "demo", KustoHydrationConfig(programs_root=root),
    )
    return result, KustoSignalExtractor().extract(result.resources, "demo")


def test_existing_known_definition_rejects_both_row_orders(tmp_path: Path) -> None:
    first, signals = _hydrate([{"Count": 1}, {"Count": 999}], tmp_path)
    reverse, reversed_signals = _hydrate([{"Count": 999}, {"Count": 1}], tmp_path)
    assert first.errors == reverse.errors
    assert len(first.errors) == 1
    assert first.errors[0].message == (
        "Kusto query count-query returned 2 row(s) for metric demo.count; "
        "scalar interpretation is ambiguous without a deterministic selection contract."
    )
    assert first.errors[0].retryable is False
    assert first.errors[0].stage == "hydration"
    assert first.resources.result_sets == reverse.resources.result_sets == ()
    assert first.failed_ref_ids == reverse.failed_ref_ids == (("count-query", "kusto_query"),)
    assert first.hydrated_ref_ids == reverse.hydrated_ref_ids == ()
    assert signals.signals == reversed_signals.signals == ()


@pytest.mark.parametrize("rows", [[{"Count": 0}], [{"Count": 1, "Other": 999}]])
@pytest.mark.parametrize("cardinality", ["zero_ok", "scalar_required"])
def test_single_row_keeps_zero_and_named_numeric_column(
    rows: list[dict[str, Any]], cardinality: str, tmp_path: Path,
) -> None:
    result, signals = _hydrate(rows, tmp_path, cardinality)
    assert result.errors == ()
    assert result.resources.result_sets[0].observed_value == rows[0]["Count"]
    assert signals.signals[0].text == f"demo.count = {rows[0]['Count']}count."


def test_explicit_scalar_rejects_multiple_rows(tmp_path: Path) -> None:
    result, signals = _hydrate([{"Count": 1}, {"Count": 999}], tmp_path, "scalar_required")
    assert result.resources.result_sets == ()
    assert len(result.errors) == 1
    assert signals.signals == ()


def test_uncontracted_known_metric_must_not_publish_arbitrary_first_row(tmp_path: Path) -> None:
    result, signals = _hydrate([{"Count": 1}, {"Count": 999}], tmp_path)
    assert result.errors, "Known-definition result has no declared scalar/selection contract but is accepted."
    assert signals.signals == ()


def test_diagnostic_only_multirow_query_keeps_row_count_signal(tmp_path: Path) -> None:
    result, signals = _hydrate([{"Count": 1}, {"Count": 999}], tmp_path, diagnostic=True)
    assert result.errors == ()
    assert result.resources.result_sets[0].observed_value is None
    assert signals.signals[0].text == "Kusto query count-query: 2 row(s) observed."


def test_known_metric_zero_rows_keeps_explicit_data_gap(tmp_path: Path) -> None:
    result, signals = _hydrate([], tmp_path)
    assert result.errors == ()
    assert result.resources.result_sets[0].observed_value is None
    assert "query or data gap" in signals.signals[0].text


@pytest.mark.parametrize("values", [(1, 999), (999, 1)])
def test_bound_multirow_is_withheld_by_direct_builders_numeric_and_state(values: tuple[int, int]) -> None:
    query = _query()
    rows = [{"Count": value} for value in values]
    state: dict[str, dict[str, Any]] = {}
    assert kusto_query_helpers.build_kusto_signal(
        query=query, rows=rows, program_id="demo", as_of=NOW,
    ) is None
    assert kusto_query_helpers.extract_kusto_query_numeric_value(query, rows) is None
    assert kusto_query_helpers.build_kusto_signals(
        queries=(query,), program_id="demo", as_of=NOW, executor=lambda _: rows,
        query_state_sink=state, previous_query_states={query.id: PREVIOUS},
    ) == ()
    assert state[query.id]["last_cycle_succeeded"] is False
    assert state[query.id]["is_degraded"] is True
    assert "scalar interpretation is ambiguous" in state[query.id]["last_error"]
    assert state[query.id]["last_succeeded_at"] == datetime(2026, 10, 24, tzinfo=timezone.utc)
    assert state[query.id]["value_last_4"] == [7.0, 8.0]
    assert gather._build_kusto_kpi_signal(query=query, rows=rows, program_id="demo", as_of=NOW) is None


@pytest.mark.parametrize("values", [(1, 999), (999, 1), (0,)])
def test_actual_kpi_stage_withholds_ambiguous_metric_and_keeps_scalar(values: tuple[int, ...]) -> None:
    query = _query()
    rows = [{"Count": value} for value in values]
    state: dict[str, dict[str, Any]] = {}
    signals = ado_kpi_stage.build_kusto_kpi_signals(
        queries=(query,), program=Program(schema_version="2.0", id="demo", name="Demo"),
        program_id="demo", as_of=NOW, workstreams=(), executor=lambda _: rows,
        ado_client_factory=lambda *args, **kwargs: pytest.fail("provider access"),
        normalize_ado_team_name_fn=lambda value: value,
        record_kusto_query_state_fn=kusto_query_helpers.record_kusto_query_state,
        build_kusto_kpi_signal_fn=gather._build_kusto_kpi_signal,
        summarize_pull_requests_fn=lambda *args, **kwargs: None,
        pull_request_provider_ref_fn=lambda *args: None,
        pull_request_entity_refs_fn=lambda *args: (),
        query_state_sink=state, previous_query_states={query.id: PREVIOUS},
    )
    assert len(signals) == (1 if len(rows) == 1 else 0)
    assert state[query.id]["last_cycle_succeeded"] is (len(rows) == 1)
    if len(rows) == 1:
        assert signals[0].metadata["result_value"] == "0"
    else:
        assert state[query.id]["value_last_4"] == [7.0, 8.0]
        assert state[query.id]["is_degraded"] is True


def test_actual_kpi_stage_without_state_sink_does_not_call_builder_for_ambiguous_metric() -> None:
    ado_kpi_stage.build_kusto_kpi_signals(
        queries=(_query(),), program=Program(schema_version="2.0", id="demo", name="Demo"),
        program_id="demo", as_of=NOW, workstreams=(), executor=lambda _: [{"Count": 1}, {"Count": 999}],
        ado_client_factory=lambda *args, **kwargs: pytest.fail("provider access"),
        normalize_ado_team_name_fn=lambda value: value,
        record_kusto_query_state_fn=kusto_query_helpers.record_kusto_query_state,
        build_kusto_kpi_signal_fn=lambda **kwargs: pytest.fail("ambiguous result reached KPI builder"),
        summarize_pull_requests_fn=lambda *args, **kwargs: None,
        pull_request_provider_ref_fn=lambda *args: None,
        pull_request_entity_refs_fn=lambda *args: (),
    )


def test_shared_guard_preserves_old_api_diagnostics_empty_and_scalar_priority() -> None:
    assert kusto_scalar_cardinality_error("query", "zero_ok", 2) is None
    assert kusto_scalar_cardinality_error("query", "zero_ok", 0, metric_id="demo.count") is None
    assert kusto_scalar_cardinality_error("query", "scalar_required", 2, metric_id="demo.count") == (
        "Kusto query query expected scalar_required (exactly 1 row); actual 2 row(s)."
    )


def test_diagnostic_multirow_helpers_keep_existing_behavior() -> None:
    query = replace(_query(), metric_id=None)
    rows = [{"Count": 1}, {"Count": 999}]
    state: dict[str, dict[str, Any]] = {}
    assert gather._build_kusto_kpi_signal(query=query, rows=rows, program_id="demo", as_of=NOW) is not None
    assert kusto_query_helpers.build_kusto_signal(query=query, rows=rows, program_id="demo", as_of=NOW) is not None
    assert kusto_query_helpers.extract_kusto_query_numeric_value(query, rows) == 1
    kusto_query_helpers.record_kusto_query_state(state, query, rows=rows, as_of=NOW, duration_ms=0)
    assert state[query.id]["last_cycle_succeeded"] is True


INVALID_SCALAR_ROWS = [
    {"Other": 99}, {"Count": None}, {"Count": ""}, {"Count": "unavailable"},
    {"Count": True}, {"Count": False}, {"Count": float("nan")},
    {"Count": float("inf")}, {"Count": float("-inf")},
    {"Count": "NaN"}, {"Count": "1e999"}, {"Count": [1]},
]


@pytest.mark.parametrize("row", INVALID_SCALAR_ROWS)
def test_invalid_bound_scalar_is_failed_by_real_hydration_and_extraction(row: dict[str, Any], tmp_path: Path) -> None:
    result, signals = _hydrate([row], tmp_path)
    assert len(result.errors) == 1
    assert result.errors[0].retryable is False
    assert "finite numeric" in result.errors[0].message
    assert result.resources.result_sets == ()
    assert result.failed_ref_ids == (("count-query", "kusto_query"),)
    assert signals.signals == ()


@pytest.mark.parametrize("row", INVALID_SCALAR_ROWS)
def test_invalid_bound_scalar_cannot_publish_or_update_numeric_history(row: dict[str, Any]) -> None:
    query = _query()
    rows = [row]
    state: dict[str, dict[str, Any]] = {}
    assert gather._build_kusto_kpi_signal(query=query, rows=rows, program_id="demo", as_of=NOW) is None
    assert kusto_query_helpers.extract_kusto_query_numeric_value(query, rows) is None
    assert kusto_query_helpers.build_kusto_signals(
        queries=(query,), program_id="demo", as_of=NOW, executor=lambda _: rows,
        query_state_sink=state, previous_query_states={query.id: PREVIOUS},
    ) == ()
    assert state[query.id]["last_cycle_succeeded"] is False
    assert state[query.id]["is_degraded"] is True
    assert "finite numeric" in state[query.id]["last_error"]
    assert state[query.id]["last_succeeded_at"] == datetime(2026, 10, 24, tzinfo=timezone.utc)
    assert state[query.id]["value_last_4"] == [7.0, 8.0]
    ado_kpi_stage.build_kusto_kpi_signals(
        queries=(query,), program=Program(schema_version="2.0", id="demo", name="Demo"),
        program_id="demo", as_of=NOW, workstreams=(), executor=lambda _: rows,
        ado_client_factory=lambda *args, **kwargs: pytest.fail("provider access"),
        normalize_ado_team_name_fn=lambda value: value,
        record_kusto_query_state_fn=kusto_query_helpers.record_kusto_query_state,
        build_kusto_kpi_signal_fn=lambda **kwargs: pytest.fail("invalid metric reached KPI builder"),
        summarize_pull_requests_fn=lambda *args, **kwargs: None,
        pull_request_provider_ref_fn=lambda *args: None,
        pull_request_entity_refs_fn=lambda *args: (),
    )


@pytest.mark.parametrize("value", [0, -2, 1.25, "0", "2.5"])
def test_finite_bound_scalar_remains_accepted(value: int | float | str, tmp_path: Path) -> None:
    result, signals = _hydrate([{"Count": value}], tmp_path)
    assert result.errors == ()
    assert result.resources.result_sets[0].observed_value == float(value)
    assert signals.signals
    assert kusto_query_helpers.extract_kusto_query_numeric_value(_query(), [{"Count": value}]) == float(value)


def test_formatted_scalar_uses_same_parser_in_hydration_history_and_projection(tmp_path: Path) -> None:
    from src.commands.gather_pipeline.kpi_projection_stage import coerce_metric_value

    value = "1,200%"
    result, signals = _hydrate([{"Count": value}], tmp_path)
    assert result.errors == ()
    assert result.resources.result_sets[0].observed_value == 1200
    assert signals.signals
    assert coerce_metric_value(value) == 1200
    assert kusto_query_helpers.extract_kusto_query_numeric_value(_query(), [{"Count": value}]) == 1200


@pytest.mark.parametrize("value", [True, False, None, {}, "NaN", "inf", float("-inf"), "private-raw-value"])
def test_existing_observation_projection_import_rejects_invalid_numeric_value(value: Any) -> None:
    from src.commands.gather_pipeline.kpi_projection_stage import coerce_metric_value

    assert coerce_metric_value(value) is None


def test_missing_bound_column_contract_does_not_guess_first_value() -> None:
    from src.core.metric_binding_validator import kusto_metric_result_error

    query = replace(_query(), result_column=None)
    rows = [{"Count": 2}]
    assert kusto_metric_result_error(
        query.id, query.expected_cardinality, rows, metric_id=query.metric_id,
    )
    assert kusto_query_helpers.extract_kusto_query_numeric_value(query, rows) is None
    assert gather._build_kusto_kpi_signal(query=query, rows=rows, program_id="demo", as_of=NOW) is None


@pytest.mark.parametrize("value", [True, None, "NaN", "inf", "unavailable"])
def test_observation_projection_rejects_invalid_scalar_and_records_failed_run(value: Any, tmp_path: Path) -> None:
    from src.commands.gather_pipeline.kpi_projection_stage import project_kpi_signals_to_observations

    query = _query()
    assert query.metric_id is not None
    store = RealityStore("demo", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_metric_source_binding(MetricSourceBinding(
        binding_id="count-binding", metric_id=query.metric_id, program_id="demo",
        source_kind="kusto", cluster=query.cluster, database=query.database,
        kql_template=query.kql, result_column=query.result_column,
    ))
    signal = gather._build_kusto_kpi_signal(
        query=query, rows=[{"Count": 1}], program_id="demo", as_of=NOW,
    )
    assert signal is not None
    assert signal.metadata is not None
    signal = replace(signal, metadata={
        **signal.metadata, "result_value": str(value), "result_json": json.dumps({"Count": value}),
    })
    state: dict[str, dict[str, Any]] = {}
    kusto_query_helpers.record_kusto_query_state(
        state, query, rows=[{"Count": value}], as_of=NOW, duration_ms=0,
    )
    assert project_kpi_signals_to_observations(
        "demo", queries=(query,), as_of=NOW, kpi_signals=(signal,),
        query_states=state, store=store,
    ) == ()
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute(
            "SELECT status, metrics_observed FROM reality_ingestion_runs WHERE source_ref = ?",
            (query.id,),
        ).fetchall() == [("failed", 0)]
