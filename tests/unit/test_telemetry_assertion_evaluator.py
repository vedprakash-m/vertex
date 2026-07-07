from __future__ import annotations

from datetime import datetime, timezone

from src.core.hypothesis_models import AssertionOperator, TelemetryAssertion
from src.core.metric_models import MetricAggregation, MetricObservation, MetricQualityState, ObservationWindow
from src.core.source_models import MetricBindingHealth
from src.core.telemetry_assertion_evaluator import evaluate_assertion


def test_evaluate_assertion_returns_violated_result_for_threshold_breach() -> None:
    assertion = TelemetryAssertion(
        id="assertion-001",
        program_id="acme",
        metric_id="acme.cluster_count",
        window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
        operator=AssertionOperator.GTE,
        threshold=150.0,
    )
    observations = (
        MetricObservation(
            observation_id="obs-001",
            program_id="acme",
            metric_id="acme.cluster_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 1, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 1, 5, tzinfo=timezone.utc),
            value_num=120.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
        ),
    )

    result = evaluate_assertion(assertion, observations, datetime(2026, 5, 20, 2, 0, tzinfo=timezone.utc), None)

    assert result.status == "violated"
    assert result.violated is True
    assert result.observed_value == 120.0
    assert result.expected_value == 150.0
    assert result.delta_magnitude is not None
    assert result.delta_magnitude > 1.0


def test_evaluate_assertion_returns_degraded_source_when_binding_is_degraded() -> None:
    assertion = TelemetryAssertion(
        id="assertion-001",
        program_id="acme",
        metric_id="acme.cluster_count",
        window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
        operator=AssertionOperator.GTE,
        threshold=150.0,
    )
    observations = ()
    binding_health = MetricBindingHealth(
        program_id="acme",
        binding_id="binding-001",
        metric_id="acme.cluster_count",
        last_success_at=None,
        last_attempt_at=datetime(2026, 5, 20, 2, 0, tzinfo=timezone.utc),
        last_successful_observation_at=None,
        last_failure_at=datetime(2026, 5, 20, 2, 0, tzinfo=timezone.utc),
        consecutive_failures=1,
        is_degraded=True,
    )

    result = evaluate_assertion(assertion, observations, datetime(2026, 5, 20, 2, 0, tzinfo=timezone.utc), binding_health)

    assert result.status == "degraded_source"
    assert result.violated is False


def test_evaluate_assertion_between_operator_uses_upper_bound_when_observation_is_above_range() -> None:
    assertion = TelemetryAssertion(
        id="assertion-001",
        program_id="acme",
        metric_id="acme.success_rate",
        window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
        operator=AssertionOperator.BETWEEN,
        threshold=95.0,
        threshold_upper=99.5,
    )
    observations = (
        MetricObservation(
            observation_id="obs-001",
            program_id="acme",
            metric_id="acme.success_rate",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 1, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 1, 5, tzinfo=timezone.utc),
            value_num=100.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
        ),
    )

    result = evaluate_assertion(assertion, observations, datetime(2026, 5, 20, 2, 0, tzinfo=timezone.utc), None)

    assert result.status == "violated"
    assert result.violated is True
    assert result.observed_value == 100.0
    assert result.expected_value == 99.5
    assert "outside expected range 95..99.5" in result.rationale


def test_evaluate_assertion_between_operator_passes_when_observation_is_inside_range() -> None:
    assertion = TelemetryAssertion(
        id="assertion-001",
        program_id="acme",
        metric_id="acme.success_rate",
        window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
        operator=AssertionOperator.BETWEEN,
        threshold=95.0,
        threshold_upper=99.5,
    )
    observations = (
        MetricObservation(
            observation_id="obs-001",
            program_id="acme",
            metric_id="acme.success_rate",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 1, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 1, 5, tzinfo=timezone.utc),
            value_num=97.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
        ),
    )

    result = evaluate_assertion(assertion, observations, datetime(2026, 5, 20, 2, 0, tzinfo=timezone.utc), None)

    assert result.status == "passed"
    assert result.violated is False
    assert result.expected_value == 95.0


def test_evaluate_assertion_pct_improvement_uses_baseline_threshold() -> None:
    assertion = TelemetryAssertion(
        id="assertion-001",
        program_id="acme",
        metric_id="acme.cluster_count",
        window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
        operator=AssertionOperator.PCT_IMPROVEMENT,
        threshold=10.0,
        baseline_value=100.0,
    )
    observations = (
        MetricObservation(
            observation_id="obs-001",
            program_id="acme",
            metric_id="acme.cluster_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 1, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 1, 5, tzinfo=timezone.utc),
            value_num=108.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
        ),
    )

    result = evaluate_assertion(assertion, observations, datetime(2026, 5, 20, 2, 0, tzinfo=timezone.utc), None)

    assert result.status == "violated"
    assert result.violated is True
    assert result.expected_value == 110.0
    assert "required improvement >= 10%" in result.rationale


def test_evaluate_assertion_pct_regression_allows_small_decline_from_baseline() -> None:
    assertion = TelemetryAssertion(
        id="assertion-001",
        program_id="acme",
        metric_id="acme.cluster_count",
        window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
        operator=AssertionOperator.PCT_REGRESSION,
        threshold=10.0,
        baseline_value=100.0,
    )
    observations = (
        MetricObservation(
            observation_id="obs-001",
            program_id="acme",
            metric_id="acme.cluster_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 1, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 1, 5, tzinfo=timezone.utc),
            value_num=92.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
        ),
    )

    result = evaluate_assertion(assertion, observations, datetime(2026, 5, 20, 2, 0, tzinfo=timezone.utc), None)

    assert result.status == "passed"
    assert result.violated is False
    assert result.expected_value == 90.0
    assert "allowed regression <= 10%" in result.rationale


def test_evaluate_assertion_pct_change_requires_non_zero_baseline() -> None:
    assertion = TelemetryAssertion(
        id="assertion-001",
        program_id="acme",
        metric_id="acme.cluster_count",
        window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
        operator=AssertionOperator.PCT_IMPROVEMENT,
        threshold=10.0,
        baseline_value=0.0,
    )
    observations = (
        MetricObservation(
            observation_id="obs-001",
            program_id="acme",
            metric_id="acme.cluster_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 1, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 1, 5, tzinfo=timezone.utc),
            value_num=108.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
        ),
    )

    result = evaluate_assertion(assertion, observations, datetime(2026, 5, 20, 2, 0, tzinfo=timezone.utc), None)

    assert result.status == "insufficient_data"
    assert result.violated is False
    assert result.rationale == "percent-change assertion baseline cannot be zero"


def test_evaluate_assertion_forecast_gte_violates_when_projection_falls_below_threshold() -> None:
    assertion = TelemetryAssertion(
        id="assertion-001",
        program_id="acme",
        metric_id="acme.cluster_count",
        window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST, minimum_observations=2),
        operator=AssertionOperator.FORECAST_GTE,
        threshold=90.0,
    )
    observations = (
        MetricObservation(
            observation_id="obs-001",
            program_id="acme",
            metric_id="acme.cluster_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 13, 0, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 13, 1, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 13, 1, 5, tzinfo=timezone.utc),
            value_num=140.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
        ),
        MetricObservation(
            observation_id="obs-002",
            program_id="acme",
            metric_id="acme.cluster_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 1, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 1, 5, tzinfo=timezone.utc),
            value_num=100.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
        ),
    )

    result = evaluate_assertion(assertion, observations, datetime(2026, 5, 20, 2, 0, tzinfo=timezone.utc), None)

    assert result.status == "violated"
    assert result.violated is True
    assert result.observed_value is not None
    assert result.observed_value < 90.0
    assert result.expected_value == 90.0
    assert "Projected value" in result.rationale


def test_evaluate_assertion_forecast_lte_passes_when_projection_stays_below_threshold() -> None:
    assertion = TelemetryAssertion(
        id="assertion-001",
        program_id="acme",
        metric_id="acme.error_rate",
        window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST, minimum_observations=2),
        operator=AssertionOperator.FORECAST_LTE,
        threshold=8.0,
    )
    observations = (
        MetricObservation(
            observation_id="obs-001",
            program_id="acme",
            metric_id="acme.error_rate",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 13, 0, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 13, 1, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 13, 1, 5, tzinfo=timezone.utc),
            value_num=7.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
        ),
        MetricObservation(
            observation_id="obs-002",
            program_id="acme",
            metric_id="acme.error_rate",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 1, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 1, 5, tzinfo=timezone.utc),
            value_num=7.2,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
        ),
    )

    result = evaluate_assertion(assertion, observations, datetime(2026, 5, 20, 2, 0, tzinfo=timezone.utc), None)

    assert result.status == "passed"
    assert result.violated is False
    assert result.observed_value is not None
    assert result.observed_value <= 8.0


def test_evaluate_assertion_forecast_requires_two_numeric_observations() -> None:
    assertion = TelemetryAssertion(
        id="assertion-001",
        program_id="acme",
        metric_id="acme.cluster_count",
        window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
        operator=AssertionOperator.FORECAST_GTE,
        threshold=150.0,
    )
    observations = (
        MetricObservation(
            observation_id="obs-001",
            program_id="acme",
            metric_id="acme.cluster_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 1, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 1, 5, tzinfo=timezone.utc),
            value_num=160.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
        ),
    )

    result = evaluate_assertion(assertion, observations, datetime(2026, 5, 20, 2, 0, tzinfo=timezone.utc), None)

    assert result.status == "insufficient_data"
    assert result.violated is False
    assert result.rationale == "trend assertion requires at least 2 numeric observations"


def test_evaluate_assertion_burn_rate_gte_violates_when_burndown_is_too_slow() -> None:
    assertion = TelemetryAssertion(
        id="assertion-001",
        program_id="acme",
        metric_id="acme.backlog_count",
        window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST, minimum_observations=2),
        operator=AssertionOperator.BURN_RATE_GTE,
        threshold=20.0,
    )
    observations = (
        MetricObservation(
            observation_id="obs-001",
            program_id="acme",
            metric_id="acme.backlog_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 13, 0, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 13, 1, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 13, 1, 5, tzinfo=timezone.utc),
            value_num=100.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
        ),
        MetricObservation(
            observation_id="obs-002",
            program_id="acme",
            metric_id="acme.backlog_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 1, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 1, 5, tzinfo=timezone.utc),
            value_num=88.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
        ),
    )

    result = evaluate_assertion(assertion, observations, datetime(2026, 5, 20, 2, 0, tzinfo=timezone.utc), None)

    assert result.status == "violated"
    assert result.violated is True
    assert result.observed_value is not None
    assert result.observed_value < 20.0
    assert result.expected_value == 20.0
    assert "Observed burn rate" in result.rationale


def test_evaluate_assertion_burn_rate_lte_passes_when_burndown_stays_under_cap() -> None:
    assertion = TelemetryAssertion(
        id="assertion-001",
        program_id="acme",
        metric_id="acme.backlog_count",
        window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST, minimum_observations=2),
        operator=AssertionOperator.BURN_RATE_LTE,
        threshold=25.0,
    )
    observations = (
        MetricObservation(
            observation_id="obs-001",
            program_id="acme",
            metric_id="acme.backlog_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 13, 0, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 13, 1, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 13, 1, 5, tzinfo=timezone.utc),
            value_num=100.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
        ),
        MetricObservation(
            observation_id="obs-002",
            program_id="acme",
            metric_id="acme.backlog_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 1, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 1, 5, tzinfo=timezone.utc),
            value_num=82.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
        ),
    )

    result = evaluate_assertion(assertion, observations, datetime(2026, 5, 20, 2, 0, tzinfo=timezone.utc), None)

    assert result.status == "passed"
    assert result.violated is False
    assert result.observed_value is not None
    assert result.observed_value <= 25.0


def test_evaluate_assertion_burn_rate_requires_two_numeric_observations() -> None:
    assertion = TelemetryAssertion(
        id="assertion-001",
        program_id="acme",
        metric_id="acme.backlog_count",
        window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
        operator=AssertionOperator.BURN_RATE_GTE,
        threshold=20.0,
    )
    observations = (
        MetricObservation(
            observation_id="obs-001",
            program_id="acme",
            metric_id="acme.backlog_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 1, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 1, 5, tzinfo=timezone.utc),
            value_num=80.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
        ),
    )

    result = evaluate_assertion(assertion, observations, datetime(2026, 5, 20, 2, 0, tzinfo=timezone.utc), None)

    assert result.status == "insufficient_data"
    assert result.violated is False
    assert result.rationale == "trend assertion requires at least 2 numeric observations"