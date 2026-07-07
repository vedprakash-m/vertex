from __future__ import annotations
import pytest
from pathlib import Path
pytestmark = pytest.mark.skipif(not (Path(__file__).resolve().parents[2] / "editions").exists(), reason="Requires private data")

import json
from pathlib import Path

from typer.testing import CliRunner

from cli import app
from src.core.reality_store import RealityStore
from tests.unit.test_commands_reality import _seed_threshold_breach


runner = CliRunner()


def test_admin_reconcile_runs_full_truth_loop_and_updates_digest_cache(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    _seed_threshold_breach(store)

    result = runner.invoke(
        app,
        ["admin", "reconcile", "--program", "acme", "--db-root", str(tmp_path / "db"), "--format", "json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["program_id"] == "acme"
    assert payload["tier"] == "all"
    assert payload["digest_cache_updated"] is True
    assert payload["evaluations_written"] == 1
    assert payload["challenges_opened"] == 1
    assert payload["hypotheses_challenged"] == 1
    assert payload["health"] == "red"
    assert store.read_digest_cache_row() is not None


def test_admin_reconcile_tier_filters_metrics_and_skips_digest_cache_write(monkeypatch, tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    _seed_threshold_breach(store)
    _seed_cold_threshold_breach(store)

    monkeypatch.setattr(
        "src.commands.admin_reconcile._build_metric_definition_map",
        lambda *, as_of: {
            "acme.cluster_count": _metric_definition("acme.cluster_count", freshness_tier="hot"),
            "acme.deployment_latency": _metric_definition("acme.deployment_latency", freshness_tier="cold"),
        },
    )

    result = runner.invoke(
        app,
        [
            "admin",
            "reconcile",
            "--program",
            "acme",
            "--tier",
            "hot",
            "--db-root",
            str(tmp_path / "db"),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["tier"] == "hot"
    assert payload["digest_cache_updated"] is False
    assert payload["evaluations_written"] == 1
    assert payload["challenges_opened"] == 1
    challenges = store.list_open_challenges()
    assert len(challenges) == 1
    assert challenges[0].hypothesis_id == "hyp-001"
    assert store.read_digest_cache_row() is None


def _seed_cold_threshold_breach(store: RealityStore) -> None:
    from datetime import date, datetime, timezone

    from src.core.hypothesis_models import AssertionOperator, Hypothesis, HypothesisKind, HypothesisStatus, TelemetryAssertion
    from src.core.metric_models import MetricAggregation, MetricObservation, MetricQualityState, MetricSourceBinding, ObservationWindow
    from src.core.source_models import MetricBindingHealth

    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="binding-002",
            metric_id="acme.deployment_latency",
            program_id="acme",
            source_kind="kusto",
            kql_template="Metrics | summarize p95(Duration)",
            result_column="P95",
            validated=True,
        )
    )
    store.upsert_telemetry_assertion(
        TelemetryAssertion(
            id="assertion-002",
            program_id="acme",
            metric_id="acme.deployment_latency",
            window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
            operator=AssertionOperator.LTE,
            threshold=45.0,
            tolerance_rel=0.05,
        )
    )
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-002",
            short_id="H-002",
            program_id="acme",
            kind=HypothesisKind.SCALAR_FACT,
            statement="Deployment latency stays at or below 45 minutes.",
            expected_value=45.0,
            as_of_date=date(2026, 5, 20),
            telemetry_assertion_id="assertion-002",
            proposed_by="pm",
            proposed_at=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            status=HypothesisStatus.CONFIRMED,
            confirmed_by="pm",
            confirmed_at=datetime(2026, 5, 20, 9, 5, tzinfo=timezone.utc),
        )
    )
    store.upsert_binding_health(
        MetricBindingHealth(
            program_id="acme",
            binding_id="binding-002",
            metric_id="acme.deployment_latency",
            last_success_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            last_attempt_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            last_successful_observation_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            last_failure_at=None,
            consecutive_failures=0,
            is_degraded=False,
        )
    )
    store.write_metric_observation(
        MetricObservation(
            observation_id="obs-002",
            program_id="acme",
            metric_id="acme.deployment_latency",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            value_num=90.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
            source_binding_id="binding-002",
            binding_version=1,
        )
    )


def _metric_definition(metric_id: str, *, freshness_tier: str):
    from src.core.metric_models import MetricAggregation, MetricDefinition

    return MetricDefinition(
        id=metric_id,
        title=metric_id,
        unit="count",
        aggregation=MetricAggregation.LAST,
        freshness_tier=freshness_tier,
    )