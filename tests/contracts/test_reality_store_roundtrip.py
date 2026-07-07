from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from src.core.hypothesis_models import AssertionOperator, Hypothesis, HypothesisKind, HypothesisStatus, TelemetryAssertion
from src.core.metric_models import MetricAggregation, MetricSourceBinding, ObservationWindow
from src.core.reality_store import RealityStore


def test_reality_store_round_trips_l1_foundation_contract(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()

    binding = MetricSourceBinding(
        binding_id="binding-contract",
        metric_id="acme.cluster_count",
        program_id="acme",
        source_kind="kusto",
        kql_template="Metrics | summarize count()",
        result_column="cluster_count",
    )
    assertion = TelemetryAssertion(
        id="assertion-contract",
        program_id="acme",
        metric_id="acme.cluster_count",
        window=ObservationWindow(days=1, aggregation=MetricAggregation.LAST),
        operator=AssertionOperator.GTE,
        threshold=1.0,
    )
    hypothesis = Hypothesis(
        id="hyp-contract",
        short_id="H-001",
        program_id="acme",
        kind=HypothesisKind.SCALAR_FACT,
        statement="At least one cluster exists.",
        expected_value=1.0,
        as_of_date=date(2026, 5, 20),
        telemetry_assertion_id="assertion-contract",
        status=HypothesisStatus.PROPOSED,
    )

    store.upsert_metric_source_binding(binding)
    store.upsert_telemetry_assertion(assertion)
    store.upsert_hypothesis(hypothesis)

    assert store.get_metric_source_binding(binding.binding_id) == binding
    assert store.get_telemetry_assertion(assertion.id) == assertion
    assert store.get_hypothesis(hypothesis.id) == hypothesis

    store.record_schema_version(
        "2026_05_20_001_l1_foundation",
        "vertex-test",
        applied_at=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
    )

    assert store.read_schema_versions() == ("2026_05_20_001_l1_foundation",)