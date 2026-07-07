from __future__ import annotations
import pytest
from pathlib import Path
pytestmark = pytest.mark.skipif(not (Path(__file__).resolve().parents[2] / "editions").exists(), reason="Requires private data")

from datetime import date, datetime, timezone
from pathlib import Path

from typer.testing import CliRunner
import yaml

from cli import app
from src.core.hypothesis_models import AssertionOperator, Hypothesis, HypothesisKind, HypothesisStatus, TelemetryAssertion
from src.core.metric_models import MetricAggregation, MetricSourceBinding, ObservationWindow
from src.core.reality_store import RealityStore


runner = CliRunner()


def test_hypothesis_quickstart_creates_linked_assertion_and_proposal(tmp_path: Path) -> None:
    metrics_root = tmp_path / "knowledge" / "metrics"
    metrics_root.mkdir(parents=True)
    (metrics_root / "acme.yaml").write_text(
        """
metrics:
  - id: acme.availability
    title: Acme availability
    unit: percent
    aggregation: last
""".strip()
        + "\n",
        encoding="utf-8",
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="binding-001",
            metric_id="acme.availability",
            program_id="acme",
            source_kind="kusto",
            validated=True,
        )
    )

    result = runner.invoke(
        app,
        [
            "hypothesis",
            "quickstart",
            "--program",
            "acme",
            "--metric-id",
            "acme.availability",
            "--binding-id",
            "binding-001",
            "--operator",
            ">=",
            "--threshold",
            "99.9",
            "--proposed-by",
            "lead.pm",
            "--db-root",
            str(tmp_path / "db"),
            "--metrics-root",
            str(metrics_root),
        ],
    )

    hypotheses = store.list_hypotheses(statuses=(HypothesisStatus.PROPOSED,))
    assertions = store.list_active_telemetry_assertions()

    assert result.exit_code == 0
    assert "Quickstart created proposed hypothesis H-001 with assertion" in result.stdout
    assert len(hypotheses) == 1
    assert len(assertions) == 1

    hypothesis = hypotheses[0]
    assertion = assertions[0]

    assert hypothesis.kind is HypothesisKind.SCALAR_FACT
    assert hypothesis.telemetry_assertion_id == assertion.id
    assert hypothesis.expected_value == 99.9
    assert hypothesis.statement == "Acme availability should stay at or above 99.9."
    assert hypothesis.proposed_by == "lead.pm"
    assert assertion.operator is AssertionOperator.GTE
    assert assertion.threshold == 99.9
    assert assertion.linked_hypothesis_id == hypothesis.id
    assert assertion.description == hypothesis.statement


def test_hypothesis_quickstart_rejects_duplicate_active_metric_hypothesis(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="binding-001",
            metric_id="acme.availability",
            program_id="acme",
            source_kind="kusto",
        )
    )
    store.upsert_telemetry_assertion(
        TelemetryAssertion(
            id="assertion-001",
            program_id="acme",
            metric_id="acme.availability",
            window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
            operator=AssertionOperator.GTE,
            threshold=99.9,
        )
    )
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-001",
            short_id="H-001",
            program_id="acme",
            kind=HypothesisKind.SCALAR_FACT,
            statement="Acme availability should stay at or above 99.9.",
            expected_value=99.9,
            as_of_date=date(2026, 5, 21),
            telemetry_assertion_id="assertion-001",
            proposed_by="lead.pm",
            proposed_at=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
            status=HypothesisStatus.PROPOSED,
        )
    )

    result = runner.invoke(
        app,
        [
            "hypothesis",
            "quickstart",
            "--program",
            "acme",
            "--metric-id",
            "acme.availability",
            "--binding-id",
            "binding-001",
            "--operator",
            ">=",
            "--threshold",
            "99.9",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    assert result.exit_code != 0
    assert "Metric acme.availability already has active hypothesis H-001." in result.output


def test_hypothesis_quickstart_rejects_binding_metric_mismatch(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="binding-001",
            metric_id="acme.latency",
            program_id="acme",
            source_kind="kusto",
        )
    )

    result = runner.invoke(
        app,
        [
            "hypothesis",
            "quickstart",
            "--program",
            "acme",
            "--metric-id",
            "acme.availability",
            "--binding-id",
            "binding-001",
            "--operator",
            ">=",
            "--threshold",
            "99.9",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    assert result.exit_code != 0
    assert "acme.latency" in result.output
    assert "acme.availability" in result.output


def test_hypothesis_quickstart_creates_binding_inline_when_missing(tmp_path: Path) -> None:
    metrics_root = tmp_path / "knowledge" / "metrics"
    metrics_root.mkdir(parents=True)
    (metrics_root / "acme.yaml").write_text(
        """
metrics:
  - id: acme.availability
    title: Acme availability
    unit: percent
    aggregation: last
""".strip()
        + "\n",
        encoding="utf-8",
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()

    result = runner.invoke(
        app,
        [
            "hypothesis",
            "quickstart",
            "--program",
            "acme",
            "--metric-id",
            "acme.availability",
            "--binding-id",
            "binding-001",
            "--cluster",
            "https://cluster.kusto.windows.net",
            "--database",
            "VertexMetrics",
            "--kql-template",
            "Availability | summarize latest_value=max(availability)",
            "--result-column",
            "latest_value",
            "--operator",
            ">=",
            "--threshold",
            "99.9",
            "--proposed-by",
            "lead.pm",
            "--db-root",
            str(tmp_path / "db"),
            "--metrics-root",
            str(metrics_root),
        ],
    )

    binding = store.get_metric_source_binding("binding-001")
    hypotheses = store.list_hypotheses(statuses=(HypothesisStatus.PROPOSED,))

    assert result.exit_code == 0
    assert "Quickstart created binding binding-001 for metric acme.availability" in result.stdout
    assert "vertex admin metric validate --program acme --binding-id binding-001" in result.stdout
    assert binding is not None
    assert binding.metric_id == "acme.availability"
    assert binding.cluster == "https://cluster.kusto.windows.net"
    assert binding.database == "VertexMetrics"
    assert binding.kql_template == "Availability | summarize latest_value=max(availability)"
    assert binding.result_column == "latest_value"
    assert binding.validated is False
    assert len(hypotheses) == 1


def test_hypothesis_quickstart_reuses_sole_active_binding_when_binding_id_omitted(tmp_path: Path) -> None:
    metrics_root = tmp_path / "knowledge" / "metrics"
    metrics_root.mkdir(parents=True)
    (metrics_root / "acme.yaml").write_text(
        """
metrics:
  - id: acme.availability
    title: Acme availability
    unit: percent
    aggregation: last
""".strip()
        + "\n",
        encoding="utf-8",
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="custom-binding",
            metric_id="acme.availability",
            program_id="acme",
            source_kind="kusto",
            validated=True,
        )
    )

    result = runner.invoke(
        app,
        [
            "hypothesis",
            "quickstart",
            "--program",
            "acme",
            "--metric-id",
            "acme.availability",
            "--operator",
            ">=",
            "--threshold",
            "99.9",
            "--db-root",
            str(tmp_path / "db"),
            "--metrics-root",
            str(metrics_root),
        ],
    )

    bindings = store.list_active_metric_source_bindings(metric_id="acme.availability")
    hypotheses = store.list_hypotheses(statuses=(HypothesisStatus.PROPOSED,))

    assert result.exit_code == 0
    assert "Quickstart created binding" not in result.stdout
    assert len(bindings) == 1
    assert bindings[0].binding_id == "custom-binding"
    assert len(hypotheses) == 1


def test_hypothesis_quickstart_requires_binding_inputs_for_missing_binding(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "hypothesis",
            "quickstart",
            "--program",
            "acme",
            "--metric-id",
            "acme.availability",
            "--binding-id",
            "binding-001",
            "--operator",
            ">=",
            "--threshold",
            "99.9",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    assert result.exit_code != 0
    assert "Provide" in result.output
    assert "--cluster" in result.output
    assert "--database" in result.output
    assert "--kql-template" in result.output
    assert "--result-column" in result.output


def test_hypothesis_quickstart_requires_binding_id_when_metric_has_multiple_active_bindings(tmp_path: Path) -> None:
    metrics_root = tmp_path / "knowledge" / "metrics"
    metrics_root.mkdir(parents=True)
    (metrics_root / "acme.yaml").write_text(
        """
metrics:
  - id: acme.availability
    title: Acme availability
    unit: percent
    aggregation: last
""".strip()
        + "\n",
        encoding="utf-8",
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="binding-001",
            metric_id="acme.availability",
            program_id="acme",
            source_kind="kusto",
            validated=True,
        )
    )
    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="binding-002",
            metric_id="acme.availability",
            program_id="acme",
            source_kind="kusto",
            validated=True,
            priority=1,
        )
    )

    result = runner.invoke(
        app,
        [
            "hypothesis",
            "quickstart",
            "--program",
            "acme",
            "--metric-id",
            "acme.availability",
            "--operator",
            ">=",
            "--threshold",
            "99.9",
            "--db-root",
            str(tmp_path / "db"),
            "--metrics-root",
            str(metrics_root),
        ],
    )

    assert result.exit_code != 0
    assert "--binding-id is required" in result.output
    assert "multiple active bindings" in result.output
    assert "binding-001" in result.output
    assert "binding-002" in result.output


def test_hypothesis_quickstart_requires_metric_definition_before_inline_binding_create(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "hypothesis",
            "quickstart",
            "--program",
            "acme",
            "--metric-id",
            "acme.availability",
            "--binding-id",
            "binding-001",
            "--cluster",
            "https://cluster.kusto.windows.net",
            "--database",
            "VertexMetrics",
            "--kql-template",
            "Availability | summarize latest_value=max(availability)",
            "--result-column",
            "latest_value",
            "--unit",
            "percent",
            "--operator",
            ">=",
            "--threshold",
            "99.9",
            "--db-root",
            str(tmp_path / "db"),
            "--metrics-root",
            str(tmp_path / "knowledge" / "metrics"),
        ],
    )

    assert result.exit_code == 0
    assert "Quickstart created metric definition acme.availability" in result.stdout


def test_hypothesis_quickstart_creates_metric_definition_inline_when_missing(tmp_path: Path) -> None:
    metrics_root = tmp_path / "knowledge" / "metrics"
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()

    result = runner.invoke(
        app,
        [
            "hypothesis",
            "quickstart",
            "--program",
            "acme",
            "--metric-id",
            "acme.availability",
            "--binding-id",
            "binding-001",
            "--cluster",
            "https://cluster.kusto.windows.net",
            "--database",
            "VertexMetrics",
            "--kql-template",
            "Availability | summarize latest_value=max(availability)",
            "--result-column",
            "latest_value",
            "--metric-title",
            "Acme availability",
            "--unit",
            "percent",
            "--operator",
            ">=",
            "--threshold",
            "99.9",
            "--proposed-by",
            "lead.pm",
            "--db-root",
            str(tmp_path / "db"),
            "--metrics-root",
            str(metrics_root),
        ],
    )

    metric_file = metrics_root / "acme.yaml"
    metric_doc = yaml.safe_load(metric_file.read_text(encoding="utf-8"))
    binding = store.get_metric_source_binding("binding-001")

    assert result.exit_code == 0
    assert "Quickstart created metric definition acme.availability" in result.stdout
    assert metric_file.exists()
    assert metric_doc["metrics"][0]["id"] == "acme.availability"
    assert metric_doc["metrics"][0]["title"] == "Acme availability"
    assert metric_doc["metrics"][0]["unit"] == "percent"
    assert metric_doc["metrics"][0]["aggregation"] == "last"
    assert binding is not None
    assert binding.metric_id == "acme.availability"


def test_hypothesis_quickstart_derives_binding_id_when_omitted(tmp_path: Path) -> None:
    metrics_root = tmp_path / "knowledge" / "metrics"
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()

    result = runner.invoke(
        app,
        [
            "hypothesis",
            "quickstart",
            "--program",
            "acme",
            "--metric-id",
            "acme.availability",
            "--cluster",
            "https://cluster.kusto.windows.net",
            "--database",
            "VertexMetrics",
            "--kql-template",
            "Availability | summarize latest_value=max(availability)",
            "--result-column",
            "latest_value",
            "--unit",
            "percent",
            "--operator",
            ">=",
            "--threshold",
            "99.9",
            "--db-root",
            str(tmp_path / "db"),
            "--metrics-root",
            str(metrics_root),
        ],
    )

    binding = store.get_metric_source_binding("acme-availability-binding")

    assert result.exit_code == 0
    assert "Quickstart created binding acme-availability-binding for metric acme.availability" in result.stdout
    assert binding is not None
    assert binding.metric_id == "acme.availability"


def test_hypothesis_quickstart_reuses_query_catalog_binding_inputs(tmp_path: Path) -> None:
    metrics_root = tmp_path / "knowledge" / "metrics"
    metrics_root.mkdir(parents=True)
    (metrics_root / "acme.yaml").write_text(
        """
metrics:
  - id: acme.availability
    title: Acme availability
    unit: percent
    aggregation: last
""".strip()
        + "\n",
        encoding="utf-8",
    )

    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
kpis:
  - id: availability-latest
    metric_id: acme.availability
    cluster: https://cluster.kusto.windows.net
    database: VertexMetrics
    kql: Availability | summarize latest_value=max(availability)
    result_column: latest_value
""".strip()
        + "\n",
        encoding="utf-8",
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()

    result = runner.invoke(
        app,
        [
            "hypothesis",
            "quickstart",
            "--program",
            "acme",
            "--metric-id",
            "acme.availability",
            "--binding-id",
            "binding-001",
            "--query-id",
            "availability-latest",
            "--operator",
            ">=",
            "--threshold",
            "99.9",
            "--db-root",
            str(tmp_path / "db"),
            "--metrics-root",
            str(metrics_root),
            "--programs-root",
            str(programs_root),
        ],
    )

    binding = store.get_metric_source_binding("binding-001")

    assert result.exit_code == 0
    assert binding is not None
    assert binding.cluster == "https://cluster.kusto.windows.net"
    assert binding.database == "VertexMetrics"
    assert binding.kql_template == "Availability | summarize latest_value=max(availability)"
    assert binding.result_column == "latest_value"


def test_hypothesis_quickstart_derives_metric_and_binding_from_query_catalog(tmp_path: Path) -> None:
    metrics_root = tmp_path / "knowledge" / "metrics"
    metrics_root.mkdir(parents=True)
    (metrics_root / "acme.yaml").write_text(
        """
metrics:
  - id: acme.availability
    title: Acme availability
    unit: percent
    aggregation: last
""".strip()
        + "\n",
        encoding="utf-8",
    )

    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
kpis:
  - id: availability-latest
    metric_id: acme.availability
    cluster: https://cluster.kusto.windows.net
    database: VertexMetrics
    kql: Availability | summarize latest_value=max(availability)
    result_column: latest_value
""".strip()
        + "\n",
        encoding="utf-8",
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()

    result = runner.invoke(
        app,
        [
            "hypothesis",
            "quickstart",
            "--program",
            "acme",
            "--query-id",
            "availability-latest",
            "--operator",
            ">=",
            "--threshold",
            "99.9",
            "--db-root",
            str(tmp_path / "db"),
            "--metrics-root",
            str(metrics_root),
            "--programs-root",
            str(programs_root),
        ],
    )

    binding = store.get_metric_source_binding("acme-availability-binding")
    hypotheses = store.list_hypotheses(statuses=(HypothesisStatus.PROPOSED,))

    assert result.exit_code == 0
    assert "Quickstart created binding acme-availability-binding for metric acme.availability" in result.stdout
    assert binding is not None
    assert binding.metric_id == "acme.availability"
    assert len(hypotheses) == 1
    assert hypotheses[0].statement == "Acme availability should stay at or above 99.9."


def test_hypothesis_quickstart_requires_metric_id_when_query_catalog_entry_has_none(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
kpis:
  - id: availability-latest
    cluster: https://cluster.kusto.windows.net
    database: VertexMetrics
    kql: Availability | summarize latest_value=max(availability)
    result_column: latest_value
""".strip()
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "hypothesis",
            "quickstart",
            "--program",
            "acme",
            "--query-id",
            "availability-latest",
            "--operator",
            ">=",
            "--threshold",
            "99.9",
            "--db-root",
            str(tmp_path / "db"),
            "--metrics-root",
            str(tmp_path / "knowledge" / "metrics"),
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code != 0
    assert "Provide --metric-id or use --query-id" in result.output
    assert "declares metric_id or exactly one active assertion_id" in result.output


def test_hypothesis_quickstart_reuses_query_catalog_assertion_policy_when_operator_and_threshold_omitted(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
kpis:
  - id: availability-latest
    assertion_ids: [assertion-001]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="acme-availability-binding",
            metric_id="acme.availability",
            program_id="acme",
            source_kind="kusto",
            validated=True,
        )
    )
    store.upsert_telemetry_assertion(
        TelemetryAssertion(
            id="assertion-001",
            program_id="acme",
            metric_id="acme.availability",
            window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
            operator=AssertionOperator.GTE,
            threshold=99.9,
            description="Availability should stay at or above 99.9%.",
        )
    )

    result = runner.invoke(
        app,
        [
            "hypothesis",
            "quickstart",
            "--program",
            "acme",
            "--query-id",
            "availability-latest",
            "--db-root",
            str(tmp_path / "db"),
            "--programs-root",
            str(programs_root),
        ],
    )

    hypotheses = store.list_hypotheses(statuses=(HypothesisStatus.PROPOSED,))
    assertions = store.list_active_telemetry_assertions()

    assert result.exit_code == 0
    assert "Quickstart created proposed hypothesis H-001 with assertion assertion-001 for metric acme.availability." in result.stdout
    assert len(hypotheses) == 1
    assert len(assertions) == 1
    assert hypotheses[0].telemetry_assertion_id == "assertion-001"
    assert hypotheses[0].expected_value == 99.9
    assert hypotheses[0].statement == "Availability should stay at or above 99.9%."
    assert assertions[0].linked_hypothesis_id == hypotheses[0].id


def test_hypothesis_quickstart_rejects_omitted_operator_and_threshold_when_query_assertions_are_ambiguous(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
kpis:
  - id: availability-latest
    assertion_ids: [assertion-001, assertion-002]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_telemetry_assertion(
        TelemetryAssertion(
            id="assertion-001",
            program_id="acme",
            metric_id="acme.availability",
            window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
            operator=AssertionOperator.GTE,
            threshold=99.9,
        )
    )
    store.upsert_telemetry_assertion(
        TelemetryAssertion(
            id="assertion-002",
            program_id="acme",
            metric_id="acme.availability",
            window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
            operator=AssertionOperator.LTE,
            threshold=99.5,
        )
    )

    result = runner.invoke(
        app,
        [
            "hypothesis",
            "quickstart",
            "--program",
            "acme",
            "--query-id",
            "availability-latest",
            "--db-root",
            str(tmp_path / "db"),
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code != 0
    assert "Provide both --operator and --threshold" in result.output
    assert "exactly one active assertion_id" in result.output


def test_hypothesis_quickstart_persists_percent_change_baseline(tmp_path: Path) -> None:
    metrics_root = tmp_path / "knowledge" / "metrics"
    metrics_root.mkdir(parents=True)
    (metrics_root / "acme.yaml").write_text(
        """
metrics:
  - id: acme.availability
    title: Acme availability
    unit: percent
    aggregation: last
""".strip()
        + "\n",
        encoding="utf-8",
    )

    programs_root = tmp_path / "programs"
    (programs_root / "acme").mkdir(parents=True)

    result = runner.invoke(
        app,
        [
            "hypothesis",
            "quickstart",
            "--program",
            "acme",
            "--metric-id",
            "acme.availability",
            "--binding-id",
            "binding-001",
            "--cluster",
            "https://cluster.kusto.windows.net",
            "--database",
            "VertexMetrics",
            "--kql-template",
            "Availability | summarize latest_value=max(availability)",
            "--result-column",
            "latest_value",
            "--operator",
            "pct_improvement",
            "--threshold",
            "5",
            "--baseline-value",
            "95",
            "--baseline-captured-at",
            "2026-05-20T09:00:00+00:00",
            "--db-root",
            str(tmp_path / "db"),
            "--metrics-root",
            str(metrics_root),
            "--programs-root",
            str(programs_root),
        ],
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    assertions = store.list_active_telemetry_assertions()

    assert result.exit_code == 0
    assert len(assertions) == 1
    assert assertions[0].operator is AssertionOperator.PCT_IMPROVEMENT
    assert assertions[0].baseline_value == 95.0
    assert assertions[0].baseline_captured_at == datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)


def test_hypothesis_quickstart_persists_forecast_operator(tmp_path: Path) -> None:
    metrics_root = tmp_path / "knowledge" / "metrics"
    metrics_root.mkdir(parents=True)
    (metrics_root / "acme.yaml").write_text(
        """
metrics:
  - id: acme.availability
    title: Acme availability
    unit: percent
    aggregation: last
""".strip()
        + "\n",
        encoding="utf-8",
    )

    programs_root = tmp_path / "programs"
    (programs_root / "acme").mkdir(parents=True)

    result = runner.invoke(
        app,
        [
            "hypothesis",
            "quickstart",
            "--program",
            "acme",
            "--metric-id",
            "acme.availability",
            "--binding-id",
            "binding-001",
            "--cluster",
            "https://cluster.kusto.windows.net",
            "--database",
            "VertexMetrics",
            "--kql-template",
            "Availability | summarize latest_value=max(availability)",
            "--result-column",
            "latest_value",
            "--operator",
            "forecast_gte",
            "--threshold",
            "97",
            "--db-root",
            str(tmp_path / "db"),
            "--metrics-root",
            str(metrics_root),
            "--programs-root",
            str(programs_root),
        ],
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    assertions = store.list_active_telemetry_assertions()

    assert result.exit_code == 0
    assert len(assertions) == 1
    assert assertions[0].operator is AssertionOperator.FORECAST_GTE
    assert assertions[0].threshold == 97.0


def test_hypothesis_quickstart_persists_burn_rate_operator(tmp_path: Path) -> None:
    metrics_root = tmp_path / "knowledge" / "metrics"
    metrics_root.mkdir(parents=True)
    (metrics_root / "acme.yaml").write_text(
        """
metrics:
  - id: acme.backlog_count
    title: Acme backlog count
    unit: count
    aggregation: last
""".strip()
        + "\n",
        encoding="utf-8",
    )

    programs_root = tmp_path / "programs"
    (programs_root / "acme").mkdir(parents=True)

    result = runner.invoke(
        app,
        [
            "hypothesis",
            "quickstart",
            "--program",
            "acme",
            "--metric-id",
            "acme.backlog_count",
            "--binding-id",
            "binding-001",
            "--cluster",
            "https://cluster.kusto.windows.net",
            "--database",
            "VertexMetrics",
            "--kql-template",
            "Backlog | summarize latest_value=max(count)",
            "--result-column",
            "latest_value",
            "--operator",
            "burn_rate_gte",
            "--threshold",
            "20",
            "--db-root",
            str(tmp_path / "db"),
            "--metrics-root",
            str(metrics_root),
            "--programs-root",
            str(programs_root),
        ],
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    assertions = store.list_active_telemetry_assertions()

    assert result.exit_code == 0
    assert len(assertions) == 1
    assert assertions[0].operator is AssertionOperator.BURN_RATE_GTE
    assert assertions[0].threshold == 20.0


def test_hypothesis_quickstart_rejects_percent_change_without_baseline(tmp_path: Path) -> None:
    metrics_root = tmp_path / "knowledge" / "metrics"
    metrics_root.mkdir(parents=True)
    (metrics_root / "acme.yaml").write_text(
        """
metrics:
  - id: acme.availability
    title: Acme availability
    unit: percent
    aggregation: last
""".strip()
        + "\n",
        encoding="utf-8",
    )

    programs_root = tmp_path / "programs"
    (programs_root / "acme").mkdir(parents=True)

    result = runner.invoke(
        app,
        [
            "hypothesis",
            "quickstart",
            "--program",
            "acme",
            "--metric-id",
            "acme.availability",
            "--binding-id",
            "binding-001",
            "--cluster",
            "https://cluster.kusto.windows.net",
            "--database",
            "VertexMetrics",
            "--kql-template",
            "Availability | summarize latest_value=max(availability)",
            "--result-column",
            "latest_value",
            "--operator",
            "pct_regression",
            "--threshold",
            "5",
            "--db-root",
            str(tmp_path / "db"),
            "--metrics-root",
            str(metrics_root),
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code != 0
    assert "--baseline-value is required for percent-change assertions" in result.output


def test_hypothesis_quickstart_rejects_unknown_query_catalog_entry(tmp_path: Path) -> None:
    metrics_root = tmp_path / "knowledge" / "metrics"
    metrics_root.mkdir(parents=True)
    (metrics_root / "acme.yaml").write_text(
        """
metrics:
  - id: acme.availability
    title: Acme availability
    unit: percent
    aggregation: last
""".strip()
        + "\n",
        encoding="utf-8",
    )

    programs_root = tmp_path / "programs"
    (programs_root / "acme").mkdir(parents=True)

    result = runner.invoke(
        app,
        [
            "hypothesis",
            "quickstart",
            "--program",
            "acme",
            "--metric-id",
            "acme.availability",
            "--binding-id",
            "binding-001",
            "--query-id",
            "missing-query",
            "--operator",
            ">=",
            "--threshold",
            "99.9",
            "--db-root",
            str(tmp_path / "db"),
            "--metrics-root",
            str(metrics_root),
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code != 0
    assert "missing-query" in result.output
    assert "was not found for program acme" in result.output


def test_hypothesis_quickstart_rejects_metric_id_mismatch_with_query_catalog(tmp_path: Path) -> None:
    metrics_root = tmp_path / "knowledge" / "metrics"
    metrics_root.mkdir(parents=True)
    (metrics_root / "acme.yaml").write_text(
        """
metrics:
  - id: acme.availability
    title: Acme availability
    unit: percent
    aggregation: last
""".strip()
        + "\n",
        encoding="utf-8",
    )

    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
kpis:
  - id: availability-latest
    metric_id: acme.availability
    cluster: https://cluster.kusto.windows.net
    database: VertexMetrics
    kql: Availability | summarize latest_value=max(availability)
    result_column: latest_value
""".strip()
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "hypothesis",
            "quickstart",
            "--program",
            "acme",
            "--metric-id",
            "acme.latency",
            "--query-id",
            "availability-latest",
            "--operator",
            ">=",
            "--threshold",
            "99.9",
            "--db-root",
            str(tmp_path / "db"),
            "--metrics-root",
            str(metrics_root),
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code != 0
    assert "KPI query availability-latest declares metric" in result.output
    assert "acme.availability, not acme.latency" in result.output


def test_hypothesis_quickstart_requires_unit_before_inline_metric_definition_create(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "hypothesis",
            "quickstart",
            "--program",
            "acme",
            "--metric-id",
            "acme.availability",
            "--binding-id",
            "binding-001",
            "--cluster",
            "https://cluster.kusto.windows.net",
            "--database",
            "VertexMetrics",
            "--kql-template",
            "Availability | summarize latest_value=max(availability)",
            "--result-column",
            "latest_value",
            "--operator",
            ">=",
            "--threshold",
            "99.9",
            "--db-root",
            str(tmp_path / "db"),
            "--metrics-root",
            str(tmp_path / "knowledge" / "metrics"),
        ],
    )

    assert result.exit_code != 0
    assert "Metric definition acme.availability" in result.output
    assert "--unit" in result.output