"""Direct coverage for the extracted KPI projection gather stage."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sqlite3

from src.commands.gather_pipeline import kpi_projection_stage
from src.core.metric_models import MetricSourceBinding
from src.core.models import Confidence
from src.core.models_v2 import KustoQuery, Signal
from src.core.reality_store import RealityStore
from src.core.source_models import SourceKind


def test_project_kpi_signals_to_observations_persists_observation_and_success_run(tmp_path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "vertex-db")
    store.initialize()
    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="acme-deploy-binding",
            metric_id="acme.deploy_p50_hours",
            program_id="acme",
            source_kind="kusto",
            cluster="https://adventure.kusto.windows.net",
            database="xdataanalytics",
            kql_template="Metrics | take 1",
            result_column="P50",
        )
    )

    query = KustoQuery(
        id="acme-deployment-velocity",
        metric_id="acme.deploy_p50_hours",
        cluster="https://adventure.kusto.windows.net",
        database="xdataanalytics",
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
            "KPI query acme-stg-validation-open returned a non-numeric result; skipping MetricObservation projection.",
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
            cluster="https://adventure.kusto.windows.net",
            database="xdataanalytics",
            kql_template="Metrics | take 1",
            result_column="P50",
        )
    )
    query = KustoQuery(
        id="acme-deployment-velocity",
        metric_id="acme.deploy_p50_hours",
        cluster="https://adventure.kusto.windows.net",
        database="xdataanalytics",
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
