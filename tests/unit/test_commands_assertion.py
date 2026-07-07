from __future__ import annotations
import pytest
from pathlib import Path
pytestmark = pytest.mark.skipif(not (Path(__file__).resolve().parents[2] / "editions").exists(), reason="Requires private data")

from datetime import date, datetime, timezone
import json
from pathlib import Path

from typer.testing import CliRunner
import yaml

from cli import app
from src.core.hypothesis_models import AssertionEvaluation, AssertionOperator, Hypothesis, HypothesisKind, HypothesisStatus, TelemetryAssertion
from src.core.hypothesis_models import CompositeAssertionOperator
from src.core.metric_models import MetricAggregation, MetricQualityState, MetricSourceBinding, ObservationWindow
from src.core.reality_store import RealityStore


runner = CliRunner()


def test_assertion_add_command_persists_assertion_with_defaults(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "assertion",
            "add",
            "--program",
            "acme",
            "--metric-id",
            "acme.cluster_count",
            "--operator",
            ">=",
            "--threshold",
            "150",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    assertions = store.list_active_telemetry_assertions()

    assert result.exit_code == 0
    assert len(assertions) == 1
    assert assertions[0].metric_id == "acme.cluster_count"
    assert assertions[0].operator.value == ">="
    assert assertions[0].threshold == 150.0
    assert assertions[0].window.days == 7
    assert assertions[0].sustain_min_observations == 3
    assert assertions[0].cooldown_hours == 24
    assert assertions[0].created_by == "vertex/assertion"


def test_assertion_add_command_accepts_explicit_assertion_id(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "assertion",
            "add",
            "--program",
            "acme",
            "--id",
            "assertion-acme-node-availability-7d",
            "--metric-id",
            "acme.node_availability_7d",
            "--operator",
            ">=",
            "--threshold",
            "99.9",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    assertion = store.get_telemetry_assertion("assertion-acme-node-availability-7d")

    assert result.exit_code == 0
    assert assertion is not None
    assert assertion.metric_id == "acme.node_availability_7d"

def test_assertion_composite_add_command_persists_composite_assertion(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    for assertion_id, metric_id, threshold in (
        ("assertion-001", "acme.cluster_count", 150.0),
        ("assertion-002", "acme.capacity_margin", 10.0),
    ):
        store.upsert_telemetry_assertion(
            TelemetryAssertion(
                id=assertion_id,
                program_id="acme",
                metric_id=metric_id,
                window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
                operator=AssertionOperator.GTE,
                threshold=threshold,
            )
        )

    result = runner.invoke(
        app,
        [
            "assertion",
            "composite",
            "add",
            "--program",
            "acme",
            "--id",
            "composite-001",
            "--operator",
            "and",
            "--child-assertion-id",
            "assertion-001",
            "--child-assertion-id",
            "assertion-002",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    composite = store.get_composite_assertion("composite-001")

    assert result.exit_code == 0
    assert composite is not None
    assert composite.operator is CompositeAssertionOperator.AND
    assert composite.child_assertion_ids == ("assertion-001", "assertion-002")


def test_assertion_composite_list_command_renders_json(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_composite_assertion(
        __import__("src.core.hypothesis_models", fromlist=["CompositeAssertion"]).CompositeAssertion(
            id="composite-001",
            program_id="acme",
            operator=CompositeAssertionOperator.OR,
            child_assertion_ids=("assertion-001", "assertion-002"),
        )
    )

    result = runner.invoke(
        app,
        [
            "assertion",
            "composite",
            "list",
            "--program",
            "acme",
            "--format",
            "json",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert payload[0]["id"] == "composite-001"
    assert payload[0]["operator"] == "or"


def test_assertion_add_command_can_reuse_catalog_linked_query_id(tmp_path: Path) -> None:
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
            "assertion",
            "add",
            "--program",
            "acme",
            "--query-id",
            "acme-node-availability-7d",
            "--operator",
            ">=",
            "--threshold",
            "99.9",
            "--programs-root",
            str(programs_root),
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    assertion = store.get_telemetry_assertion("assertion-acme-node-availability-7d")

    assert result.exit_code == 0
    assert assertion is not None
    assert assertion.metric_id == "acme.node_availability_7d"


def test_assertion_add_command_can_infer_threshold_from_metric_definition(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    metrics_root = tmp_path / "metrics"
    metrics_root.mkdir(parents=True)
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
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
    (program_dir / "kpis.yaml").write_text(
        """
schema_version: "1.0"
kpis:
  - id: acme-node-availability-7d
    metric_id: acme.node_availability_7d
    assertion_ids: [assertion-acme-node-availability-7d]
    workstream_ids: [acme]
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
            "assertion",
            "add",
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
    assertion = store.get_telemetry_assertion("assertion-acme-node-availability-7d")

    assert result.exit_code == 0
    assert assertion is not None
    assert assertion.operator is AssertionOperator.GTE
    assert assertion.threshold == 99.9


def test_assertion_add_command_persists_between_operator_with_upper_threshold(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "assertion",
            "add",
            "--program",
            "acme",
            "--metric-id",
            "acme.success_rate",
            "--operator",
            "between",
            "--threshold",
            "95",
            "--threshold-upper",
            "99.5",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    assertions = store.list_active_telemetry_assertions()

    assert result.exit_code == 0
    assert len(assertions) == 1
    assert assertions[0].operator is AssertionOperator.BETWEEN
    assert assertions[0].threshold == 95.0
    assert assertions[0].threshold_upper == 99.5


def test_assertion_add_command_persists_percent_change_baseline(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "assertion",
            "add",
            "--program",
            "acme",
            "--metric-id",
            "acme.cluster_count",
            "--operator",
            "pct_improvement",
            "--threshold",
            "12.5",
            "--baseline-value",
            "100",
            "--baseline-captured-at",
            "2026-05-20T10:00:00+00:00",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    assertions = store.list_active_telemetry_assertions()

    assert result.exit_code == 0
    assert len(assertions) == 1
    assert assertions[0].operator is AssertionOperator.PCT_IMPROVEMENT
    assert assertions[0].baseline_value == 100.0
    assert assertions[0].baseline_captured_at == datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc)


def test_assertion_add_command_persists_forecast_operator(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "assertion",
            "add",
            "--program",
            "acme",
            "--metric-id",
            "acme.cluster_count",
            "--operator",
            "forecast_gte",
            "--threshold",
            "120",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    assertions = store.list_active_telemetry_assertions()

    assert result.exit_code == 0
    assert len(assertions) == 1
    assert assertions[0].operator is AssertionOperator.FORECAST_GTE
    assert assertions[0].threshold == 120.0


def test_assertion_add_command_persists_burn_rate_operator(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "assertion",
            "add",
            "--program",
            "acme",
            "--metric-id",
            "acme.backlog_count",
            "--operator",
            "burn_rate_gte",
            "--threshold",
            "20",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    assertions = store.list_active_telemetry_assertions()

    assert result.exit_code == 0
    assert len(assertions) == 1
    assert assertions[0].operator is AssertionOperator.BURN_RATE_GTE
    assert assertions[0].threshold == 20.0


def test_assertion_add_command_rejects_percent_change_without_baseline(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "assertion",
            "add",
            "--program",
            "acme",
            "--metric-id",
            "acme.cluster_count",
            "--operator",
            "pct_regression",
            "--threshold",
            "5",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    assert result.exit_code != 0

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    assert store.list_active_telemetry_assertions() == ()


def test_assertion_list_command_returns_active_assertions_as_json(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_telemetry_assertion(
        TelemetryAssertion(
            id="assertion-001",
            program_id="acme",
            metric_id="acme.cluster_count",
            window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
            operator=AssertionOperator.GTE,
            threshold=150.0,
        )
    )

    result = runner.invoke(
        app,
        [
            "assertion",
            "list",
            "--program",
            "acme",
            "--format",
            "json",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload[0]["id"] == "assertion-001"
    assert payload[0]["metric_id"] == "acme.cluster_count"
    assert payload[0]["policy_version"] == 1


def test_admin_assertion_list_command_alias_returns_active_assertions_as_json(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_telemetry_assertion(
        TelemetryAssertion(
            id="assertion-001",
            program_id="acme",
            metric_id="acme.cluster_count",
            window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
            operator=AssertionOperator.GTE,
            threshold=150.0,
            created_by="vertex/assertion",
        )
    )

    result = runner.invoke(
        app,
        [
            "admin",
            "assertion",
            "list",
            "--program",
            "acme",
            "--format",
            "json",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload[0]["id"] == "assertion-001"
    assert payload[0]["metric_id"] == "acme.cluster_count"


def test_assertion_update_command_versions_assertion_and_repoints_linked_hypothesis(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_telemetry_assertion(
        TelemetryAssertion(
            id="assertion-001",
            program_id="acme",
            metric_id="acme.cluster_count",
            window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
            operator=AssertionOperator.GTE,
            threshold=150.0,
            tolerance_rel=0.10,
            sustain_min_observations=3,
            cooldown_hours=24,
            description="Original threshold",
            linked_hypothesis_id="hyp-001",
            created_by="vertex/assertion",
        )
    )
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-001",
            short_id="H-001",
            program_id="acme",
            kind=HypothesisKind.SCALAR_FACT,
            statement="Cluster count stays above 150.",
            expected_value=150.0,
            as_of_date=date(2026, 5, 20),
            telemetry_assertion_id="assertion-001",
            proposed_by="pm",
            proposed_at=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            status=HypothesisStatus.CONFIRMED,
            confirmed_by="pm",
            confirmed_at=datetime(2026, 5, 20, 9, 5, tzinfo=timezone.utc),
        )
    )

    result = runner.invoke(
        app,
        [
            "assertion",
            "update",
            "--program",
            "acme",
            "--id",
            "assertion-001",
            "--threshold",
            "175",
            "--cooldown-hours",
            "48",
            "--description",
            "Adjusted threshold",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    archived = store.get_telemetry_assertion("assertion-001")
    assertions = store.list_active_telemetry_assertions()
    hypothesis = store.get_hypothesis("hyp-001")

    assert result.exit_code == 0
    assert archived is not None
    assert archived.threshold == 150.0
    assert archived.valid_until is not None
    assert len(assertions) == 1
    assert assertions[0].id != "assertion-001"
    assert assertions[0].threshold == 175.0
    assert assertions[0].cooldown_hours == 48
    assert assertions[0].description == "Adjusted threshold"
    assert assertions[0].policy_version == 2
    assert hypothesis is not None
    assert hypothesis.telemetry_assertion_id == assertions[0].id


def test_assertion_update_command_can_promote_assertion_to_between_range(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_telemetry_assertion(
        TelemetryAssertion(
            id="assertion-001",
            program_id="acme",
            metric_id="acme.success_rate",
            window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
            operator=AssertionOperator.GTE,
            threshold=95.0,
        )
    )

    result = runner.invoke(
        app,
        [
            "assertion",
            "update",
            "--program",
            "acme",
            "--id",
            "assertion-001",
            "--operator",
            "between",
            "--threshold",
            "95",
            "--threshold-upper",
            "99.5",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    assertions = store.list_active_telemetry_assertions()

    assert result.exit_code == 0
    assert len(assertions) == 1
    assert assertions[0].operator is AssertionOperator.BETWEEN
    assert assertions[0].threshold == 95.0
    assert assertions[0].threshold_upper == 99.5


def test_assertion_update_command_can_promote_assertion_to_percent_change_with_baseline(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_telemetry_assertion(
        TelemetryAssertion(
            id="assertion-001",
            program_id="acme",
            metric_id="acme.cluster_count",
            window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
            operator=AssertionOperator.GTE,
            threshold=150.0,
        )
    )

    result = runner.invoke(
        app,
        [
            "assertion",
            "update",
            "--program",
            "acme",
            "--id",
            "assertion-001",
            "--operator",
            "pct_improvement",
            "--threshold",
            "10",
            "--baseline-value",
            "140",
            "--baseline-captured-at",
            "2026-05-20T09:00:00+00:00",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    assertions = store.list_active_telemetry_assertions()

    assert result.exit_code == 0
    assert len(assertions) == 1
    assert assertions[0].operator is AssertionOperator.PCT_IMPROVEMENT
    assert assertions[0].threshold == 10.0
    assert assertions[0].baseline_value == 140.0
    assert assertions[0].baseline_captured_at == datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)


def test_assertion_update_command_can_promote_assertion_to_forecast_operator(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_telemetry_assertion(
        TelemetryAssertion(
            id="assertion-001",
            program_id="acme",
            metric_id="acme.cluster_count",
            window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
            operator=AssertionOperator.GTE,
            threshold=150.0,
        )
    )

    result = runner.invoke(
        app,
        [
            "assertion",
            "update",
            "--program",
            "acme",
            "--id",
            "assertion-001",
            "--operator",
            "forecast_gte",
            "--threshold",
            "130",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    assertions = store.list_active_telemetry_assertions()

    assert result.exit_code == 0
    assert len(assertions) == 1
    assert assertions[0].operator is AssertionOperator.FORECAST_GTE
    assert assertions[0].threshold == 130.0


def test_assertion_update_command_can_promote_assertion_to_burn_rate_operator(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_telemetry_assertion(
        TelemetryAssertion(
            id="assertion-001",
            program_id="acme",
            metric_id="acme.backlog_count",
            window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
            operator=AssertionOperator.GTE,
            threshold=150.0,
        )
    )

    result = runner.invoke(
        app,
        [
            "assertion",
            "update",
            "--program",
            "acme",
            "--id",
            "assertion-001",
            "--operator",
            "burn_rate_gte",
            "--threshold",
            "18",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    assertions = store.list_active_telemetry_assertions()

    assert result.exit_code == 0
    assert len(assertions) == 1
    assert assertions[0].operator is AssertionOperator.BURN_RATE_GTE
    assert assertions[0].threshold == 18.0


def test_assertion_add_evidence_url_command_updates_binding_template(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="binding-001",
            metric_id="acme.cluster_count",
            program_id="acme",
            source_kind="kusto",
            kql_template="Metrics | summarize count()",
            result_column="Count",
        )
    )

    result = runner.invoke(
        app,
        [
            "assertion",
            "add-evidence-url",
            "--program",
            "acme",
            "--binding-id",
            "binding-001",
            "--template",
            "https://kusto/{metric_id}/{binding_id}",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    binding = store.get_metric_source_binding("binding-001")

    assert result.exit_code == 0
    assert binding is not None
    assert binding.evidence_url_template == "https://kusto/{metric_id}/{binding_id}"


def test_assertion_add_evidence_url_command_rejects_unknown_tokens(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="binding-001",
            metric_id="acme.cluster_count",
            program_id="acme",
            source_kind="kusto",
            kql_template="Metrics | summarize count()",
            result_column="Count",
        )
    )

    result = runner.invoke(
        app,
        [
            "assertion",
            "add-evidence-url",
            "--program",
            "acme",
            "--binding-id",
            "binding-001",
            "--template",
            "https://kusto/{metric_id}/{unknown_token}",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    binding = store.get_metric_source_binding("binding-001")

    assert result.exit_code != 0
    assert binding is not None
    assert binding.evidence_url_template is None


def test_assertion_export_command_writes_snapshot_artifact(tmp_path: Path) -> None:
    db_root = tmp_path / "db"
    programs_root = tmp_path / "programs"
    store = RealityStore("acme", db_root=db_root)
    store.initialize()
    store.upsert_telemetry_assertion(
        TelemetryAssertion(
            id="assertion-001",
            program_id="acme",
            metric_id="acme.cluster_count",
            window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
            operator=AssertionOperator.GTE,
            threshold=150.0,
        )
    )

    result = runner.invoke(
        app,
        [
            "assertion",
            "export",
            "--program",
            "acme",
            "--db-root",
            str(db_root),
            "--programs-root",
            str(programs_root),
        ],
    )

    export_path = programs_root / "acme" / "reality" / "assertions.snapshot.yaml"

    assert result.exit_code == 0
    assert export_path.exists()
    exported = export_path.read_text(encoding="utf-8")
    assert "READ-ONLY" in exported
    assert "metric_id: acme.cluster_count" in exported


def test_assertion_add_command_rejects_between_without_upper_threshold(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "assertion",
            "add",
            "--program",
            "acme",
            "--metric-id",
            "acme.success_rate",
            "--operator",
            "between",
            "--threshold",
            "95",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    assert result.exit_code != 0
    assert "--threshold-upper is required when --operator is between" in result.output


def test_assertion_history_command_returns_versioned_rows_and_evaluations(tmp_path: Path) -> None:
    db_root = tmp_path / "db"
    store = RealityStore("acme", db_root=db_root)
    store.initialize()
    store.upsert_telemetry_assertion(
        TelemetryAssertion(
            id="assertion-001",
            program_id="acme",
            metric_id="acme.cluster_count",
            window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
            operator=AssertionOperator.GTE,
            threshold=150.0,
            linked_hypothesis_id="hyp-001",
        )
    )
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-001",
            short_id="H-001",
            program_id="acme",
            kind=HypothesisKind.SCALAR_FACT,
            statement="Cluster count stays above threshold.",
            expected_value=150.0,
            as_of_date=date(2026, 5, 21),
            telemetry_assertion_id="assertion-001",
            proposed_by="pm",
            proposed_at=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
            status=HypothesisStatus.CONFIRMED,
            confirmed_by="pm",
            confirmed_at=datetime(2026, 5, 21, 9, 5, tzinfo=timezone.utc),
        )
    )
    store.append_assertion_evaluation(
        AssertionEvaluation(
            id="eval-001",
            program_id="acme",
            hypothesis_id="hyp-001",
            assertion_id="assertion-001",
            observation_id=None,
            evaluated_at=datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc),
            violated=False,
            value_num=160.0,
            expected_value=150.0,
            quality_state=MetricQualityState.OK,
        )
    )

    update_result = runner.invoke(
        app,
        [
            "assertion",
            "update",
            "--program",
            "acme",
            "--id",
            "assertion-001",
            "--threshold",
            "175",
            "--db-root",
            str(db_root),
        ],
    )

    active_assertion = store.list_active_telemetry_assertions()[0]
    store.append_assertion_evaluation(
        AssertionEvaluation(
            id="eval-002",
            program_id="acme",
            hypothesis_id="hyp-001",
            assertion_id=active_assertion.id,
            observation_id=None,
            evaluated_at=datetime(2026, 5, 21, 11, 0, tzinfo=timezone.utc),
            violated=True,
            value_num=170.0,
            expected_value=175.0,
            quality_state=MetricQualityState.OK,
            note="threshold breach",
        )
    )

    result = runner.invoke(
        app,
        [
            "assertion",
            "history",
            "--program",
            "acme",
            "--id",
            active_assertion.id,
            "--format",
            "json",
            "--db-root",
            str(db_root),
        ],
    )

    payload = json.loads(result.stdout)

    assert update_result.exit_code == 0
    assert result.exit_code == 0
    assert payload["scope_assertion_id"] == active_assertion.id
    assert [entry["id"] for entry in payload["assertions"]] == [active_assertion.id, "assertion-001"]
    assert payload["assertions"][0]["status"] == "active"
    assert payload["assertions"][0]["evaluation_count"] == 1
    assert payload["assertions"][0]["evaluations"][0]["id"] == "eval-002"
    assert payload["assertions"][1]["status"] == "archived"
    assert payload["assertions"][1]["evaluations"][0]["id"] == "eval-001"


def test_admin_assertion_history_command_alias_returns_metric_history_as_json(tmp_path: Path) -> None:
    db_root = tmp_path / "db"
    store = RealityStore("acme", db_root=db_root)
    store.initialize()
    store.upsert_telemetry_assertion(
        TelemetryAssertion(
            id="assertion-001",
            program_id="acme",
            metric_id="acme.cluster_count",
            window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
            operator=AssertionOperator.GTE,
            threshold=150.0,
        )
    )

    result = runner.invoke(
        app,
        [
            "admin",
            "assertion",
            "history",
            "--program",
            "acme",
            "--metric-id",
            "acme.cluster_count",
            "--format",
            "json",
            "--db-root",
            str(db_root),
        ],
    )

    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["metric_id"] == "acme.cluster_count"
    assert payload["assertions"][0]["id"] == "assertion-001"


def test_assertion_export_command_include_history_writes_versioned_rows_and_evaluations(tmp_path: Path) -> None:
    db_root = tmp_path / "db"
    programs_root = tmp_path / "programs"
    store = RealityStore("acme", db_root=db_root)
    store.initialize()
    archived_at = datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc)
    store.upsert_telemetry_assertion(
        TelemetryAssertion(
            id="assertion-001",
            program_id="acme",
            metric_id="acme.cluster_count",
            window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
            operator=AssertionOperator.GTE,
            threshold=150.0,
            valid_until=archived_at,
        )
    )
    store.upsert_telemetry_assertion(
        TelemetryAssertion(
            id="assertion-002",
            program_id="acme",
            metric_id="acme.cluster_count",
            window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
            operator=AssertionOperator.GTE,
            threshold=175.0,
            policy_version=2,
            valid_from=archived_at,
        )
    )
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-001",
            short_id="H-001",
            program_id="acme",
            kind=HypothesisKind.SCALAR_FACT,
            statement="Cluster count stays above threshold.",
            expected_value=150.0,
            as_of_date=date(2026, 5, 21),
            telemetry_assertion_id="assertion-002",
            proposed_by="pm",
            proposed_at=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
            status=HypothesisStatus.CONFIRMED,
            confirmed_by="pm",
            confirmed_at=datetime(2026, 5, 21, 9, 5, tzinfo=timezone.utc),
        )
    )
    store.append_assertion_evaluation(
        AssertionEvaluation(
            id="eval-001",
            program_id="acme",
            hypothesis_id="hyp-001",
            assertion_id="assertion-001",
            observation_id=None,
            evaluated_at=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
            violated=False,
            value_num=160.0,
            expected_value=150.0,
            quality_state=MetricQualityState.OK,
        )
    )

    result = runner.invoke(
        app,
        [
            "assertion",
            "export",
            "--program",
            "acme",
            "--include-history",
            "--db-root",
            str(db_root),
            "--programs-root",
            str(programs_root),
        ],
    )

    export_path = programs_root / "acme" / "reality" / "assertions.snapshot.yaml"
    document = yaml.safe_load(export_path.read_text(encoding="utf-8"))

    assert result.exit_code == 0
    assert document["assertions"][0]["id"] == "assertion-002"
    assert [entry["id"] for entry in document["assertion_history"]] == ["assertion-002", "assertion-001"]
    assert document["assertion_history"][1]["evaluations"][0]["id"] == "eval-001"