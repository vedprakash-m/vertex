from __future__ import annotations
import pytest
from pathlib import Path
pytestmark = pytest.mark.skipif(not (Path(__file__).resolve().parents[2] / "editions").exists(), reason="Requires private data")

import json
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from cli import app
from src.core.kusto_client import KustoColumn
from src.core.metric_models import MetricAggregation, MetricObservation, MetricQualityState, MetricSourceBinding, ObservationWindow
from src.core.reality_store import RealityStore


runner = CliRunner()


def test_admin_metric_list_command_filters_by_product(monkeypatch, tmp_path: Path) -> None:
    metrics_root = tmp_path / "metrics"
    metrics_root.mkdir()
    (metrics_root / "catalog.yaml").write_text(
        """
metrics:
  - id: acme.cluster_count
    title: Cluster Count
    unit: count
    aggregation: last
    owning_product_id: acme
  - id: fabrikam.deployments_healthy
    title: Healthy Deployments
    unit: count
    aggregation: last
    owning_product_id: fabrikam
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.commands.metric.METRICS_ROOT", metrics_root)

    result = runner.invoke(app, ["admin", "metric", "list", "--product", "acme", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert [entry["metric_id"] for entry in payload] == ["acme.cluster_count"]


def test_admin_metric_bind_command_creates_binding_from_query_catalog(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
schema_version: "1.0"
kpis:
  - id: acme-node-availability-7d
    metric_id: acme.node_availability_7d
    workstream_ids: [acme]
    program_ids: [acme]
    engine: wiql
    wiql: SELECT [System.Id] FROM WorkItems
    section: Fleet Health
    render_as: metric_highlight
    confidence: medium
    refresh_on_gather: true
    result_column: NodeAvailabilityPct
""".strip(),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "admin",
            "metric",
            "bind",
            "--program",
            "acme",
            "--query-id",
            "acme-node-availability-7d",
            "--programs-root",
            str(programs_root),
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    bindings = store.list_active_metric_source_bindings(metric_id="acme.node_availability_7d")

    assert result.exit_code == 0
    assert len(bindings) == 1
    assert bindings[0].source_kind == "wiql"
    assert bindings[0].result_column == "NodeAvailabilityPct"


def test_admin_metric_status_defaults_to_repo_vertex_db(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("VERTEX_DB_PATH", raising=False)

    metrics_root = tmp_path / "metrics"
    metrics_root.mkdir()
    (metrics_root / "acme.yaml").write_text(
        """
metrics:
  - id: acme.node_availability_7d
    title: Acme Node Availability 7d
    unit: percent
    aggregation: last
    slo_target: 99.9
    slo_direction: gte
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.commands.metric.METRICS_ROOT", metrics_root)

    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
schema_version: "1.0"
kpis:
  - id: acme-node-availability-7d
    metric_id: acme.node_availability_7d
    assertion_ids: [assertion-acme-node-availability-7d]
    workstream_ids: [acme]
    program_ids: [acme]
    engine: wiql
    wiql: SELECT [System.Id] FROM WorkItems
    section: Fleet Health
    render_as: metric_highlight
    confidence: medium
    refresh_on_gather: true
    result_column: NodeAvailabilityPct
""".strip(),
        encoding="utf-8",
    )

    db_root = tmp_path / "vertex-db"
    store = RealityStore("acme", db_root=db_root)
    store.initialize()
    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="binding-001",
            metric_id="acme.node_availability_7d",
            program_id="acme",
            source_kind="wiql",
            kql_template="SELECT [System.Id] FROM WorkItems",
            result_column="NodeAvailabilityPct",
        )
    )
    from src.core.hypothesis_models import AssertionOperator, TelemetryAssertion
    store.upsert_telemetry_assertion(
        TelemetryAssertion(
            id="assertion-acme-node-availability-7d",
            program_id="acme",
            metric_id="acme.node_availability_7d",
            window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
            operator=AssertionOperator.GTE,
            threshold=99.9,
        )
    )

    result = runner.invoke(
        app,
        [
            "admin",
            "metric",
            "status",
            "--program",
            "acme",
            "--all-eligible",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload == [
        {
            "query_id": "acme-node-availability-7d",
            "metric_id": "acme.node_availability_7d",
            "eligible": True,
            "eligible_reason": None,
            "binding_count": 1,
            "assertion_count": 1,
            "ready": True,
        }
    ]


def test_admin_metric_provision_command_creates_binding_and_assertion_from_query_catalog(tmp_path: Path) -> None:
    metrics_root = tmp_path / "metrics"
    metrics_root.mkdir()
    (metrics_root / "acme.yaml").write_text(
        """
metrics:
  - id: acme.node_availability_7d
    title: Acme Node Availability 7d
    unit: percent
    aggregation: last
    slo_target: 99.9
    slo_direction: gte
""".strip(),
        encoding="utf-8",
    )

    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
schema_version: "1.0"
kpis:
  - id: acme-node-availability-7d
    metric_id: acme.node_availability_7d
    assertion_ids: [assertion-acme-node-availability-7d]
    workstream_ids: [acme]
    program_ids: [acme]
    engine: wiql
    wiql: SELECT [System.Id] FROM WorkItems
    section: Fleet Health
    render_as: metric_highlight
    confidence: medium
    refresh_on_gather: true
    result_column: NodeAvailabilityPct
""".strip(),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "admin",
            "metric",
            "provision",
            "--program",
            "acme",
            "--query-id",
            "acme-node-availability-7d",
            "--programs-root",
            str(programs_root),
            "--metrics-root",
            str(metrics_root),
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    bindings = store.list_active_metric_source_bindings(metric_id="acme.node_availability_7d")
    assertion = store.get_telemetry_assertion("assertion-acme-node-availability-7d")

    assert result.exit_code == 0
    assert "binding" in result.stdout
    assert len(bindings) == 1
    assert bindings[0].source_kind == "wiql"
    assert assertion is not None
    assert assertion.operator.value == ">="
    assert assertion.threshold == 99.9


def test_admin_metric_provision_command_reuses_existing_rollout_records(tmp_path: Path) -> None:
    metrics_root = tmp_path / "metrics"
    metrics_root.mkdir()
    (metrics_root / "acme.yaml").write_text(
        """
metrics:
  - id: acme.node_availability_7d
    title: Acme Node Availability 7d
    unit: percent
    aggregation: last
    slo_target: 99.9
    slo_direction: gte
""".strip(),
        encoding="utf-8",
    )

    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
schema_version: "1.0"
kpis:
  - id: acme-node-availability-7d
    metric_id: acme.node_availability_7d
    assertion_ids: [assertion-acme-node-availability-7d]
    workstream_ids: [acme]
    program_ids: [acme]
    engine: wiql
    wiql: SELECT [System.Id] FROM WorkItems
    section: Fleet Health
    render_as: metric_highlight
    confidence: medium
    refresh_on_gather: true
    result_column: NodeAvailabilityPct
""".strip(),
        encoding="utf-8",
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="ad7d77c4-a4e1-5e38-b57c-c12b39d67cf8",
            metric_id="acme.node_availability_7d",
            program_id="acme",
            source_kind="wiql",
            kql_template="SELECT [System.Id] FROM WorkItems",
            result_column="NodeAvailabilityPct",
        )
    )

    from src.core.hypothesis_models import TelemetryAssertion, AssertionOperator
    from src.core.metric_models import ObservationWindow, MetricAggregation

    store.upsert_telemetry_assertion(
        TelemetryAssertion(
            id="assertion-acme-node-availability-7d",
            program_id="acme",
            metric_id="acme.node_availability_7d",
            window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
            operator=AssertionOperator.GTE,
            threshold=99.9,
        )
    )

    result = runner.invoke(
        app,
        [
            "admin",
            "metric",
            "provision",
            "--program",
            "acme",
            "--query-id",
            "acme-node-availability-7d",
            "--programs-root",
            str(programs_root),
            "--metrics-root",
            str(metrics_root),
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    bindings = store.list_active_metric_source_bindings(metric_id="acme.node_availability_7d")
    assertions = store.list_active_telemetry_assertions()

    assert result.exit_code == 0
    assert "reused" in result.stdout
    assert len(bindings) == 1
    assert len(assertions) == 1


def test_admin_metric_provision_command_can_bulk_provision_all_eligible_queries(tmp_path: Path) -> None:
    metrics_root = tmp_path / "metrics"
    metrics_root.mkdir()
    (metrics_root / "acme.yaml").write_text(
        """
metrics:
  - id: acme.node_availability_7d
    title: Acme Node Availability 7d
    unit: percent
    aggregation: last
    slo_target: 99.9
    slo_direction: gte
  - id: acme.ready_stamp_count
    title: Acme Ready Stamp Count
    unit: count
    aggregation: last
""".strip(),
        encoding="utf-8",
    )

    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
schema_version: "1.0"
kpis:
  - id: acme-node-availability-7d
    metric_id: acme.node_availability_7d
    assertion_ids: [assertion-acme-node-availability-7d]
    workstream_ids: [acme]
    program_ids: [acme]
    engine: wiql
    wiql: SELECT [System.Id] FROM WorkItems
    section: Fleet Health
    render_as: metric_highlight
    confidence: medium
    refresh_on_gather: true
    result_column: NodeAvailabilityPct
  - id: acme-ready-stamp-count
    metric_id: acme.ready_stamp_count
    workstream_ids: [acme]
    program_ids: [acme]
    cluster: https://example.kusto.windows.net
    database: ExampleDb
    kql: ReadyStamps | summarize ReadyStampCount=count()
    section: Fleet Health
    render_as: metric_highlight
    confidence: medium
    refresh_on_gather: true
    result_column: ReadyStampCount
""".strip(),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "admin",
            "metric",
            "provision",
            "--program",
            "acme",
            "--all-eligible",
            "--programs-root",
            str(programs_root),
            "--metrics-root",
            str(metrics_root),
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    bindings = store.list_active_metric_source_bindings()
    assertions = store.list_active_telemetry_assertions()

    assert result.exit_code == 0
    assert "Provisioned query acme-node-availability-7d" in result.stdout
    assert "Skipped query acme-ready-stamp-count" in result.stdout
    assert "Bulk provision summary: 1 provisioned, 1 skipped." in result.stdout
    assert len(bindings) == 1
    assert len(assertions) == 1
    assert bindings[0].metric_id == "acme.node_availability_7d"
    assert assertions[0].metric_id == "acme.node_availability_7d"


def test_admin_metric_status_command_reports_missing_rollout_for_eligible_query(tmp_path: Path) -> None:
    metrics_root = tmp_path / "metrics"
    metrics_root.mkdir()
    (metrics_root / "acme.yaml").write_text(
        """
metrics:
  - id: acme.node_availability_7d
    title: Acme Node Availability 7d
    unit: percent
    aggregation: last
    slo_target: 99.9
    slo_direction: gte
""".strip(),
        encoding="utf-8",
    )
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
schema_version: "1.0"
kpis:
  - id: acme-node-availability-7d
    metric_id: acme.node_availability_7d
    assertion_ids: [assertion-acme-node-availability-7d]
    workstream_ids: [acme]
    program_ids: [acme]
    engine: wiql
    wiql: SELECT [System.Id] FROM WorkItems
    section: Fleet Health
    render_as: metric_highlight
    confidence: medium
    refresh_on_gather: true
    result_column: NodeAvailabilityPct
""".strip(),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "admin",
            "metric",
            "status",
            "--program",
            "acme",
            "--query-id",
            "acme-node-availability-7d",
            "--programs-root",
            str(programs_root),
            "--metrics-root",
            str(metrics_root),
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    assert result.exit_code == 0
    assert "eligible=yes" in result.stdout
    assert "binding_count=0" in result.stdout
    assert "assertion_count=0" in result.stdout
    assert "ready=no" in result.stdout


def test_admin_metric_status_command_reports_bulk_eligible_rollout_readiness(tmp_path: Path) -> None:
    metrics_root = tmp_path / "metrics"
    metrics_root.mkdir()
    (metrics_root / "acme.yaml").write_text(
        """
metrics:
  - id: acme.node_availability_7d
    title: Acme Node Availability 7d
    unit: percent
    aggregation: last
    slo_target: 99.9
    slo_direction: gte
  - id: acme.ready_stamp_count
    title: Acme Ready Stamp Count
    unit: count
    aggregation: last
""".strip(),
        encoding="utf-8",
    )
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
schema_version: "1.0"
kpis:
  - id: acme-node-availability-7d
    metric_id: acme.node_availability_7d
    assertion_ids: [assertion-acme-node-availability-7d]
    workstream_ids: [acme]
    program_ids: [acme]
    engine: wiql
    wiql: SELECT [System.Id] FROM WorkItems
    section: Fleet Health
    render_as: metric_highlight
    confidence: medium
    refresh_on_gather: true
    result_column: NodeAvailabilityPct
  - id: acme-ready-stamp-count
    metric_id: acme.ready_stamp_count
    workstream_ids: [acme]
    program_ids: [acme]
    cluster: https://example.kusto.windows.net
    database: ExampleDb
    kql: ReadyStamps | summarize ReadyStampCount=count()
    section: Fleet Health
    render_as: metric_highlight
    confidence: medium
    refresh_on_gather: true
    result_column: ReadyStampCount
""".strip(),
        encoding="utf-8",
    )

    provision = runner.invoke(
        app,
        [
            "admin",
            "metric",
            "provision",
            "--program",
            "acme",
            "--query-id",
            "acme-node-availability-7d",
            "--programs-root",
            str(programs_root),
            "--metrics-root",
            str(metrics_root),
            "--db-root",
            str(tmp_path / "db"),
        ],
    )
    assert provision.exit_code == 0

    result = runner.invoke(
        app,
        [
            "admin",
            "metric",
            "status",
            "--program",
            "acme",
            "--all-eligible",
            "--format",
            "json",
            "--programs-root",
            str(programs_root),
            "--metrics-root",
            str(metrics_root),
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload == [
        {
            "query_id": "acme-node-availability-7d",
            "metric_id": "acme.node_availability_7d",
            "eligible": True,
            "eligible_reason": None,
            "binding_count": 1,
            "assertion_count": 1,
            "ready": True,
        }
    ]



def test_admin_metric_validate_command_marks_wiql_binding_validated(monkeypatch, tmp_path: Path) -> None:
    metrics_root = tmp_path / "metrics"
    metrics_root.mkdir()
    (metrics_root / "acme.yaml").write_text(
        """
metrics:
  - id: acme.stg_validation_open
    title: STG validation open
    unit: count
    aggregation: last
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.commands.metric.METRICS_ROOT", metrics_root)

    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text(
        """
schema_version: "2.0"
id: acme
name: Acme
ado:
  organization: your-org
  project: One
  area_paths: [One\\Adventure\\Acme]
  work_item_types: [Feature]
  excluded_states: [Removed]
  date_window_days: 14
  api_timeout_seconds: 30
""".strip(),
        encoding="utf-8",
    )
    (program_dir / "workstreams.yaml").write_text(
        """
schema_version: "2.0"
workstreams:
  - id: acme
    name: Acme
    area_paths: [One\\Adventure\\Acme]
""".strip(),
        encoding="utf-8",
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="binding-wiql",
            metric_id="acme.stg_validation_open",
            program_id="acme",
            source_kind="wiql",
            kql_template="SELECT [System.Id] FROM WorkItems WHERE [System.IterationPath] = '{current_iteration_path}'",
            result_column="OpenValidationItems",
        )
    )

    class _FakeADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def list_team_iterations(self, timeframe: str | None = None, team: str | None = None) -> list[dict[str, object]]:
            assert timeframe == "current"
            assert team is None
            return [{"id": "iteration-24", "path": "One\\Sprint 24"}]

        def execute_wiql(self, wiql: str, top: int | None = None) -> list[int]:
            assert wiql == "SELECT [System.Id] FROM WorkItems WHERE [System.IterationPath] = 'One\\Sprint 24'"
            return [1001, 1002, 1003]

    monkeypatch.setattr("src.commands.metric.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.metric.ADOClient", _FakeADOClient)

    result = runner.invoke(
        app,
        [
            "admin",
            "metric",
            "validate",
            "--program",
            "acme",
            "--binding-id",
            "binding-wiql",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    updated = store.get_metric_source_binding("binding-wiql")

    assert result.exit_code == 0
    assert "Validated binding binding-wiql for metric acme.stg_validation_open." in result.stdout
    assert updated is not None
    assert updated.validated is True
    assert updated.last_validated_kql_hash is not None

def test_admin_metric_history_command_flags_corrected_rows(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="binding-001",
            metric_id="acme.cluster_count",
            program_id="acme",
            source_kind="kusto",
            cluster="https://cluster.kusto.windows.net",
            database="MetricsDb",
            kql_template="Metrics | summarize Count=count()",
            result_column="Count",
        )
    )
    store.write_metric_observation(
        MetricObservation(
            observation_id="obs-001",
            program_id="acme",
            metric_id="acme.cluster_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 1, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 1, 5, tzinfo=timezone.utc),
            value_num=175.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
            source_binding_id="binding-001",
            binding_version=1,
        )
    )
    store.write_metric_observation(
        MetricObservation(
            observation_id="obs-002",
            program_id="acme",
            metric_id="acme.cluster_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 1, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 1, 10, tzinfo=timezone.utc),
            value_num=177.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
            source_binding_id="binding-001",
            binding_version=2,
            corrected_at=datetime(2026, 5, 20, 1, 11, tzinfo=timezone.utc),
            corrected_reason="late source correction",
        ),
        corrected_reason="late source correction",
    )

    result = runner.invoke(
        app,
        [
            "admin",
            "metric",
            "history",
            "--program",
            "acme",
            "--metric",
            "acme.cluster_count",
            "--format",
            "json",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["observation_count"] == 1
    assert payload["observations"][0]["quality_state"] == "late_corrected"
    assert payload["observations"][0]["is_corrected"] is True
    assert payload["observations"][0]["corrected_reason"] == "late source correction"


def test_admin_metric_validate_command_marks_binding_validated(monkeypatch, tmp_path: Path) -> None:
    metrics_root = tmp_path / "metrics"
    metrics_root.mkdir()
    (metrics_root / "acme.yaml").write_text(
        """
metrics:
  - id: acme.cluster_count
    title: Cluster Count
    unit: count
    aggregation: last
    dimension_columns:
      - cluster
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.commands.metric.METRICS_ROOT", metrics_root)
    monkeypatch.setattr(
        "src.commands.metric._live_metric_binding_probe",
        lambda: (
            lambda _binding: (
                [{"Count": 175.0, "cluster": "eastus2"}],
                (KustoColumn("Count", "long"), KustoColumn("cluster", "string")),
            )
        ),
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="binding-001",
            metric_id="acme.cluster_count",
            program_id="acme",
            source_kind="kusto",
            cluster="https://cluster.kusto.windows.net",
            database="MetricsDb",
            kql_template="Metrics | summarize Count=count() by cluster",
            result_column="Count",
        )
    )

    result = runner.invoke(
        app,
        [
            "admin",
            "metric",
            "validate",
            "--program",
            "acme",
            "--binding-id",
            "binding-001",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    updated = store.get_metric_source_binding("binding-001")

    assert result.exit_code == 0
    assert updated is not None
    assert updated.validated is True
    assert updated.last_validated_at is not None
    assert updated.last_validated_kql_hash is not None


def test_admin_metric_validate_command_reports_schema_mismatch_and_marks_binding_unvalidated(
    monkeypatch,
    tmp_path: Path,
) -> None:
    metrics_root = tmp_path / "metrics"
    metrics_root.mkdir()
    (metrics_root / "acme.yaml").write_text(
        """
metrics:
  - id: acme.cluster_count
    title: Cluster Count
    unit: count
    aggregation: last
    dimension_columns:
      - cluster
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.commands.metric.METRICS_ROOT", metrics_root)
    monkeypatch.setattr(
        "src.commands.metric._live_metric_binding_probe",
        lambda: (
            lambda _binding: (
                [{"WrongCount": 175.0, "cluster": "eastus2"}],
                (KustoColumn("WrongCount", "long"), KustoColumn("cluster", "string")),
            )
        ),
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="binding-001",
            metric_id="acme.cluster_count",
            program_id="acme",
            source_kind="kusto",
            cluster="https://cluster.kusto.windows.net",
            database="MetricsDb",
            kql_template="Metrics | summarize WrongCount=count() by cluster",
            result_column="Count",
            validated=True,
            last_validated_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
        )
    )

    result = runner.invoke(
        app,
        [
            "admin",
            "metric",
            "validate",
            "--program",
            "acme",
            "--binding-id",
            "binding-001",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    updated = store.get_metric_source_binding("binding-001")

    assert result.exit_code == 2
    assert "result_column 'Count' was not returned" in result.output
    assert updated is not None
    assert updated.validated is False
    assert updated.last_validated_at == datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc)
