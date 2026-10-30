"""Direct coverage for the extracted KPI projection gather stage."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import NAMESPACE_URL, uuid5

import pytest

from src.commands.gather_pipeline import kpi_projection_stage
from src.core.exceptions import QueryError
from src.core.metric_models import MetricAggregation, MetricDefinition, MetricSourceBinding, ObservationWindow
from src.core.models import Confidence
from src.core.models_v2 import KustoQuery, Signal
from src.core.reality_store import RealityStore
from src.core.source_models import SourceKind


@pytest.mark.parametrize("row,expected", [
    ({"measurement_period_start": "2026-10-24", "measurement_period_end": "2026-10-25"}, "ordered"),
    ({"measurement_period_start": "2026-10-25", "measurement_period_end": "2026-10-25"}, "ordered"),
    ({"window_start": "2026-10-25T02:00:00+02:00", "window_end": "2026-10-25T00:00:00Z"}, "ordered"),
    ({}, "ordered"),
    ({"window_start": "not-a-date", "window_end": "not-a-date"}, "ordered"),
    ({"measurement_period_start": "2026-10-26", "measurement_period_end": "2026-10-25"}, "reversed"),
    ({"window_start": "2026-10-25T01:00:00Z", "window_end": "2026-10-25T02:00:00+02:00"}, "reversed"),
    ({"Count": "not-numeric", "window_start": "2026-10-26", "window_end": "2026-10-25"}, "nonnumeric"),
])
def test_metric_observation_builder_admits_only_ordered_resolved_windows(
    row: dict[str, str], expected: str,
) -> None:
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    query = KustoQuery(
        id="count", metric_id="demo.count", section="Count", render_as="metric_highlight",
        confidence="high", result_column="Count", cluster="synthetic", database="synthetic",
        kql="print Count=1",
    )
    binding = MetricSourceBinding(
        binding_id="count", metric_id="demo.count", program_id="demo", source_kind="kusto",
    )
    signal = Signal(
        id="count", timestamp=now, source="kusto_kpi", program_id="demo",
        workstream_id=None, entity_refs=(), text="Count: 1", raw_ref="synthetic:count",
        confidence=Confidence.HIGH,
        metadata={"result_json": json.dumps({"Count": 1, **row})},
    )
    if expected == "reversed":
        with pytest.raises(QueryError, match="measurement.*start.*end"):
            kpi_projection_stage.build_metric_observation_from_kpi_signal(
                program_id="demo", query=query, signal=signal, binding=binding,
                observed_at=now, ingestion_run_id="synthetic",
            )
        return
    observation = kpi_projection_stage.build_metric_observation_from_kpi_signal(
        program_id="demo", query=query, signal=signal, binding=binding,
        observed_at=now, ingestion_run_id="synthetic",
    )
    if expected == "nonnumeric":
        assert observation is None
        return
    assert observation is not None
    assert observation.measurement_period_start <= observation.measurement_period_end


def test_reversed_window_cannot_replace_history_or_stop_independent_query(tmp_path: Path) -> None:
    store = RealityStore("demo", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_metric_source_binding(MetricSourceBinding(
        binding_id="count", metric_id="demo.count", program_id="demo",
        source_kind="kusto", cluster="synthetic", database="synthetic",
        kql_template="print Count=1", result_column="Count",
    ))
    query = KustoQuery(
        id="count", metric_id="demo.count", cluster="synthetic", database="synthetic",
        kql="print Count=1", section="Count", render_as="metric_highlight",
        confidence="high", result_column="Count",
    )
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    signal = Signal(
        id="count", timestamp=now, source="kusto_kpi", program_id="demo",
        workstream_id=None, entity_refs=(), text="Count: 1", raw_ref="synthetic:count",
        confidence=Confidence.HIGH,
        metadata={"query_id": query.id, "result_json": json.dumps({
            "Count": 1, "measurement_period_start": "2026-10-24",
            "measurement_period_end": "2026-10-25",
        })},
    )
    initial = kpi_projection_stage.project_kpi_signals_to_observations(
        "demo", queries=(query,), as_of=now, kpi_signals=(signal,),
        query_states={query.id: {"last_cycle_succeeded": True}}, store=store,
    )
    assert len(initial) == 1
    with sqlite3.connect(store.db_path) as connection:
        before = connection.execute("SELECT * FROM reality_metric_observations").fetchall()
    invalid = replace(signal, metadata={
        "query_id": query.id, "result_json": json.dumps({
            "Count": 99, "measurement_period_start": "2026-10-26",
            "measurement_period_end": "2026-10-25",
        }),
    })
    other_query = replace(query, id="other-count")
    other_signal = replace(signal, id="other-count", metadata={
        "query_id": other_query.id, "result_json": json.dumps({
            "Count": 2, "measurement_period_start": "2026-10-25",
            "measurement_period_end": "2026-10-26",
        }),
    })
    later = datetime(2026, 10, 27, tzinfo=timezone.utc)
    observations = kpi_projection_stage.project_kpi_signals_to_observations(
        "demo", queries=(query, other_query), as_of=later,
        kpi_signals=(invalid, other_signal),
        query_states={q.id: {"last_cycle_succeeded": True} for q in (query, other_query)},
        store=store,
    )
    assert len(observations) == 1
    assert observations[0].value_num == 2
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute(
            "SELECT * FROM reality_metric_observations WHERE observation_id = ?",
            (initial[0].observation_id,),
        ).fetchall() == before
        rows = connection.execute(
            "SELECT source_ref, status, metrics_observed, error_message "
            "FROM reality_ingestion_runs WHERE started_at = ? ORDER BY source_ref",
            (later.isoformat(),),
        ).fetchall()
    assert rows[0][0:3] == ("count", "partial", 0)
    assert "measurement" in rows[0][3]
    assert rows[1] == ("other-count", "success", 1, None)


@pytest.fixture
def dimensional_kpi_case(tmp_path: Path):
    from src.commands import gather

    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    programs_root = tmp_path / "selected" / "programs"
    metrics_root = programs_root.parent / "knowledge" / "metrics"
    metrics_root.mkdir(parents=True)
    metrics_root.joinpath("latency.yaml").write_text(
        "metrics:\n"
        "  - id: demo.latency\n"
        "    title: Latency\n"
        "    unit: ms\n"
        "    aggregation: last\n"
        "    dimension_columns: [Region]\n"
        "    valid_from: '2026-01-01T00:00:00Z'\n"
        "  - id: demo.latency\n"
        "    title: Future latency\n"
        "    unit: ms\n"
        "    aggregation: last\n"
        "    dimension_columns: [WrongClockDimension]\n"
        "    valid_from: '2026-10-28T12:00:00Z'\n",
        encoding="utf-8",
    )
    store = RealityStore("demo", db_root=tmp_path / "db")
    store.initialize()
    query = KustoQuery(
        id="latency", metric_id="demo.latency", section="Latency",
        render_as="metric_highlight", confidence="high", result_column="Latency",
        cluster="synthetic", database="synthetic", kql="synthetic latency",
    )
    store.upsert_metric_source_binding(MetricSourceBinding(
        binding_id="latency", metric_id="demo.latency", program_id="demo",
        source_kind="kusto", result_column="Latency", cluster="synthetic",
        database="synthetic", kql_template=query.kql, validated=True,
    ))
    signal = gather._build_kusto_kpi_signal(
        query=query, rows=[{
            "Latency": 12, "Region": "west", "Unrelated": "not a dimension",
            "measurement_period_end": now.isoformat(),
        }], program_id="demo", as_of=now,
    )
    assert signal is not None
    return store, query, signal, programs_root, now


@pytest.fixture
def unit_kpi_case(dimensional_kpi_case):
    from src.core.hypothesis_models import (
        AssertionOperator, Hypothesis, HypothesisKind, HypothesisStatus, TelemetryAssertion,
    )

    store, query, _, programs_root, now = dimensional_kpi_case
    program_root = programs_root / "demo"
    program_root.mkdir(parents=True)
    program_root.joinpath("kpis.yaml").write_text(json.dumps({"kpis": [{
        "id": query.id, "metric_id": query.metric_id, "cluster": query.cluster,
        "database": query.database, "kql": query.kql, "section": query.section,
        "render_as": query.render_as, "confidence": query.confidence,
        "result_column": query.result_column, "unit": "ms",
        "expected_cardinality": "scalar_required", "validated": True, "refresh_on_gather": True,
    }]}), encoding="utf-8")
    programs_root.parent.joinpath("knowledge", "metrics", "latency.yaml").write_text(
        json.dumps({"metrics": [{
            "id": "demo.latency", "title": "Latency", "unit": "ms", "aggregation": "last",
            "dimension_columns": ["Region"], "policy_version": 7,
            "valid_from": "2026-01-01T00:00:00Z",
        }]}), encoding="utf-8",
    )
    binding = store.get_metric_source_binding("latency")
    assert binding is not None
    store.upsert_metric_source_binding(replace(binding, binding_version=3))
    store.upsert_telemetry_assertion(TelemetryAssertion(
        id="latency", program_id="demo", metric_id="demo.latency",
        window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
        operator=AssertionOperator.LTE, threshold=20, sustain_min_observations=1,
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
    ))
    store.upsert_hypothesis(Hypothesis(
        id="latency", short_id="latency", program_id="demo", kind=HypothesisKind.SCALAR_FACT,
        statement="Latency <= 20", expected_value=20, as_of_date=now.date(),
        telemetry_assertion_id="latency", proposed_by="synthetic", proposed_at=now,
        status=HypothesisStatus.CONFIRMED, confirmed_by="synthetic", confirmed_at=now,
    ))
    return store, programs_root, now


@pytest.mark.parametrize("query_unit,definition_unit,expected", [
    ("s", "ms", False), ("ms", "s", False), ("MS", "ms", False),
    ("milliseconds", "ms", False), ("ms", "ms", True), (None, "ms", True),
    ("s", None, True),
])
def test_authored_unit_admission_reaches_store_reconcile_and_history(
    unit_kpi_case, query_unit, definition_unit, expected: bool,
) -> None:
    from src.commands import gather, metric
    from src.core.metric_registry import load_metric_definition_map
    from src.core.reality_reconciler import reconcile_reality

    store, programs_root, now = unit_kpi_case
    kpis = programs_root / "demo" / "kpis.yaml"
    authored = json.loads(kpis.read_text(encoding="utf-8"))
    authored["kpis"][0]["unit"] = query_unit
    kpis.write_text(json.dumps(authored), encoding="utf-8")
    metrics_root = programs_root.parent / "knowledge" / "metrics"
    registry = metrics_root / "latency.yaml"
    definitions = json.loads(registry.read_text(encoding="utf-8"))
    if definition_unit is None:
        definitions["metrics"] = []
    else:
        definitions["metrics"][0]["unit"] = definition_unit
    registry.write_text(json.dumps(definitions), encoding="utf-8")
    query, = gather._load_refresh_kpi_queries("demo", programs_root=programs_root)
    assert query.unit == query_unit
    signal = gather._build_kusto_kpi_signal(
        query=query, rows=[{"Latency": 12, "Region": "west", "measurement_period_end": now.isoformat()}],
        program_id="demo", as_of=now,
    )
    assert signal is not None
    projected = gather._project_kpi_signals_to_observations(
        "demo", programs_root=programs_root, as_of=now,
        kpi_signals=(signal,), query_states={}, reality_store=store,
    )
    reconcile_reality(
        store=store, as_of=now, write_digest_cache=False,
        metric_definitions_by_id=load_metric_definition_map(metrics_root=metrics_root, as_of=now),
    )
    evaluation, = store.list_assertion_evaluations()
    history = [
        metric._serialize_metric_observation(observation)
        for observation in store.list_metric_observations("demo.latency")
    ]
    assert len(projected) == len(history) == int(expected)
    assert evaluation.value_num == (12 if expected else None)
    assert evaluation.violated is False
    with sqlite3.connect(store.db_path) as connection:
        run, = connection.execute(
            "SELECT status, metrics_observed, error_message FROM reality_ingestion_runs",
        ).fetchall()
    if expected:
        assert history[0]["binding_version"] == 3
        assert run == ("success", 1, None)
    else:
        assert run == (
            "partial", 0,
            "KPI query latency unit does not match its metric definition; skipping MetricObservation projection.",
        )


def test_unit_refusal_preserves_selected_clock_retained_history_and_independent_query(unit_kpi_case) -> None:
    from src.commands import gather, metric
    from src.core.metric_registry import load_metric_definition_map
    from src.core.reality_reconciler import reconcile_reality

    store, programs_root, now = unit_kpi_case
    metrics_root = programs_root.parent / "knowledge" / "metrics"
    registry = metrics_root / "latency.yaml"
    definitions = json.loads(registry.read_text(encoding="utf-8"))
    definition = definitions["metrics"][0]
    definitions["metrics"] = [
        {**definition, "policy_version": 1, "valid_until": "2026-10-28T12:00:00Z"},
        {**definition, "unit": "s", "valid_from": "2026-10-28T12:00:00Z"},
    ]
    registry.write_text(json.dumps(definitions), encoding="utf-8")
    query, = gather._load_refresh_kpi_queries("demo", programs_root=programs_root)
    signal = gather._build_kusto_kpi_signal(
        query=query, rows=[{"Latency": 12, "Region": "west", "measurement_period_end": now.isoformat()}],
        program_id="demo", as_of=now,
    )
    assert signal is not None
    initial, = gather._project_kpi_signals_to_observations(
        "demo", programs_root=programs_root, as_of=now,
        kpi_signals=(signal,), query_states={}, reality_store=store,
    )
    before_json = metric._serialize_metric_observation(store.list_metric_observations("demo.latency")[0])
    with sqlite3.connect(store.db_path) as connection:
        before_sql = connection.execute("SELECT * FROM reality_metric_observations").fetchall()
    later = now.replace(day=29)
    assert load_metric_definition_map(metrics_root=metrics_root, as_of=now)["demo.latency"].policy_version == 1
    assert load_metric_definition_map(metrics_root=metrics_root, as_of=later)["demo.latency"].unit == "s"
    kpis = programs_root / "demo" / "kpis.yaml"
    authored = json.loads(kpis.read_text(encoding="utf-8"))
    authored["kpis"].append({**authored["kpis"][0], "id": "independent", "unit": "s"})
    kpis.write_text(json.dumps(authored), encoding="utf-8")
    signals = tuple(
        gather._build_kusto_kpi_signal(
            query=loaded,
            rows=[{
                "Latency": 99 if loaded.id == "latency" else 8,
                "Region": "west" if loaded.id == "latency" else "east",
                "measurement_period_end": later.isoformat(),
            }], program_id="demo", as_of=later,
        )
        for loaded in gather._load_refresh_kpi_queries("demo", programs_root=programs_root)
    )
    assert all(signal is not None for signal in signals)
    projected = gather._project_kpi_signals_to_observations(
        "demo", programs_root=programs_root, as_of=later,
        kpi_signals=signals, query_states={}, reality_store=store,
    )
    assert [observation.value_num for observation in projected] == [8]
    history = [
        metric._serialize_metric_observation(observation)
        for observation in store.list_metric_observations("demo.latency")
    ]
    assert sorted(row["value_num"] for row in history) == [8, 12]
    assert next(row for row in history if row["observation_id"] == initial.observation_id) == before_json
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute(
            "SELECT * FROM reality_metric_observations WHERE observation_id = ?", (initial.observation_id,),
        ).fetchall() == before_sql
        runs = connection.execute(
            "SELECT source_ref, status, metrics_observed, error_message FROM reality_ingestion_runs "
            "WHERE started_at = ? ORDER BY source_ref", (later.isoformat(),),
        ).fetchall()
    assert runs[0] == ("independent", "success", 1, None)
    assert runs[1][0:3] == ("latency", "partial", 0)
    assert "unit does not match" in runs[1][3]
    reconcile_reality(store=store, as_of=later, write_digest_cache=False)
    evaluation, = store.list_assertion_evaluations()
    assert evaluation.value_num == 8
    assert evaluation.violated is False


@pytest.mark.parametrize("definition_map", [None, {}, "known"])
@pytest.mark.parametrize("value,expected", [(0, 0), ("1,234.5", 1234.5), ("12%", 12)])
def test_unit_compatibility_explicit_map_absence_and_version_independence(
    dimensional_kpi_case, definition_map, value, expected: float,
) -> None:
    store, query, signal, _, now = dimensional_kpi_case
    definition = MetricDefinition(
        id="demo.latency", title="Latency", unit="ms", aggregation=MetricAggregation.LAST,
        dimension_columns=("Region",), policy_version=99,
    )
    binding = store.get_metric_source_binding("latency")
    assert binding is not None
    store.upsert_metric_source_binding(replace(binding, binding_version=2))
    query = replace(query, unit="ms" if definition_map == "known" else "s")
    signal = replace(signal, metadata={
        "query_id": query.id, "result_json": json.dumps({"Latency": value, "Region": "west"}),
    })
    observation, = kpi_projection_stage.project_kpi_signals_to_observations(
        "demo", queries=(query,), as_of=now, kpi_signals=(signal,), query_states={},
        store=store, metric_definitions={"demo.latency": definition} if definition_map == "known" else definition_map,
    )
    assert observation.value_num == expected
    assert observation.binding_version == 2


@pytest.mark.parametrize("mode,message", [
    ("failed", "failed; skipping"), ("numeric", "non-numeric"),
    ("window", "measurement period start is after its end"), ("grain", "declared dimension Region"),
])
def test_unit_mismatch_retains_existing_admission_error_priority(unit_kpi_case, mode: str, message: str) -> None:
    from src.commands import gather

    store, programs_root, now = unit_kpi_case
    kpis = programs_root / "demo" / "kpis.yaml"
    authored = json.loads(kpis.read_text(encoding="utf-8"))
    authored["kpis"][0]["unit"] = "s"
    kpis.write_text(json.dumps(authored), encoding="utf-8")
    query, = gather._load_refresh_kpi_queries("demo", programs_root=programs_root)
    row = {"Latency": 12, "Region": "west", "measurement_period_end": now.isoformat()}
    if mode == "grain":
        del row["Region"]
    if mode == "window":
        row["measurement_period_start"] = now.replace(day=29).isoformat()
    signal = gather._build_kusto_kpi_signal(query=query, rows=[row], program_id="demo", as_of=now)
    assert signal is not None
    if mode == "numeric":
        signal = replace(signal, metadata={"query_id": query.id, "result_json": '{"Latency":"bad","Region":"west"}'})
    projected = gather._project_kpi_signals_to_observations(
        "demo", programs_root=programs_root, as_of=now, kpi_signals=(signal,),
        query_states={query.id: {"last_cycle_succeeded": False}} if mode == "failed" else {},
        reality_store=store,
    )
    assert projected == ()
    assert store.list_metric_observations("demo.latency") == ()
    with sqlite3.connect(store.db_path) as connection:
        status, count, error = connection.execute(
            "SELECT status, metrics_observed, error_message FROM reality_ingestion_runs",
        ).fetchone()
    assert count == 0
    assert status == ("failed" if mode == "failed" else "partial")
    assert message in error
    assert "unit does not match" not in error


def test_declared_grain_survives_actual_gather_store_reconciliation_and_history(
    dimensional_kpi_case, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.commands import gather, metric
    from src.core import metric_registry
    from src.core.hypothesis_models import (
        AssertionOperator, Hypothesis, HypothesisKind, HypothesisStatus, TelemetryAssertion,
    )
    from src.core.reality_reconciler import _select_observations, reconcile_reality
    from src.core.telemetry_assertion_evaluator import evaluate_assertion

    store, query, signal, programs_root, now = dimensional_kpi_case
    monkeypatch.setattr(gather, "_load_refresh_kpi_queries", lambda *args, **kwargs: (query,))
    decoy = programs_root.parent.parent / "ambient" / "metrics"
    decoy.mkdir(parents=True)
    decoy.joinpath("decoy.yaml").write_text("metrics: not-a-list", encoding="utf-8")
    monkeypatch.setattr(metric_registry, "METRICS_ROOT", decoy)
    monkeypatch.chdir(decoy.parent)
    real_loader = metric_registry.load_metric_definition_map
    definition_loads = []

    def load_definitions(*, metrics_root=decoy, as_of=None):
        definition_loads.append((metrics_root, as_of))
        return real_loader(metrics_root=metrics_root, as_of=as_of)

    monkeypatch.setattr(kpi_projection_stage, "load_metric_definition_map", load_definitions, raising=False)

    def project(current_signal):
        return gather._project_kpi_signals_to_observations(
            "demo", programs_root=programs_root, as_of=now,
            kpi_signals=(current_signal,), query_states={}, reality_store=store,
        )

    west = project(signal)[0]
    assert west.dimensions_json == '{"Region":"west"}'
    assert west.value_num == 12
    assert west.source_binding_id == "latency"
    assert west.binding_version == 1
    assert west.measurement_period_start == west.measurement_period_end == now
    east_signal = replace(signal, metadata={
        **signal.metadata,
        "result_json": json.dumps({"Latency": 99, "Region": "east", "measurement_period_end": now.isoformat()}),
    })
    east = project(east_signal)[0]
    assert east.observation_id != west.observation_id
    repeated = project(signal)[0]
    assert definition_loads == [
        (programs_root.parent / "knowledge" / "metrics", now),
    ] * 3
    assert repeated.observation_id == west.observation_id
    assert len(store.list_metric_observations("demo.latency")) == 2
    for region, expected_value, expected_status in [
        ("west", 12, "passed"), ("east", 99, "violated"), ("north", None, "insufficient_data"),
    ]:
        window = ObservationWindow(
            days=7, aggregation=MetricAggregation.LAST, dimensions=(("Region", region),),
        )
        selected = _select_observations(store, "demo.latency", window, now)
        assertion = TelemetryAssertion(
            id=region, program_id="demo", metric_id="demo.latency", window=window,
            operator=AssertionOperator.LTE, threshold=20,
        )
        result = evaluate_assertion(assertion, selected, now, None)
        assert result.status == expected_status
        assert result.observed_value == expected_value
        store.upsert_telemetry_assertion(assertion)
        store.upsert_hypothesis(Hypothesis(
            id=region, short_id=region, program_id="demo", kind=HypothesisKind.SCALAR_FACT,
            statement=f"{region} latency", expected_value=20, as_of_date=now.date(),
            telemetry_assertion_id=region, proposed_by="synthetic", proposed_at=now,
            status=HypothesisStatus.CONFIRMED, confirmed_by="synthetic", confirmed_at=now,
        ))
    reconciled = reconcile_reality(store=store, as_of=now, write_digest_cache=False)
    assert reconciled.evaluations_written == 3
    evaluations = {evaluation.assertion_id: evaluation for evaluation in store.list_assertion_evaluations()}
    assert evaluations["west"].value_num == 12
    assert evaluations["west"].violated is False
    assert evaluations["east"].value_num == 99
    assert evaluations["east"].violated is True
    assert evaluations["north"].value_num is None
    history = json.loads(json.dumps([
        metric._serialize_metric_observation(observation)
        for observation in store.list_metric_observations("demo.latency")
    ]))
    assert {(row["dimensions_json"], row["value_num"]) for row in history} == {
        ('{"Region":"west"}', 12), ('{"Region":"east"}', 99),
    }


@pytest.mark.parametrize("row,defaults", [
    ({}, ()),
    ({"Region": None}, ()),
    ({"Region": 1}, ()),
    ({"Region": True}, ()),
    ({"Region": ["west"]}, ()),
    ({"Region": {"name": "west"}}, ()),
    ({"Region": "west"}, (("Region", "east"),)),
    ({}, (("Region", "west"),)),
])
def test_declared_grain_refusal_preserves_history_and_independent_query(
    dimensional_kpi_case, monkeypatch: pytest.MonkeyPatch, row, defaults,
) -> None:
    from src.commands import gather

    store, query, signal, programs_root, now = dimensional_kpi_case
    monkeypatch.setattr(gather, "_load_refresh_kpi_queries", lambda *args, **kwargs: (query,))
    initial = gather._project_kpi_signals_to_observations(
        "demo", programs_root=programs_root, as_of=now, kpi_signals=(signal,),
        query_states={}, reality_store=store,
    )
    with sqlite3.connect(store.db_path) as connection:
        before = connection.execute("SELECT * FROM reality_metric_observations").fetchall()
    binding = store.get_metric_source_binding("latency")
    store.upsert_metric_source_binding(replace(binding, dimension_defaults=defaults))
    invalid = replace(signal, metadata={
        **signal.metadata, "result_json": json.dumps({"Latency": 777, **row}),
    })
    independent_query = replace(query, id="independent")
    independent = replace(signal, metadata={
        **signal.metadata, "query_id": "independent",
        "result_json": json.dumps({"Latency": 33, "Region": "south"}),
    })
    monkeypatch.setattr(
        gather, "_load_refresh_kpi_queries", lambda *args, **kwargs: (query, independent_query),
    )
    # A same-key conflicting default would also invalidate the independent row.
    # Use another metric/binding so its authored scope remains independent.
    independent_query = replace(independent_query, metric_id="demo.other", kql="synthetic other")
    store.upsert_metric_source_binding(replace(
        binding, binding_id="other", metric_id="demo.other", kql_template="synthetic other",
    ))
    later = datetime(2026, 10, 28, 1, tzinfo=timezone.utc)
    result = gather._project_kpi_signals_to_observations(
        "demo", programs_root=programs_root, as_of=later,
        kpi_signals=(invalid, independent), query_states={}, reality_store=store,
    )
    assert len(result) == 1
    assert result[0].metric_id == "demo.other"
    assert result[0].value_num == 33
    assert store.list_metric_observations("demo.latency") == initial
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute(
            "SELECT * FROM reality_metric_observations WHERE metric_id='demo.latency'",
        ).fetchall() == before
        run = connection.execute(
            "SELECT status, metrics_observed, error_message FROM reality_ingestion_runs "
            "WHERE source_ref='latency' AND started_at=?", (later.isoformat(),),
        ).fetchone()
    assert run[:2] == ("partial", 0)
    assert "dimension" in run[2]
    assert "777" not in run[2]


@pytest.mark.parametrize("dimensions,defaults,row", [
    (("Region",), (), {"Region": "west"}),
    (("Region",), (("Region", "west"), ("Scope", "team")), {"Region": "west"}),
    (("Region",), (), {"Region": ""}),
    (("Region", "Tier"), (("Scope", "team"),), {"Tier": "gold", "Region": "west"}),
    ((), (), {"Region": "ignored"}),
    ((), (("Scope", "team"),), {"Region": "ignored"}),
])
def test_explicit_definition_grain_and_legacy_id_compatibility(
    dimensional_kpi_case, dimensions, defaults, row,
) -> None:
    store, query, signal, programs_root, now = dimensional_kpi_case
    binding = replace(store.get_metric_source_binding("latency"), dimension_defaults=defaults)
    definition = MetricDefinition(
        id="demo.latency", title="Latency", unit="ms",
        aggregation=MetricAggregation.LAST, dimension_columns=dimensions,
    )
    signal = replace(signal, metadata={
        **signal.metadata, "result_json": json.dumps({"Latency": 12, **row}),
    })
    observation = kpi_projection_stage.build_metric_observation_from_kpi_signal(
        program_id="demo", query=query, signal=signal, binding=binding,
        observed_at=now, ingestion_run_id="synthetic", metric_definition=definition,
    )
    expected_dimensions = {**dict(defaults), **{key: row[key] for key in dimensions}}
    assert json.loads(observation.dimensions_json) == expected_dimensions
    old_id = str(uuid5(NAMESPACE_URL, f"demo|latency|latency|{now.isoformat()}"))
    assert (observation.observation_id == old_id) == (not expected_dimensions)
    reordered = replace(definition, dimension_columns=tuple(reversed(dimensions)))
    repeated = kpi_projection_stage.build_metric_observation_from_kpi_signal(
        program_id="demo", query=query, signal=signal, binding=binding,
        observed_at=now, ingestion_run_id="synthetic", metric_definition=reordered,
    )
    assert repeated.observation_id == observation.observation_id


@pytest.mark.parametrize("state,row,error", [
    ({"last_cycle_succeeded": False}, {"Latency": 12}, "failed"),
    ({}, {"Latency": "invalid"}, "non-numeric"),
    ({}, {"Latency": 12, "window_start": "2026-10-29", "window_end": "2026-10-28"}, "measurement"),
])
def test_declared_dimension_admission_retains_existing_error_priority(
    dimensional_kpi_case, state, row, error,
) -> None:
    store, query, signal, programs_root, now = dimensional_kpi_case
    definition = MetricDefinition(
        id="demo.latency", title="Latency", unit="ms",
        aggregation=MetricAggregation.LAST, dimension_columns=("Region",),
    )
    result = kpi_projection_stage.project_kpi_signals_to_observations(
        "demo", queries=(query,), as_of=now,
        kpi_signals=(replace(signal, metadata={
            **signal.metadata, "result_value": None, "result_json": json.dumps(row),
        }),), query_states={query.id: state}, store=store,
        metric_definitions={definition.id: definition},
    )
    assert result == ()
    with sqlite3.connect(store.db_path) as connection:
        run = connection.execute("SELECT error_message FROM reality_ingestion_runs").fetchone()
    assert error in run[0]
    assert "dimension" not in run[0]


@pytest.mark.parametrize("definitions", [None, {}])
def test_pure_projection_without_definition_never_loads_ambient_registry(
    dimensional_kpi_case, monkeypatch: pytest.MonkeyPatch, definitions,
) -> None:
    from src.commands import gather
    from src.core import metric_registry

    store, query, signal, programs_root, now = dimensional_kpi_case
    def forbidden_loader(*args, **kwargs):
        pytest.fail("pure projection consulted ambient metric definitions")

    monkeypatch.setattr(gather, "load_metric_definition_map", forbidden_loader, raising=False)
    monkeypatch.setattr(metric_registry, "load_metric_definition_map", forbidden_loader)
    monkeypatch.setattr(kpi_projection_stage, "load_metric_definition_map", forbidden_loader, raising=False)
    observations = kpi_projection_stage.project_kpi_signals_to_observations(
        "demo", queries=(query,), as_of=now, kpi_signals=(signal,),
        query_states={}, store=store, metric_definitions=definitions,
    )
    assert len(observations) == 1
    assert observations[0].dimensions_json == "{}"
    assert observations[0].value_num == 12
    assert observations[0].observation_id == str(uuid5(
        NAMESPACE_URL, f"demo|latency|latency|{now.isoformat()}",
    ))


@pytest.mark.parametrize("definitions", [None, {}])
def test_refresh_without_selected_registry_preserves_no_read_compatibility(
    dimensional_kpi_case, monkeypatch: pytest.MonkeyPatch, definitions,
) -> None:
    store, query, signal, programs_root, now = dimensional_kpi_case

    def forbidden_loader(*args, **kwargs):
        pytest.fail("refresh projection without selected registry read metric files")

    monkeypatch.setattr(kpi_projection_stage, "load_metric_definition_map", forbidden_loader, raising=False)
    observations = kpi_projection_stage.project_refresh_kpi_signals_to_observations(
        "demo", programs_root=programs_root, as_of=now, kpi_signals=(signal,),
        query_states={}, store=store, load_refresh_kpi_queries_fn=lambda *args, **kwargs: (query,),
        dedupe_queries_fn=lambda queries: queries, metric_definitions=definitions,
    )
    assert len(observations) == 1
    assert observations[0].dimensions_json == "{}"


def test_refresh_explicit_definition_map_wins_over_selected_registry(
    dimensional_kpi_case, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, query, signal, programs_root, now = dimensional_kpi_case

    def forbidden_loader(*args, **kwargs):
        pytest.fail("refresh projection replaced explicitly supplied definition map")

    monkeypatch.setattr(kpi_projection_stage, "load_metric_definition_map", forbidden_loader, raising=False)
    observations = kpi_projection_stage.project_refresh_kpi_signals_to_observations(
        "demo", programs_root=programs_root, as_of=now, kpi_signals=(signal,),
        query_states={}, store=store, load_refresh_kpi_queries_fn=lambda *args, **kwargs: (query,),
        dedupe_queries_fn=lambda queries: queries, metric_definitions={},
        metrics_root=programs_root.parent / "knowledge" / "metrics",
    )
    assert len(observations) == 1
    assert observations[0].dimensions_json == "{}"


def test_project_kpi_signals_to_observations_persists_observation_and_success_run(tmp_path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "vertex-db")
    store.initialize()
    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="acme-deploy-binding",
            metric_id="acme.deploy_p50_hours",
            program_id="acme",
            source_kind="kusto",
            cluster="https://example.kusto.windows.net",
            database="ExampleDb",
            kql_template="Metrics | take 1",
            result_column="P50",
        )
    )

    query = KustoQuery(
        id="acme-deployment-velocity",
        metric_id="acme.deploy_p50_hours",
        cluster="https://example.kusto.windows.net",
        database="ExampleDb",
        kql="Metrics | take 1",
        section="Deployment Velocity",
        render_as="metric_highlight",
        confidence="high",
        engine="kusto",
        result_column="P50",
        workstream_ids=("acme",),
    )
    signal = Signal(
        id="signal-1",
        timestamp=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        source="kusto_kpi",
        program_id="acme",
        workstream_id="acme",
        entity_refs=("WS:acme",),
        text="KPI Deploy P50 (hrs): 4.2",
        raw_ref="kusto_kpi:acme-deployment-velocity",
        confidence=Confidence.HIGH,
        metadata={
            "query_id": "acme-deployment-velocity",
            "result_value": "4.2",
            "result_json": json.dumps({"P50": 4.2, "Timestamp": "2026-05-10T07:00:00Z"}),
        },
    )

    observations = kpi_projection_stage.project_kpi_signals_to_observations(
        "acme",
        queries=(query,),
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        kpi_signals=(signal,),
        query_states={
            "acme-deployment-velocity": {
                "last_attempted_at": "2026-05-10T08:00:00+00:00",
                "last_succeeded_at": "2026-05-10T08:00:00+00:00",
                "last_cycle_succeeded": True,
            }
        },
        store=store,
    )

    assert len(observations) == 1
    assert observations[0].source_binding_id == "acme-deploy-binding"
    assert observations[0].value_num == 4.2
    assert observations[0].measurement_period_end == datetime(2026, 5, 10, 7, 0, tzinfo=timezone.utc)

    with sqlite3.connect(store.db_path) as connection:
        rows = connection.execute(
            "SELECT source_ref, binding_id, status, metrics_observed, signals_written, query_hash, captured_window "
            "FROM reality_ingestion_runs WHERE source_kind = ?",
            (SourceKind.KPI_QUERY.value,),
        ).fetchall()

    assert rows == [
        (
            "acme-deployment-velocity",
            "acme-deploy-binding",
            "success",
            1,
            1,
            hashlib.sha256("Metrics | take 1".encode("utf-8")).hexdigest(),
            "2026-05-10T07:00:00+00:00/2026-05-10T07:00:00+00:00",
        )
    ]


@pytest.mark.parametrize("failed_state", [
    {"last_cycle_succeeded": False},
    {"last_cycle_succeeded": False, "last_error": "query execution failed"},
    {"last_cycle_succeeded": True, "last_error": "query execution failed"},
    {"last_error": "query execution failed"},
])
@pytest.mark.parametrize("supply_signal", [True, False])
def test_current_query_failure_cannot_publish_supplied_numeric_signal(
    tmp_path: Path, failed_state: dict[str, object], supply_signal: bool,
) -> None:
    store = RealityStore("demo", db_root=tmp_path / "db")
    store.initialize()
    binding = MetricSourceBinding(
        binding_id="count-binding", metric_id="demo.count", program_id="demo",
        source_kind="kusto", cluster="synthetic", database="synthetic",
        kql_template="print Count=1", result_column="Count",
    )
    store.upsert_metric_source_binding(binding)
    query = KustoQuery(
        id="count-query", metric_id="demo.count", cluster="synthetic", database="synthetic",
        kql="print Count=1", section="Count", render_as="metric_highlight", confidence="high",
        result_column="Count",
    )
    previous = datetime(2026, 10, 24, tzinfo=timezone.utc)
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    signal = Signal(
        id="count-signal", timestamp=previous, source="kusto_kpi", program_id="demo",
        workstream_id=None, entity_refs=(), text="Count: 1", raw_ref="synthetic:count",
        confidence=Confidence.HIGH,
        metadata={"query_id": query.id, "result_value": "1", "result_json": '{"Count": 1}'},
    )
    baseline = kpi_projection_stage.project_kpi_signals_to_observations(
        "demo", queries=(query,), as_of=previous, kpi_signals=(signal,),
        query_states={query.id: {"last_cycle_succeeded": True}}, store=store,
    )
    assert len(baseline) == 1
    with sqlite3.connect(store.db_path) as connection:
        before = connection.execute("SELECT * FROM reality_metric_observations").fetchall()
    observations = kpi_projection_stage.project_kpi_signals_to_observations(
        "demo", queries=(query,), as_of=now, kpi_signals=(signal,) if supply_signal else (),
        query_states={query.id: {
            **failed_state, "last_attempted_at": now, "last_succeeded_at": previous,
        }}, store=store,
    )
    assert observations == ()
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT * FROM reality_metric_observations").fetchall() == before
        rows = connection.execute(
            "SELECT status, metrics_observed, error_message FROM reality_ingestion_runs WHERE started_at = ?",
            (now.isoformat(),),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0:2] == ("failed", 0)
    assert rows[0][2]


@pytest.mark.parametrize("query_state,has_error,has_signal,has_observation,expected", [
    ({}, False, True, True, "success"),
    ({}, False, False, False, "partial"),
    ({"last_cycle_succeeded": True}, True, True, False, "partial"),
    ({"last_cycle_succeeded": False}, False, True, True, "failed"),
    ({"last_error": "query failed"}, False, True, True, "failed"),
])
def test_ingestion_classification_retains_unknown_and_partial_distinctions(
    query_state: dict[str, object], has_error: bool, has_signal: bool,
    has_observation: bool, expected: str,
) -> None:
    from unittest.mock import MagicMock
    from src.core.metric_models import MetricObservation

    assert kpi_projection_stage.classify_kpi_ingestion_run_status(
        query_state=query_state,
        error_message="binding unavailable" if has_error else None,
        signal=MagicMock(spec=Signal) if has_signal else None,
        observation=MagicMock(spec=MetricObservation) if has_observation else None,
    ) == expected


def test_project_kpi_signals_to_observations_records_failed_run_for_non_numeric_signal(tmp_path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "vertex-db")
    store.initialize()
    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="acme-stg-binding",
            metric_id="acme.stg_validation_open",
            program_id="acme",
            source_kind="wiql",
            cluster="",
            database="",
            kql_template="SELECT [System.Id] FROM WorkItems WHERE [System.IterationPath] = '{current_iteration_path}'",
            result_column="OpenValidationItems",
        )
    )

    query = KustoQuery(
        id="acme-stg-validation-open",
        metric_id="acme.stg_validation_open",
        cluster="",
        database="",
        kql="",
        wiql="SELECT [System.Id] FROM WorkItems WHERE [System.IterationPath] = '{current_iteration_path}'",
        section="Scenarios / STG Sign-Off",
        render_as="metric_highlight",
        confidence="medium",
        engine="wiql",
        result_column="OpenValidationItems",
        workstream_ids=("acme",),
    )
    signal = Signal(
        id="signal-2",
        timestamp=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        source="kusto_kpi",
        program_id="acme",
        workstream_id="acme",
        entity_refs=("WI:1001", "WI:1002", "WS:acme"),
        text="KPI STG Validation Open: unavailable",
        raw_ref="kusto_kpi:acme-stg-validation-open",
        confidence=Confidence.MEDIUM,
        metadata={
            "query_id": "acme-stg-validation-open",
            "result_value": "n/a",
            "result_json": json.dumps({"OpenValidationItems": "n/a"}),
        },
    )

    observations = kpi_projection_stage.project_kpi_signals_to_observations(
        "acme",
        queries=(query,),
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        kpi_signals=(signal,),
        query_states={
            "acme-stg-validation-open": {
                "last_attempted_at": "2026-05-10T08:00:00+00:00",
                "last_succeeded_at": "2026-05-09T08:00:00+00:00",
                "last_cycle_succeeded": False,
                "last_error": "wiql execution failed",
                "max_data_timestamp": "2026-05-10T07:30:00+00:00",
            }
        },
        store=store,
    )

    assert observations == ()

    with sqlite3.connect(store.db_path) as connection:
        rows = connection.execute(
            "SELECT source_ref, status, metrics_observed, signals_written, error_message, captured_window "
            "FROM reality_ingestion_runs WHERE source_kind = ?",
            (SourceKind.KPI_QUERY.value,),
        ).fetchall()

    assert rows == [
        (
            "acme-stg-validation-open",
            "failed",
            0,
            1,
            "KPI query acme-stg-validation-open failed; skipping MetricObservation projection.",
            "2026-05-10T08:00:00+00:00/2026-05-10T08:00:00+00:00",
        )
    ]


def test_project_kpi_signals_to_observations_records_partial_run_when_binding_missing(tmp_path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "vertex-db")
    query = KustoQuery(
        id="acme-open-pr-age-p90",
        metric_id="acme.open_pr_age_p90",
        cluster="",
        database="",
        kql="",
        section="Deployment Velocity",
        render_as="metric_highlight",
        confidence="medium",
        engine="ado_pr",
        result_column="P90AgeDays",
        workstream_ids=("acme",),
    )
    signal = Signal(
        id="signal-3",
        timestamp=datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc),
        source="kusto_kpi",
        program_id="acme",
        workstream_id="acme",
        entity_refs=("PR:XStoreApp/301", "WS:acme"),
        text="KPI Open PR Age P90: 10.0",
        raw_ref="kusto_kpi:acme-open-pr-age-p90",
        confidence=Confidence.MEDIUM,
        metadata={
            "query_id": "acme-open-pr-age-p90",
            "result_value": "10.0",
            "result_json": json.dumps({"P90AgeDays": 10.0}),
        },
    )

    observations = kpi_projection_stage.project_kpi_signals_to_observations(
        "acme",
        queries=(query,),
        as_of=datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc),
        kpi_signals=(signal,),
        query_states={"acme-open-pr-age-p90": {"last_cycle_succeeded": True}},
        store=store,
    )

    assert observations == ()

    with sqlite3.connect(store.db_path) as connection:
        rows = connection.execute(
            "SELECT source_ref, status, metrics_observed, signals_written, error_message, query_hash "
            "FROM reality_ingestion_runs WHERE source_kind = ?",
            (SourceKind.KPI_QUERY.value,),
        ).fetchall()

    assert rows == [
        (
            "acme-open-pr-age-p90",
            "partial",
            0,
            1,
            "No active metric binding matches KPI query acme-open-pr-age-p90 for metric acme.open_pr_age_p90.",
            hashlib.sha256("ado_pr:acme-open-pr-age-p90:acme".encode("utf-8")).hexdigest(),
        )
    ]


def test_project_refresh_kpi_signals_to_observations_returns_empty_when_loader_has_no_queries(tmp_path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "vertex-db")

    observations = kpi_projection_stage.project_refresh_kpi_signals_to_observations(
        "acme",
        programs_root=tmp_path / "programs",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        kpi_signals=(),
        query_states={},
        store=store,
        load_refresh_kpi_queries_fn=lambda *args, **kwargs: (),
        dedupe_queries_fn=lambda queries: queries,
    )

    assert observations == ()


def test_project_refresh_kpi_signals_to_observations_dedupes_loaded_queries(tmp_path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "vertex-db")
    store.initialize()
    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="acme-deploy-binding",
            metric_id="acme.deploy_p50_hours",
            program_id="acme",
            source_kind="kusto",
            cluster="https://example.kusto.windows.net",
            database="ExampleDb",
            kql_template="Metrics | take 1",
            result_column="P50",
        )
    )
    query = KustoQuery(
        id="acme-deployment-velocity",
        metric_id="acme.deploy_p50_hours",
        cluster="https://example.kusto.windows.net",
        database="ExampleDb",
        kql="Metrics | take 1",
        section="Deployment Velocity",
        render_as="metric_highlight",
        confidence="high",
        engine="kusto",
        result_column="P50",
        workstream_ids=("acme",),
    )
    signal = Signal(
        id="signal-refresh",
        timestamp=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        source="kusto_kpi",
        program_id="acme",
        workstream_id="acme",
        entity_refs=("WS:acme",),
        text="KPI Deploy P50 (hrs): 4.2",
        raw_ref="kusto_kpi:acme-deployment-velocity",
        confidence=Confidence.HIGH,
        metadata={
            "query_id": "acme-deployment-velocity",
            "result_value": "4.2",
            "result_json": json.dumps({"P50": 4.2, "Timestamp": "2026-05-10T07:00:00Z"}),
        },
    )

    def _dedupe_queries(queries: tuple[KustoQuery, ...]) -> tuple[KustoQuery, ...]:
        deduped: dict[str, KustoQuery] = {}
        for loaded in queries:
            deduped.setdefault(loaded.id, loaded)
        return tuple(deduped.values())

    observations = kpi_projection_stage.project_refresh_kpi_signals_to_observations(
        "acme",
        programs_root=tmp_path / "programs",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        kpi_signals=(signal,),
        query_states={
            "acme-deployment-velocity": {
                "last_attempted_at": "2026-05-10T08:00:00+00:00",
                "last_succeeded_at": "2026-05-10T08:00:00+00:00",
                "last_cycle_succeeded": True,
            }
        },
        store=store,
        load_refresh_kpi_queries_fn=lambda *args, **kwargs: (query, query),
        dedupe_queries_fn=_dedupe_queries,
    )

    assert len(observations) == 1
