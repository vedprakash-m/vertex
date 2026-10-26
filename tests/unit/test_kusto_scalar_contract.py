from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.commands import gather
from src.commands.gather_pipeline import ado_kpi_stage, kusto_query_helpers, uil_loader_stage
from src.core.integration_types import ChannelRegistration, RegistrationStatus
from src.core.kusto_hydration import KustoHydrationConfig, KustoHydrationProvider
from src.core.kusto_signal_extractor import KustoSignalExtractor
from src.core.models_v2 import KustoQuery, Program


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
PREVIOUS = {"last_succeeded_at": "2025-12-31T00:00:00+00:00", "value_last_4": [7.0, 8.0]}


def _query(cardinality: str = "scalar_required") -> KustoQuery:
    return KustoQuery(
        id="synthetic-scalar",
        cluster="synthetic",
        database="synthetic",
        kql="print Value=0",
        section="Synthetic",
        render_as="metric_highlight",
        confidence="high",
        result_column="Value",
        metric_id="synthetic.metric",
        expected_cardinality=cardinality,
        validated=True,
        slo_target=2,
        comparison=">=",
    )


def _rows(count: int, value: object = 3) -> list[dict[str, Any]]:
    return [{"Value": value, "Diagnostic": "must-not-appear-in-error"} for _ in range(count)]


def _error(count: int) -> str:
    return f"Kusto query synthetic-scalar expected scalar_required (exactly 1 row); actual {count} row(s)."


@pytest.mark.parametrize("count", [0, 1, 2])
def test_scalar_gather_helpers_enforce_cardinality(count: int) -> None:
    query = _query()
    rows = _rows(count)
    state: dict[str, dict[str, Any]] = {}
    signals = kusto_query_helpers.build_kusto_signals(
        queries=(query,), program_id="synthetic", as_of=NOW, executor=lambda _: rows,
        query_state_sink=state, previous_query_states={query.id: PREVIOUS},
    )
    assert len(signals) == (1 if count == 1 else 0)
    assert kusto_query_helpers.extract_kusto_query_numeric_value(query, rows) == (3.0 if count == 1 else None)
    assert state[query.id]["row_count"] == count
    assert state[query.id]["last_cycle_succeeded"] is (count == 1)
    assert state[query.id]["is_degraded"] is (count != 1)
    assert state[query.id]["last_error"] == (None if count == 1 else _error(count))
    assert state[query.id]["last_succeeded_at"] == (
        NOW if count == 1 else datetime(2025, 12, 31, tzinfo=timezone.utc)
    )
    assert state[query.id]["value_last_4"] == ([7.0, 8.0, 3.0] if count == 1 else [7.0, 8.0])


@pytest.mark.parametrize("count", [0, 2])
def test_direct_builders_and_state_without_sink_cannot_bypass_scalar_contract(count: int) -> None:
    query = _query()
    rows = _rows(count)
    assert kusto_query_helpers.build_kusto_signal(
        query=query, rows=rows, program_id="synthetic", as_of=NOW,
    ) is None
    assert gather._build_kusto_kpi_signal(
        query=query, rows=rows, program_id="synthetic", as_of=NOW,
    ) is None
    assert kusto_query_helpers.build_kusto_signals(
        queries=(query,), program_id="synthetic", as_of=NOW, executor=lambda _: rows,
    ) == ()


def test_scalar_violation_preserves_existing_error_and_previous_history() -> None:
    state: dict[str, dict[str, Any]] = {}
    kusto_query_helpers.record_kusto_query_state(
        state, _query(), rows=_rows(2), as_of=NOW, duration_ms=12,
        error="existing provider failure", previous_state=PREVIOUS,
    )
    assert state["synthetic-scalar"]["last_error"] == "existing provider failure"
    assert state["synthetic-scalar"]["is_degraded"] is True
    assert state["synthetic-scalar"]["value_last_4"] == [7.0, 8.0]


@pytest.mark.parametrize(
    ("cardinality", "count"),
    [("scalar_required", 0), ("scalar_required", 2), ("zero_ok", 2), ("legacy-authored-value", 2)],
)
def test_kpi_stage_rejects_invalid_scalar_without_state_sink(cardinality: str, count: int) -> None:
    signals = ado_kpi_stage.build_kusto_kpi_signals(
        queries=(_query(cardinality),), program=Program(schema_version="2.0", id="synthetic", name="Synthetic"),
        program_id="synthetic", as_of=NOW, workstreams=(), executor=lambda _: _rows(count),
        ado_client_factory=lambda *args, **kwargs: pytest.fail("provider access"),
        normalize_ado_team_name_fn=lambda value: value,
        record_kusto_query_state_fn=kusto_query_helpers.record_kusto_query_state,
        build_kusto_kpi_signal_fn=lambda **kwargs: pytest.fail("invalid scalar reached KPI builder"),
        summarize_pull_requests_fn=lambda *args, **kwargs: None,
        pull_request_provider_ref_fn=lambda *args: None,
        pull_request_entity_refs_fn=lambda *args: (),
    )
    assert signals == ()


def test_invalid_scalar_without_previous_state_is_not_successful_zero() -> None:
    state: dict[str, dict[str, Any]] = {}
    kusto_query_helpers.record_kusto_query_state(
        state, _query(), rows=[], as_of=NOW, duration_ms=1,
    )
    assert state["synthetic-scalar"]["last_succeeded_at"] is None
    assert state["synthetic-scalar"]["zero_rows_ok"] is False
    assert state["synthetic-scalar"]["last_cycle_succeeded"] is False
    assert state["synthetic-scalar"]["is_degraded"] is True
    assert "value_last_4" not in state["synthetic-scalar"]


@pytest.mark.parametrize("count", [0, 1, 2])
def test_actual_kpi_stage_enforces_scalar_and_records_failure(count: int) -> None:
    query = _query()
    state: dict[str, dict[str, Any]] = {}
    signals = ado_kpi_stage.build_kusto_kpi_signals(
        queries=(query,), program=Program(schema_version="2.0", id="synthetic", name="Synthetic"),
        program_id="synthetic", as_of=NOW, workstreams=(), executor=lambda _: _rows(count),
        ado_client_factory=lambda *args, **kwargs: pytest.fail("provider access"),
        normalize_ado_team_name_fn=lambda value: value,
        record_kusto_query_state_fn=kusto_query_helpers.record_kusto_query_state,
        build_kusto_kpi_signal_fn=gather._build_kusto_kpi_signal,
        summarize_pull_requests_fn=lambda *args, **kwargs: None,
        pull_request_provider_ref_fn=lambda *args: None,
        pull_request_entity_refs_fn=lambda *args: (),
        query_state_sink=state, previous_query_states={query.id: PREVIOUS},
    )
    assert len(signals) == (1 if count == 1 else 0)
    assert state[query.id]["last_error"] == (None if count == 1 else _error(count))
    assert state[query.id]["value_last_4"] == ([7.0, 8.0, 3.0] if count == 1 else [7.0, 8.0])


def _hydrate(query: KustoQuery, rows: list[dict[str, Any]], root: Path):
    registration = ChannelRegistration(
        channel="kusto", program_id="synthetic", provider_instance_id="default",
        ref_id=query.id, ref_kind="kusto_query", status=RegistrationStatus.ACTIVE,
        first_discovered_at=NOW, last_seen_at=NOW,
    )
    provider = KustoHydrationProvider(
        executor=lambda _: rows, query_loader=lambda *args, **kwargs: (query,),
    )
    return provider.hydrate((registration,), NOW, "synthetic", KustoHydrationConfig(programs_root=root))


@pytest.mark.parametrize("count", [0, 1, 2])
def test_hydration_rejects_scalar_violation_before_slo_or_extraction(count: int, tmp_path: Path) -> None:
    result = _hydrate(_query(), _rows(count), tmp_path)
    if count == 1:
        assert result.errors == ()
        assert result.resources.result_sets[0].observed_value == 3.0
        assert result.resources.result_sets[0].is_breach is False
    else:
        assert result.resources.result_sets == ()
        assert result.hydrated_ref_ids == ()
        assert result.failed_ref_ids == (("synthetic-scalar", "kusto_query"),)
        assert len(result.errors) == 1
        assert result.errors[0].message == _error(count)
        assert result.errors[0].retryable is False
        assert KustoSignalExtractor().extract(result.resources, "synthetic").signals == ()
        state: dict[str, dict[str, Any]] = {}
        uil_loader_stage.record_uil_kusto_query_states(
            "synthetic",
            binding=SimpleNamespace(
                hydration_provider=SimpleNamespace(_query_loader=lambda *args, **kwargs: (_query(),)),
                hydration_config=SimpleNamespace(programs_root=tmp_path),
            ),
            hydration_result=result, as_of=NOW, query_state_sink=state,
            previous_query_states={"synthetic-scalar": PREVIOUS}, include_unvalidated=False,
            record_kusto_query_state_fn=kusto_query_helpers.record_kusto_query_state,
        )
        assert state["synthetic-scalar"]["last_error"] == _error(count)
        assert state["synthetic-scalar"]["last_cycle_succeeded"] is False
        assert state["synthetic-scalar"]["is_degraded"] is True
        assert state["synthetic-scalar"]["value_last_4"] == [7.0, 8.0]
        assert state["synthetic-scalar"]["last_succeeded_at"] == datetime(2025, 12, 31, tzinfo=timezone.utc)


@pytest.mark.parametrize("value", [0, "0", "3.25"])
def test_valid_scalar_zero_and_numeric_strings_keep_existing_interpretation(value: object, tmp_path: Path) -> None:
    query = _query()
    rows = _rows(1, value)
    assert kusto_query_helpers.extract_kusto_query_numeric_value(query, rows) == float(value)
    signal = gather._build_kusto_kpi_signal(query=query, rows=rows, program_id="synthetic", as_of=NOW)
    assert signal is not None
    assert signal.text == f"KPI Synthetic: {value}"
    result = _hydrate(query, rows, tmp_path)
    assert result.errors == ()
    assert result.resources.result_sets[0].observed_value == float(value)
    assert result.resources.result_sets[0].is_breach is (float(value) < 2)


@pytest.mark.parametrize("cardinality", ["zero_ok", "legacy-authored-value"])
@pytest.mark.parametrize("count", [0, 2])
def test_non_scalar_compatibility(cardinality: str, count: int, tmp_path: Path) -> None:
    query = _query(cardinality)
    rows = _rows(count)
    state: dict[str, dict[str, Any]] = {}
    kusto_query_helpers.record_kusto_query_state(
        state, query, rows=rows, as_of=NOW, duration_ms=0,
        previous_state=PREVIOUS,
    )
    assert state[query.id]["last_cycle_succeeded"] is (count == 0)
    assert state[query.id]["value_last_4"] == [7.0, 8.0]
    if count:
        assert state[query.id]["is_degraded"] is True
        assert state[query.id]["last_error"]
        assert "must-not-appear-in-error" not in state[query.id]["last_error"]
    else:
        assert state[query.id]["last_error"] is None
    signal = gather._build_kusto_kpi_signal(query=query, rows=rows, program_id="synthetic", as_of=NOW)
    assert signal is None
    assert kusto_query_helpers.extract_kusto_query_numeric_value(query, rows) is None
    assert kusto_query_helpers.build_kusto_signal(
        query=query, rows=rows, program_id="synthetic", as_of=NOW,
    ) is None
    assert kusto_query_helpers.build_kusto_signals(
        queries=(query,), program_id="synthetic", as_of=NOW, executor=lambda _: rows,
    ) == ()
    result = _hydrate(query, rows, tmp_path)
    if count:
        assert result.resources.result_sets == ()
        assert result.failed_ref_ids == ((query.id, "kusto_query"),)
        assert len(result.errors) == 1
        assert result.errors[0].retryable is False
        assert KustoSignalExtractor().extract(result.resources, "synthetic").signals == ()
    else:
        assert result.errors == ()
        assert result.resources.result_sets[0].observed_value is None


def test_scalar_contract_does_not_change_formatted_kpi_projection_parsing() -> None:
    from src.commands.gather_pipeline.kpi_projection_stage import coerce_metric_value

    query = _query()
    signal = gather._build_kusto_kpi_signal(
        query=query, rows=_rows(1, "1,200%"), program_id="synthetic", as_of=NOW,
    )
    assert signal is not None
    assert signal.metadata["result_value"] == "1,200%"
    assert coerce_metric_value(signal.metadata["result_value"]) == 1200.0
