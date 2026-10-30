from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import runpy
import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest

from src.commands import gather
from src.commands.gather_pipeline import ado_kpi_stage, kusto_query_helpers, uil_loader_stage
from src.core.exceptions import ConfigError
from src.core.integration_types import (
    ChannelRegistration,
    ExtractionResult,
    HydrationResult,
    KustoHydrationOutput,
    RegistrationStatus,
)
from src.core.kusto_client import KustoColumn
from src.core.kusto_hydration import KustoHydrationConfig, KustoHydrationProvider
from src.core.kusto_query_loader import load_kpi_queries
from src.core.gather_state_store import load_gather_query_states, write_gather_state
from src.core.kusto_signal_extractor import KustoSignalExtractor
from src.core.metric_binding_validator import kusto_scalar_cardinality_error, validate_metric_source_binding
from src.core.metric_models import MetricSourceBinding
from src.core.metric_registry import _parse_metric_definition
from src.core.knowledge_store import KnowledgeStore
from src.core.models_v2 import KustoQuery, Program
from src.core.reality_store import RealityStore


NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)
PREVIOUS = {"last_succeeded_at": "2026-10-24T00:00:00+00:00", "value_last_4": [7.0, 8.0]}


@pytest.mark.parametrize("field,value", [
    ("unit", "s"), ("engine", "kusto-revised"), ("cluster", "other"),
    ("database", "other"), ("kql", "print Value=2"),
    ("wiql", "SELECT [System.Id] FROM WorkItems"),
    ("result_column", "Revised"), ("metric_id", "demo.revised"),
    ("label", "Editorial only"),
])
def test_actual_numeric_history_tracks_each_authored_identity_field(
    field: str, value: str, tmp_path: Path,
) -> None:
    directory = tmp_path / "demo"
    directory.mkdir()
    path = directory / "kpis.yaml"
    authored = {
        "id": "rate", "metric_id": "demo.rate", "unit": "ms",
        "cluster": "synthetic", "database": "synthetic", "kql": "print Value=1000",
        "result_column": "Value", "validated": True, "refresh_on_gather": True,
        "expected_cardinality": "zero_ok",
    }
    program = Program(schema_version="2.0", id="demo", name="Synthetic Demo")

    def cycle(index: int, *, failed: bool = False, empty: bool = False) -> dict[str, Any]:
        path.write_text(json.dumps({"kpis": [
            authored, {**authored, "id": "independent", "unit": "ms",
                       "engine": "kusto", "cluster": "synthetic", "database": "synthetic",
                       "kql": "print Value=1000", "wiql": None,
                       "metric_id": "demo.rate", "result_column": "Value"},
        ]}), encoding="utf-8")
        previous = load_gather_query_states("demo", programs_root=tmp_path)
        states: dict[str, dict[str, Any]] = {}

        def execute(query: KustoQuery) -> list[dict[str, Any]]:
            if failed and query.id == "rate":
                raise RuntimeError("synthetic execution failure")
            return [] if empty and query.id == "rate" else [{query.result_column: 1000}]

        gather._build_kusto_kpi_signals(
            program=program, program_id="demo", programs_root=tmp_path,
            as_of=NOW + timedelta(days=index), workstreams=(), executor=execute,
            query_state_sink=states, previous_query_states=previous,
        )
        write_gather_state(
            "demo", programs_root=tmp_path, gathered_at=NOW + timedelta(days=index),
            scanned_items=0, discovered_signals=0, new_signals=0, pending_review=0,
            trajectory_updates=0, auto_reviews_written=0, ado_calls=0,
            archived_journal_files=0, background_proposals=0,
            query_states=states, previous_query_states=previous,
        )
        loaded = load_gather_query_states("demo", programs_root=tmp_path)
        assert loaded["independent"]["value_last_4"] == [1000.0] * min(index + 1, 4)
        assert loaded["independent"]["last_cycle_succeeded"] is True
        return loaded["rate"]

    for index in range(5):
        state = cycle(index)
    assert state["value_last_4"] == [1000.0] * 4
    assert state["value_frozen_warning"] is True
    old_identity = state["history_contract_identity"]
    old_success = state["last_succeeded_at"]
    authored[field] = value
    failed = cycle(5, failed=True)
    assert failed["value_last_4"] == state["value_last_4"]
    assert failed["history_contract_identity"] == old_identity
    assert failed["last_succeeded_at"] == old_success
    assert failed["value_frozen_warning"] is False
    overlaid = load_kpi_queries("demo", programs_root=tmp_path)[0]
    assert overlaid.validated is True
    assert overlaid.last_cycle_succeeded is False
    assert overlaid.validated_at == (NOW + timedelta(days=4) if field == "label" else None)
    empty = cycle(6, empty=True)
    assert empty["history_contract_identity"] == old_identity
    assert empty["value_last_4"] == state["value_last_4"]
    assert empty["last_succeeded_at"] == old_success
    assert empty["last_cycle_succeeded"] is True
    assert empty["value_frozen_warning"] is False
    state = cycle(7)
    if field == "label":
        assert state["history_contract_identity"] == old_identity
        assert state["value_last_4"] == [1000.0] * 4
        assert state["value_frozen_warning"] is True
    else:
        assert state["history_contract_identity"] != old_identity
        assert state["value_last_4"] == [1000.0]
        assert state["value_frozen_warning"] is False
        assert state["history_break_reason"] == "contract_changed"
    assert load_kpi_queries("demo", programs_root=tmp_path)[0].validated_at == NOW + timedelta(days=7)
    retained = cycle(8, failed=True)
    assert retained["value_last_4"] == state["value_last_4"]
    assert retained["history_contract_identity"] == state["history_contract_identity"]
    assert retained["history_break_reason"] == state["history_break_reason"]


@pytest.mark.parametrize("legacy", [False, True])
def test_history_empty_failure_and_first_numeric_adoption(
    legacy: bool, tmp_path: Path,
) -> None:
    query = replace(_query(), expected_cardinality="zero_ok")
    previous: dict[str, Any] = dict(PREVIOUS) if legacy else {}
    for rows, error in [([], None), ([{"Count": 99}], "failed"), ([], "failed")]:
        sink: dict[str, dict[str, Any]] = {}
        kusto_query_helpers.record_kusto_query_state(
            sink, query, rows=rows, error=error, as_of=NOW, duration_ms=1,
            previous_state=previous,
        )
        state = sink[query.id]
        assert state.get("value_last_4", []) == previous.get("value_last_4", [])
        assert "history_contract_identity" not in state
        assert state["last_succeeded_at"] == (datetime(2026, 10, 24, tzinfo=timezone.utc) if legacy else None)
    sink = {}
    kusto_query_helpers.record_kusto_query_state(
        sink, query, rows=[{"Count": 99}], as_of=NOW, duration_ms=1, previous_state=previous,
    )
    state = sink[query.id]
    assert state["value_last_4"] == [99.0]
    assert state["history_contract_identity"]
    assert state["history_break_reason"] == "legacy_unverified"


def test_bound_history_transition_diagnostics_survive_persisted_reload(tmp_path: Path) -> None:
    query = _query()
    previous: dict[str, Any] | None = None
    for index in range(5):
        sink: dict[str, dict[str, Any]] = {}
        kusto_query_helpers.record_kusto_query_state(
            sink, query, rows=[{query.result_column: 7}], as_of=NOW,
            duration_ms=1, previous_state=previous,
        )
        previous = sink[query.id]
    assert previous["value_last_4"] == [7.0] * 4
    assert previous["value_frozen_warning"] is True
    query = replace(query, unit="revised", expected_cardinality="zero_ok")
    old_identity = previous["history_contract_identity"]
    for rows, error in [([], None), ([{query.result_column: 7}], "failed"), ([{query.result_column: 7}], None)]:
        sink = {}
        kusto_query_helpers.record_kusto_query_state(
            sink, query, rows=rows, error=error, as_of=NOW + timedelta(days=1),
            duration_ms=1, previous_state=previous,
        )
        write_gather_state(
            "demo", programs_root=tmp_path, gathered_at=NOW + timedelta(days=1),
            scanned_items=0, discovered_signals=0, new_signals=0, pending_review=0,
            trajectory_updates=0, auto_reviews_written=0, ado_calls=0,
            archived_journal_files=0, background_proposals=0,
            query_states=sink,
        )
        state = load_gather_query_states("demo", programs_root=tmp_path)[query.id]
        assert state["value_frozen_warning"] is False
        if not rows or error:
            assert state["value_last_4"] == [7.0] * 4
            assert state["history_contract_identity"] == old_identity
            assert state["history_break_reason"] == "legacy_unverified"
            assert state["last_succeeded_at"] == NOW.isoformat().replace("+00:00", "Z")
        else:
            assert state["value_last_4"] == [7.0]
            assert state["history_contract_identity"] != old_identity
            assert state["history_break_reason"] == "contract_changed"
        previous = state


@pytest.mark.parametrize("comparison", ["gte", ">="])
def test_actual_refresh_kpi_admission_preserves_history_and_independent_query(
    comparison: str, tmp_path: Path,
) -> None:
    program_dir = tmp_path / "demo"
    program_dir.mkdir()
    authored = {
        "id": "rate", "metric_id": "demo.rate", "validated": True, "refresh_on_gather": True,
        "cluster": "synthetic", "database": "synthetic", "kql": "print Rate=99",
        "result_column": "Rate", "unit": "%", "slo_target": 95,
        "comparison": ">=", "expected_cardinality": "scalar_required",
    }
    query_path = program_dir / "kpis.yaml"
    query_path.write_text(json.dumps({"kpis": [authored]}), encoding="utf-8")
    store = RealityStore("demo", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_metric_source_binding(MetricSourceBinding(
        binding_id="rate-binding", metric_id="demo.rate", program_id="demo",
        source_kind="kusto", cluster="synthetic", database="synthetic",
        kql_template="print Rate=99", result_column="Rate",
    ))
    program = Program(schema_version="2.0", id="demo", name="Synthetic Demo")
    initial_state: dict[str, dict[str, Any]] = {}
    initial_signals = gather._build_kusto_kpi_signals(
        program=program, program_id="demo", programs_root=tmp_path, as_of=NOW,
        workstreams=(), executor=lambda _: [{"Rate": 7, "Timestamp": NOW.isoformat()}],
        query_state_sink=initial_state,
    )
    initial = gather._project_kpi_signals_to_observations(
        "demo", programs_root=tmp_path, as_of=NOW, kpi_signals=initial_signals,
        query_states=initial_state, reality_store=store,
    )
    assert len(initial) == 1
    with sqlite3.connect(store.db_path) as connection:
        before = connection.execute("SELECT * FROM reality_metric_observations").fetchall()
    query_path.write_text(json.dumps({"kpis": [
        {**authored, "comparison": comparison}, {**authored, "id": "independent"},
    ]}), encoding="utf-8")
    state: dict[str, dict[str, Any]] = {}
    later = NOW + timedelta(days=1)
    executed: list[str] = []

    def execute(query: KustoQuery) -> list[dict[str, Any]]:
        executed.append(query.id)
        return [{"Rate": 99, "Timestamp": NOW.isoformat()}]

    signals = gather._build_kusto_kpi_signals(
        program=program, program_id="demo", programs_root=tmp_path, as_of=later,
        workstreams=(), executor=execute, query_state_sink=state,
        previous_query_states=initial_state,
    )
    observations = gather._project_kpi_signals_to_observations(
        "demo", programs_root=tmp_path, as_of=later, kpi_signals=signals,
        query_states=state, reality_store=store,
    )
    assert executed == ["rate", "independent"]
    assert initial_state["rate"]["value_last_4"] == [7.0]
    with sqlite3.connect(store.db_path) as connection:
        runs = connection.execute(
            "SELECT source_ref, status, metrics_observed, error_message FROM reality_ingestion_runs "
            "WHERE started_at = ? ORDER BY source_ref", (later.isoformat(),),
        ).fetchall()
        if comparison == "gte":
            assert connection.execute(
                "SELECT * FROM reality_metric_observations WHERE observation_id = ?",
                (initial[0].observation_id,),
            ).fetchall() == before
    assert runs[0] == ("independent", "success", 1, None)
    if comparison == "gte":
        assert [signal.metadata["query_id"] for signal in signals] == ["independent"]
        assert len(observations) == 1
        assert state["rate"]["last_cycle_succeeded"] is False
        assert state["rate"]["is_degraded"] is True
        assert "unsupported SLO comparison" in state["rate"]["last_error"]
        assert state["rate"]["last_succeeded_at"] == NOW
        assert state["rate"]["value_last_4"] == [7.0]
        assert runs[1][0:3] == ("rate", "failed", 0)
        assert runs[1][3] == "KPI query rate failed; skipping MetricObservation projection."
    else:
        assert len(signals) == len(observations) == 2
        assert state["rate"]["last_cycle_succeeded"] is True
        assert state["rate"]["value_last_4"] == [7.0, 99.0]
        assert runs[1] == ("rate", "success", 1, None)


@pytest.mark.parametrize("comparison,target,rows,cardinality,metric_id,error", [
    ("gte", 95, [{"Count": 99}], "scalar_required", "demo.count", "unsupported SLO comparison"),
    ("gte", float("nan"), [{"Count": 99}], "scalar_required", "demo.count", "finite numeric slo_target"),
    ("gte", float("inf"), [{"Count": 99}], "scalar_required", "demo.count", "finite numeric slo_target"),
    ("gte", float("-inf"), [{"Count": 99}], "scalar_required", "demo.count", "finite numeric slo_target"),
    ("gte", True, [{"Count": 99}], "scalar_required", "demo.count", "finite numeric slo_target"),
    ("gte", False, [{"Count": 99}], "scalar_required", "demo.count", "finite numeric slo_target"),
    ("gte", float("nan"), [], "scalar_required", "demo.count", "expected scalar_required"),
    ("gte", float("nan"), [{"Count": 1}, {"Count": 2}], "zero_ok", "demo.count", "scalar interpretation"),
    ("gte", float("nan"), [{"Other": 1}], "scalar_required", "demo.count", "finite numeric value"),
    ("gte", 95, [], "zero_ok", "demo.count", None),
    ("gte", 95, [{"Count": 99}], "scalar_required", None, None),
    ("gte", None, [{"Count": 99}], "scalar_required", "demo.count", None),
    (None, 95, [{"Count": 99}], "scalar_required", "demo.count", None),
    ("gte", float("nan"), [], "zero_ok", None, "finite numeric slo_target"),
    *[(operator, 0, [{"Count": value}], "scalar_required", "demo.count", None)
      for operator in (">=", "<=", "==", ">", "<") for value in (-1, 0, 1)],
])
def test_direct_metric_admission_matches_hydration_and_preserves_query_history(
    comparison: str | None, target: float | None, rows: list[dict[str, Any]],
    cardinality: str, metric_id: str | None, error: str | None, tmp_path: Path,
) -> None:
    query = replace(
        _query(), comparison=comparison, slo_target=target,
        expected_cardinality=cardinality, metric_id=metric_id,
    )
    registration = ChannelRegistration(
        channel="kusto", program_id="demo", provider_instance_id="default",
        ref_id=query.id, ref_kind="kusto_query", status=RegistrationStatus.ACTIVE,
        first_discovered_at=NOW, last_seen_at=NOW,
    )
    hydration = KustoHydrationProvider(
        executor=lambda _: rows, query_loader=lambda *args, **kwargs: (query,),
    ).hydrate((registration,), NOW, "demo", KustoHydrationConfig(programs_root=tmp_path))
    state: dict[str, dict[str, Any]] = {}
    generic = kusto_query_helpers.build_kusto_signals(
        queries=(query,), program_id="demo", as_of=NOW, executor=lambda _: rows,
        query_state_sink=state, previous_query_states={query.id: PREVIOUS},
    )
    kpi = gather._build_kusto_kpi_signal(query=query, rows=rows, program_id="demo", as_of=NOW)
    numeric = kusto_query_helpers.extract_kusto_query_numeric_value(query, rows)
    if error is not None:
        assert error in hydration.errors[0].message
        assert generic == ()
        assert kpi is None
        assert numeric is None
        assert state[query.id]["last_cycle_succeeded"] is False
        assert state[query.id]["is_degraded"] is True
        assert error in state[query.id]["last_error"]
        assert state[query.id]["last_succeeded_at"] == datetime(2026, 10, 24, tzinfo=timezone.utc)
        assert state[query.id]["value_last_4"] == [7.0, 8.0]
    else:
        assert hydration.errors == ()
        assert bool(generic) is bool(rows)
        assert (kpi is not None) is bool(rows)
        assert numeric == (float(rows[0]["Count"]) if rows else None)
        assert state[query.id]["last_cycle_succeeded"] is True
    assert PREVIOUS["value_last_4"] == [7.0, 8.0]


@pytest.mark.parametrize("source", ["kpi", "golden"])
@pytest.mark.parametrize("program_ids", [[], ["demo"], ["other", "demo"], ["other"]])
def test_real_program_scope_controls_execution_signals_and_history(
    source: str, program_ids: list[str], tmp_path: Path,
) -> None:
    program_dir = tmp_path / "demo"
    knowledge_dir = program_dir / "knowledge"
    knowledge_dir.mkdir(parents=True)
    path = program_dir / "kpis.yaml" if source == "kpi" else knowledge_dir / "golden_queries.yaml"
    collection = "kpis" if source == "kpi" else "queries"
    path.write_text(json.dumps({collection: [
        {
            "id": "rate", "program_ids": program_ids, "validated": True,
            "cluster": "synthetic", "database": "synthetic", "kql": "print Rate=99",
            "metric_id": "demo.rate", "result_column": "Rate", "unit": "%",
            "slo_target": 95, "comparison": ">=", "expected_cardinality": "scalar_required",
        },
        {
            "id": "diagnostic", "validated": True, "cluster": "synthetic",
            "database": "synthetic", "kql": "print Message='ok'",
        },
    ]}), encoding="utf-8")
    history_path = program_dir / "gather_state.json"
    history_path.write_text(json.dumps({
        "schema_version": "2.0", "queries": {"rate": {
            "last_cycle_succeeded": True, **PREVIOUS,
        }},
    }), encoding="utf-8")
    history_before = history_path.read_bytes()
    previous_states = {"rate": {**PREVIOUS, "value_last_4": list(PREVIOUS["value_last_4"])}}
    executed: list[str] = []

    def execute(query: KustoQuery) -> list[dict[str, Any]]:
        executed.append(query.id)
        return [{"Rate": 99}] if query.id == "rate" else [{"Message": "ok"}]

    registrations = tuple(
        ChannelRegistration(
            channel="kusto", program_id="demo", provider_instance_id="default",
            ref_id=query_id, ref_kind="kusto_query", status=RegistrationStatus.ACTIVE,
            first_discovered_at=NOW, last_seen_at=NOW,
        )
        for query_id in ("rate", "diagnostic")
    )
    provider = KustoHydrationProvider(executor=execute)
    config = KustoHydrationConfig(programs_root=tmp_path)
    result = provider.hydrate(registrations, NOW, "demo", config)
    signals = KustoSignalExtractor().extract(result.resources, "demo").signals
    state: dict[str, dict[str, Any]] = {}
    uil_loader_stage.record_uil_kusto_query_states(
        "demo", binding=SimpleNamespace(hydration_provider=provider, hydration_config=config),
        hydration_result=result, as_of=NOW, query_state_sink=state,
        previous_query_states=previous_states, include_unvalidated=False,
        record_kusto_query_state_fn=kusto_query_helpers.record_kusto_query_state,
    )
    applicable = not program_ids or "demo" in program_ids
    expected_ids = ["rate", "diagnostic"] if applicable else ["diagnostic"]
    assert executed == expected_ids
    assert [item.query_id for item in result.resources.result_sets] == expected_ids
    assert set(state) == set(expected_ids)
    assert result.api_call_count == len(expected_ids)
    assert all(signal.program_id == "demo" for signal in signals)
    assert signals[-1].text == "Kusto query diagnostic: 1 row(s) observed."
    assert len(signals) == len(expected_ids)
    if applicable:
        assert result.errors == ()
        assert signals[0].text == "demo.rate = 99% against SLO >=95%: OK."
        assert state["rate"]["last_cycle_succeeded"] is True
        assert state["rate"]["value_last_4"] == [99.0]
        assert state["rate"]["history_break_reason"] == "legacy_unverified"
    else:
        assert result.failed_ref_ids == (("rate", "kusto_query"),)
        assert len(result.errors) == 1
        assert result.errors[0].message == "Unknown Kusto query id 'rate'"
        assert result.errors[0].retryable is False
    assert history_path.read_bytes() == history_before
    assert previous_states["rate"] == PREVIOUS


@pytest.mark.parametrize("source", ["kpi", "golden"])
@pytest.mark.parametrize("target", ["true", "false", "null", "0", "-2", "95.5", "'0'", "'95.5'"])
def test_real_authored_target_type_admission(
    source: str, target: str, tmp_path: Path,
) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    knowledge_dir = program_dir / "knowledge"
    knowledge_dir.mkdir(parents=True)
    path = program_dir / "kpis.yaml" if source == "kpi" else knowledge_dir / "golden_queries.yaml"
    collection = "kpis" if source == "kpi" else "queries"
    path.write_text(
        f"{collection}:\n  - id: rate\n    kql: print Rate=0\n    metric_id: demo.rate\n"
        "    result_column: Rate\n    unit: '%'\n    comparison: '>='\n"
        f"    slo_target: {target}\n",
        encoding="utf-8",
    )
    if target in {"true", "false"}:
        with pytest.raises(ConfigError, match="slo_target.*finite"):
            load_kpi_queries("demo", programs_root=programs_root)
    else:
        loaded = load_kpi_queries("demo", programs_root=programs_root)[0]
        assert loaded.slo_target == (None if target == "null" else float(target.strip("'")))
        assert loaded.unit == "%"
        assert loaded.comparison == ">="


@pytest.mark.parametrize("target", [True, False])
@pytest.mark.parametrize(
    "rows,cardinality,comparison,expected_error",
    [
        ([{"Count": 0}], "scalar_required", ">=", "finite numeric slo_target"),
        ([{"Count": 0}], "scalar_required", "gte", "finite numeric slo_target"),
        ([], "scalar_required", ">=", "expected scalar_required"),
        ([{"Count": 1}, {"Count": 2}], "zero_ok", ">=", "scalar interpretation is ambiguous"),
        ([{"Other": 1}], "scalar_required", ">=", "finite numeric value"),
    ],
)
def test_injected_boolean_target_admission_preserves_history_and_error_priority(
    target: bool, rows: list[dict[str, Any]], cardinality: str,
    comparison: str, expected_error: str, tmp_path: Path,
) -> None:
    query = replace(_query(), slo_target=target, comparison=comparison, expected_cardinality=cardinality)
    registration = ChannelRegistration(
        channel="kusto", program_id="demo", provider_instance_id="default",
        ref_id=query.id, ref_kind="kusto_query", status=RegistrationStatus.ACTIVE,
        first_discovered_at=NOW, last_seen_at=NOW,
    )
    provider = KustoHydrationProvider(
        executor=lambda _: rows, query_loader=lambda *args, **kwargs: (query,),
    )
    config = KustoHydrationConfig(programs_root=tmp_path)
    result = provider.hydrate((registration,), NOW, "demo", config)
    assert len(result.errors) == 1
    assert expected_error in result.errors[0].message
    assert result.errors[0].retryable is False
    assert result.resources.result_sets == ()
    assert result.hydrated_ref_ids == ()
    assert result.failed_ref_ids == ((query.id, "kusto_query"),)
    assert KustoSignalExtractor().extract(result.resources, "demo").signals == ()
    state: dict[str, dict[str, Any]] = {}
    uil_loader_stage.record_uil_kusto_query_states(
        "demo", binding=SimpleNamespace(hydration_provider=provider, hydration_config=config),
        hydration_result=result, as_of=NOW, query_state_sink=state,
        previous_query_states={query.id: PREVIOUS}, include_unvalidated=False,
        record_kusto_query_state_fn=kusto_query_helpers.record_kusto_query_state,
    )
    assert state[query.id]["last_cycle_succeeded"] is False
    assert state[query.id]["is_degraded"] is True
    assert state[query.id]["last_succeeded_at"] == datetime(2026, 10, 24, tzinfo=timezone.utc)
    assert state[query.id]["value_last_4"] == [7.0, 8.0]
    assert PREVIOUS["value_last_4"] == [7.0, 8.0]


@pytest.mark.parametrize(
    "comparison,target,metric_id,rows,cardinality,error,breach",
    [
        *[
            (comparison, 0, "demo.rate", [{"Rate": value}], "scalar_required", None, breach)
            for comparison, outcomes in [
                (">=", [True, False, False]),
                ("<=", [False, False, True]),
                ("==", [True, False, True]),
                (">", [True, True, False]),
                ("<", [False, True, True]),
            ]
            for value, breach in zip([-1, 0, 1], outcomes)
        ],
        ("gte", 95, "demo.rate", [{"Rate": 94}], "scalar_required", "unsupported SLO comparison", None),
        ("!=", 0, "demo.rate", [{"Rate": 0}], "scalar_required", "unsupported SLO comparison", None),
        ("=>", 0, "demo.rate", [{"Rate": -1}], "scalar_required", "unsupported SLO comparison", None),
        (None, 0, "demo.rate", [{"Rate": 0}], "scalar_required", None, None),
        ("gte", None, "demo.rate", [{"Rate": 0}], "scalar_required", None, None),
        ("gte", 0, None, [{"Rate": 0}], "scalar_required", None, None),
        ("gte", 0, "demo.rate", [], "zero_ok", None, None),
        ("gte", 0, "demo.rate", [], "scalar_required", "expected scalar_required", None),
        ("gte", 0, "demo.rate", [{"Rate": 1}, {"Rate": 2}], "zero_ok", "scalar interpretation is ambiguous", None),
        ("gte", 0, "demo.rate", [{"Other": 1}], "scalar_required", "finite numeric value", None),
    ],
)
def test_real_loader_comparison_admission_and_boundaries(
    comparison: str | None, target: float | None, metric_id: str | None,
    rows: list[dict[str, Any]], cardinality: str, error: str | None, breach: bool | None,
    monkeypatch, tmp_path: Path,
) -> None:
    program_dir = tmp_path / "demo"
    program_dir.mkdir()
    (program_dir / "kpis.yaml").write_text(
        "kpis:\n  - id: rate\n    validated: true\n    cluster: synthetic\n"
        "    database: synthetic\n    kql: print Rate=0\n    result_column: Rate\n    unit: '%'\n"
        f"    metric_id: {json.dumps(metric_id)}\n    comparison: {json.dumps(comparison)}\n"
        f"    slo_target: {json.dumps(target)}\n    expected_cardinality: {cardinality}\n",
        encoding="utf-8",
    )
    (program_dir / "gather_state.json").write_text(
        json.dumps({"schema_version": "2.0", "queries": {"rate": {
            "last_cycle_succeeded": True, **PREVIOUS,
        }}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "src.core.kusto_query_loader.load_program_knowledge",
        lambda *args, **kwargs: KnowledgeStore((), (), (), (), ()),
    )
    registration = ChannelRegistration(
        channel="kusto", program_id="demo", provider_instance_id="default",
        ref_id="rate", ref_kind="kusto_query", status=RegistrationStatus.ACTIVE,
        first_discovered_at=NOW, last_seen_at=NOW,
    )
    provider = KustoHydrationProvider(executor=lambda _: rows)
    config = KustoHydrationConfig(programs_root=tmp_path)
    result = provider.hydrate((registration,), NOW, "demo", config)
    signals = KustoSignalExtractor().extract(result.resources, "demo").signals
    state: dict[str, dict[str, Any]] = {}
    uil_loader_stage.record_uil_kusto_query_states(
        "demo", binding=SimpleNamespace(hydration_provider=provider, hydration_config=config),
        hydration_result=result, as_of=NOW, query_state_sink=state,
        previous_query_states={"rate": PREVIOUS}, include_unvalidated=False,
        record_kusto_query_state_fn=kusto_query_helpers.record_kusto_query_state,
    )
    if error is not None:
        assert len(result.errors) == 1
        assert error in result.errors[0].message
        assert result.errors[0].retryable is False
        assert result.resources.result_sets == ()
        assert result.hydrated_ref_ids == ()
        assert result.failed_ref_ids == (("rate", "kusto_query"),)
        assert signals == ()
        assert state["rate"]["last_cycle_succeeded"] is False
        assert state["rate"]["is_degraded"] is True
        assert state["rate"]["last_succeeded_at"] == datetime(2026, 10, 24, tzinfo=timezone.utc)
        assert state["rate"]["value_last_4"] == [7.0, 8.0]
    else:
        assert result.errors == ()
        assert result.failed_ref_ids == ()
        assert result.hydrated_ref_ids == (("rate", "kusto_query"),)
        assert result.resources.result_sets[0].is_breach is breach
        assert state["rate"]["last_cycle_succeeded"] is True
        if rows and metric_id is not None:
            value = rows[0]["Rate"]
            if breach is None:
                assert signals[0].text == f"demo.rate = {value:g}%."
            else:
                verdict = "BREACH" if breach else "OK"
                assert signals[0].text == f"demo.rate = {value:g}% against SLO {comparison}{target:g}%: {verdict}."
        elif not rows:
            assert "query or data gap" in signals[0].text
        else:
            assert signals[0].text == "Kusto query rate: 1 row(s) observed."
    assert PREVIOUS["value_last_4"] == [7.0, 8.0]


def test_nonfinite_target_error_precedes_unsupported_comparison(tmp_path: Path) -> None:
    query = replace(_query(), slo_target=float("nan"), comparison="gte")
    registration = ChannelRegistration(
        channel="kusto", program_id="demo", provider_instance_id="default",
        ref_id=query.id, ref_kind="kusto_query", status=RegistrationStatus.ACTIVE,
        first_discovered_at=NOW, last_seen_at=NOW,
    )
    result = KustoHydrationProvider(
        executor=lambda _: [{"Count": 0}], query_loader=lambda *args, **kwargs: (query,),
    ).hydrate((registration,), NOW, "demo", KustoHydrationConfig(programs_root=tmp_path))
    assert len(result.errors) == 1
    assert "finite numeric slo_target" in result.errors[0].message
    assert result.errors[0].retryable is False
    assert result.resources.result_sets == ()


@pytest.mark.parametrize("target,value,verdict", [(95, 94, "BREACH"), (0, -1, "BREACH"), (0, 0, "OK")])
@pytest.mark.parametrize("succeeded", [True, False])
def test_persisted_state_preserves_real_loader_hydration_metric_verdict(
    target: float, value: float, verdict: str, succeeded: bool, monkeypatch, tmp_path: Path,
) -> None:
    program_dir = tmp_path / "demo"
    program_dir.mkdir()
    (program_dir / "kpis.yaml").write_text(
        "kpis:\n  - id: rate\n    validated: true\n    cluster: synthetic\n"
        "    database: synthetic\n    kql: print Rate=0\n    metric_id: demo.rate\n"
        "    result_column: Rate\n    unit: '%'\n    comparison: '>='\n"
        f"    slo_target: {target}\n    expected_cardinality: scalar_required\n",
        encoding="utf-8",
    )
    (program_dir / "gather_state.json").write_text(
        json.dumps({
            "schema_version": "2.0",
            "queries": {"rate": {
                "last_cycle_succeeded": succeeded,
                "last_succeeded_at": PREVIOUS["last_succeeded_at"],
            }},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "src.core.kusto_query_loader.load_program_knowledge",
        lambda *args, **kwargs: KnowledgeStore((), (), (), (), ()),
    )
    registration = ChannelRegistration(
        channel="kusto", program_id="demo", provider_instance_id="default",
        ref_id="rate", ref_kind="kusto_query", status=RegistrationStatus.ACTIVE,
        first_discovered_at=NOW, last_seen_at=NOW,
    )
    result = KustoHydrationProvider(
        executor=lambda _: [{"Rate": value}],
    ).hydrate((registration,), NOW, "demo", KustoHydrationConfig(programs_root=tmp_path))

    assert result.errors == ()
    result_set = result.resources.result_sets[0]
    assert result_set.unit == "%"
    assert result_set.slo_target == target
    assert result_set.comparison == ">="
    assert result_set.is_breach is (verdict == "BREACH")
    signals = KustoSignalExtractor().extract(result.resources, "demo").signals
    assert len(signals) == 1
    assert signals[0].text == f"demo.rate = {value:g}% against SLO >={target:g}%: {verdict}."


def _query() -> KustoQuery:
    authored = runpy.run_path(str(Path(__file__).with_name("test_config_metric_bindings.py")))
    return KustoQuery(
        **authored["query"](), section="Synthetic", render_as="metric_highlight",
        confidence="high", validated=True,
    )


@pytest.mark.parametrize("target", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("value", [0, 3])
def test_nonfinite_slo_target_cannot_emit_verdict_or_advance_uil_history(
    target: float, value: int, tmp_path: Path,
) -> None:
    query = replace(_query(), slo_target=target, comparison=">=")
    registration = ChannelRegistration(
        channel="kusto", program_id="demo", provider_instance_id="default",
        ref_id=query.id, ref_kind="kusto_query", status=RegistrationStatus.ACTIVE,
        first_discovered_at=NOW, last_seen_at=NOW,
    )
    provider = KustoHydrationProvider(
        executor=lambda _: [{"Count": value}],
        query_loader=lambda *args, **kwargs: (query,),
    )
    config = KustoHydrationConfig(programs_root=tmp_path)
    result = provider.hydrate((registration,), NOW, "demo", config)
    assert len(result.errors) == 1
    assert result.errors[0].retryable is False
    assert "slo_target" in result.errors[0].message
    assert "finite" in result.errors[0].message
    assert result.resources.result_sets == ()
    assert result.hydrated_ref_ids == ()
    assert result.failed_ref_ids == ((query.id, "kusto_query"),)
    assert KustoSignalExtractor().extract(result.resources, "demo").signals == ()
    state: dict[str, dict[str, Any]] = {}
    uil_loader_stage.record_uil_kusto_query_states(
        "demo", binding=SimpleNamespace(hydration_provider=provider, hydration_config=config),
        hydration_result=result, as_of=NOW, query_state_sink=state,
        previous_query_states={query.id: PREVIOUS}, include_unvalidated=False,
        record_kusto_query_state_fn=kusto_query_helpers.record_kusto_query_state,
    )
    assert state[query.id]["last_cycle_succeeded"] is False
    assert state[query.id]["is_degraded"] is True
    assert state[query.id]["last_succeeded_at"] == datetime(2026, 10, 24, tzinfo=timezone.utc)
    assert state[query.id]["value_last_4"] == [7.0, 8.0]
    assert PREVIOUS["value_last_4"] == [7.0, 8.0]


@pytest.mark.parametrize("target", [None, 0, -2, 95.5])
def test_hydration_keeps_absent_and_finite_slo_target(target: float | None, tmp_path: Path) -> None:
    query = replace(_query(), slo_target=target, comparison=">=")
    registration = ChannelRegistration(
        channel="kusto", program_id="demo", provider_instance_id="default",
        ref_id=query.id, ref_kind="kusto_query", status=RegistrationStatus.ACTIVE,
        first_discovered_at=NOW, last_seen_at=NOW,
    )
    result = KustoHydrationProvider(
        executor=lambda _: [{"Count": 0}], query_loader=lambda *args, **kwargs: (query,),
    ).hydrate((registration,), NOW, "demo", KustoHydrationConfig(programs_root=tmp_path))
    assert result.errors == ()
    assert result.resources.result_sets[0].observed_value == 0
    assert result.resources.result_sets[0].is_breach == (None if target is None else 0 < target)
    assert KustoSignalExtractor().extract(result.resources, "demo").signals


@pytest.mark.parametrize("target", [None, 0])
def test_slo_target_admission_keeps_empty_zero_ok_gap(target: float | None, tmp_path: Path) -> None:
    query = replace(_query(), slo_target=target, comparison=">=")
    registration = ChannelRegistration(
        channel="kusto", program_id="demo", provider_instance_id="default",
        ref_id=query.id, ref_kind="kusto_query", status=RegistrationStatus.ACTIVE,
        first_discovered_at=NOW, last_seen_at=NOW,
    )
    result = KustoHydrationProvider(
        executor=lambda _: [], query_loader=lambda *args, **kwargs: (query,),
    ).hydrate((registration,), NOW, "demo", KustoHydrationConfig(programs_root=tmp_path))
    assert result.errors == ()
    assert result.resources.result_sets[0].observed_value is None
    assert result.resources.result_sets[0].is_breach is None
    assert "query or data gap" in KustoSignalExtractor().extract(result.resources, "demo").signals[0].text


@pytest.mark.parametrize("rows,cardinality,expected_error", [
    ([], "scalar_required", "expected scalar_required"),
    ([{"Count": 0}, {"Count": 3}], "zero_ok", "scalar interpretation is ambiguous"),
    ([{"Other": 0}], "zero_ok", "declared result column"),
])
def test_invalid_slo_target_preserves_scalar_and_schema_error_priority(
    rows: list[dict[str, Any]], cardinality: str, expected_error: str, tmp_path: Path,
) -> None:
    query = replace(_query(), slo_target=float("nan"), comparison=">=", expected_cardinality=cardinality)
    registration = ChannelRegistration(
        channel="kusto", program_id="demo", provider_instance_id="default",
        ref_id=query.id, ref_kind="kusto_query", status=RegistrationStatus.ACTIVE,
        first_discovered_at=NOW, last_seen_at=NOW,
    )
    result = KustoHydrationProvider(
        executor=lambda _: rows, query_loader=lambda *args, **kwargs: (query,),
    ).hydrate((registration,), NOW, "demo", KustoHydrationConfig(programs_root=tmp_path))
    assert len(result.errors) == 1
    assert expected_error in result.errors[0].message
    assert result.errors[0].retryable is False
    assert result.resources.result_sets == ()


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
