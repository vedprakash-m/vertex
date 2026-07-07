from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal, NamedTuple

from src.core.hypothesis_models import AssertionOperator, TelemetryAssertion
from src.core.metric_models import MetricObservation, MetricQualityState
from src.core.source_models import MetricBindingHealth


class EvaluationResult(NamedTuple):
    status: Literal["passed", "violated", "insufficient_data", "degraded_source"]
    violated: bool
    delta_magnitude: float | None
    observed_value: float | None
    expected_value: float | None
    rationale: str


def evaluate_assertion(
    assertion: TelemetryAssertion,
    observations: Sequence[MetricObservation],
    as_of: datetime,
    binding_health: MetricBindingHealth | None,
) -> EvaluationResult:
    default_expected_value = _default_expected_value(assertion)
    if binding_health is not None and binding_health.is_degraded:
        return EvaluationResult(
            status="degraded_source",
            violated=False,
            delta_magnitude=None,
            observed_value=None,
            expected_value=default_expected_value,
            rationale="binding degraded",
        )

    if len(observations) < assertion.window.minimum_observations:
        return EvaluationResult(
            status="insufficient_data",
            violated=False,
            delta_magnitude=None,
            observed_value=None,
            expected_value=default_expected_value,
            rationale="insufficient observations",
        )

    latest = observations[-1]
    if latest.quality_state == MetricQualityState.ZERO_ROWS or latest.value_num is None:
        return EvaluationResult(
            status="insufficient_data",
            violated=False,
            delta_magnitude=None,
            observed_value=latest.value_num,
            expected_value=default_expected_value,
            rationale="latest observation has insufficient numeric value",
        )

    observed_value = float(latest.value_num)
    percent_baseline_error = _get_percent_baseline_error(assertion)
    if percent_baseline_error is not None:
        return EvaluationResult(
            status="insufficient_data",
            violated=False,
            delta_magnitude=None,
            observed_value=observed_value,
            expected_value=default_expected_value,
            rationale=percent_baseline_error,
        )
    trend_error = _get_trend_analysis_error(assertion, observations)
    if trend_error is not None:
        return EvaluationResult(
            status="insufficient_data",
            violated=False,
            delta_magnitude=None,
            observed_value=observed_value,
            expected_value=default_expected_value,
            rationale=trend_error,
        )
    if assertion.operator in {AssertionOperator.FORECAST_GTE, AssertionOperator.FORECAST_LTE}:
        observed_value = _project_observed_value(assertion, observations)
    elif assertion.operator in {AssertionOperator.BURN_RATE_GTE, AssertionOperator.BURN_RATE_LTE}:
        observed_value = _compute_burn_rate_value(assertion, observations)
    expected_value = _resolve_expected_value(assertion, observed_value)
    violated = _is_violated(assertion, observed_value)
    delta_magnitude = _compute_delta_magnitude(
        observed_value=observed_value,
        expected_value=expected_value,
        tolerance_rel=assertion.tolerance_rel,
        tolerance_abs=assertion.tolerance_abs,
    )
    return EvaluationResult(
        status="violated" if violated else "passed",
        violated=violated,
        delta_magnitude=delta_magnitude,
        observed_value=observed_value,
        expected_value=expected_value,
        rationale=_build_rationale(assertion, observed_value, expected_value, as_of),
    )


def _is_violated(assertion: TelemetryAssertion, observed_value: float) -> bool:
    operator = assertion.operator
    expected_value = float(assertion.threshold)
    if operator == AssertionOperator.GTE:
        return observed_value < expected_value
    if operator == AssertionOperator.LTE:
        return observed_value > expected_value
    if operator == AssertionOperator.EQ:
        return observed_value != expected_value
    if operator == AssertionOperator.NEQ:
        return observed_value == expected_value
    if operator == AssertionOperator.BETWEEN:
        upper_bound = _require_threshold_upper(assertion)
        return observed_value < expected_value or observed_value > upper_bound
    if operator == AssertionOperator.PCT_IMPROVEMENT:
        baseline_value = _require_percent_baseline(assertion)
        improvement_pct = ((observed_value - baseline_value) / abs(baseline_value)) * 100.0
        return improvement_pct < expected_value
    if operator == AssertionOperator.PCT_REGRESSION:
        baseline_value = _require_percent_baseline(assertion)
        regression_pct = ((baseline_value - observed_value) / abs(baseline_value)) * 100.0
        return regression_pct > expected_value
    if operator == AssertionOperator.FORECAST_GTE:
        return observed_value < expected_value
    if operator == AssertionOperator.FORECAST_LTE:
        return observed_value > expected_value
    if operator == AssertionOperator.BURN_RATE_GTE:
        return observed_value < expected_value
    if operator == AssertionOperator.BURN_RATE_LTE:
        return observed_value > expected_value
    raise ValueError(f"Unsupported assertion operator: {operator.value}")


def _resolve_expected_value(assertion: TelemetryAssertion, observed_value: float) -> float:
    lower_bound = float(assertion.threshold)
    if assertion.operator != AssertionOperator.BETWEEN:
        if assertion.operator == AssertionOperator.PCT_IMPROVEMENT:
            baseline_value = _require_percent_baseline(assertion)
            return round(baseline_value * (1.0 + lower_bound / 100.0), 10)
        if assertion.operator == AssertionOperator.PCT_REGRESSION:
            baseline_value = _require_percent_baseline(assertion)
            return round(baseline_value * (1.0 - lower_bound / 100.0), 10)
        return lower_bound
    upper_bound = _require_threshold_upper(assertion)
    if observed_value < lower_bound:
        return lower_bound
    if observed_value > upper_bound:
        return upper_bound
    return lower_bound


def _default_expected_value(assertion: TelemetryAssertion) -> float:
    baseline_value = assertion.baseline_value
    if assertion.operator == AssertionOperator.PCT_IMPROVEMENT and baseline_value is not None and baseline_value != 0.0:
        return round(float(baseline_value) * (1.0 + float(assertion.threshold) / 100.0), 10)
    if assertion.operator == AssertionOperator.PCT_REGRESSION and baseline_value is not None and baseline_value != 0.0:
        return round(float(baseline_value) * (1.0 - float(assertion.threshold) / 100.0), 10)
    return float(assertion.threshold)


def _get_percent_baseline_error(assertion: TelemetryAssertion) -> str | None:
    if assertion.operator not in {AssertionOperator.PCT_IMPROVEMENT, AssertionOperator.PCT_REGRESSION}:
        return None
    if assertion.baseline_value is None:
        return "percent-change assertion is missing a baseline value"
    if float(assertion.baseline_value) == 0.0:
        return "percent-change assertion baseline cannot be zero"
    return None


def _get_trend_analysis_error(
    assertion: TelemetryAssertion,
    observations: Sequence[MetricObservation],
) -> str | None:
    if assertion.operator not in {
        AssertionOperator.FORECAST_GTE,
        AssertionOperator.FORECAST_LTE,
        AssertionOperator.BURN_RATE_GTE,
        AssertionOperator.BURN_RATE_LTE,
    }:
        return None
    projected_points = [
        observation
        for observation in observations
        if observation.value_num is not None and observation.quality_state != MetricQualityState.ZERO_ROWS
    ]
    if len(projected_points) < 2:
        return "trend assertion requires at least 2 numeric observations"
    first = projected_points[0]
    latest = projected_points[-1]
    span_seconds = (latest.observed_at - first.observed_at).total_seconds()
    if span_seconds <= 0:
        return "trend assertion requires observations with increasing timestamps"
    return None


def _project_observed_value(assertion: TelemetryAssertion, observations: Sequence[MetricObservation]) -> float:
    projected_points = _numeric_observations(observations)
    first = projected_points[0]
    latest = projected_points[-1]
    span_seconds = (latest.observed_at - first.observed_at).total_seconds()
    first_value = _require_numeric_value(first)
    latest_value = _require_numeric_value(latest)
    slope_per_second = (latest_value - first_value) / span_seconds
    projection_horizon_seconds = float(assertion.window.days) * 24.0 * 3600.0
    return latest_value + (slope_per_second * projection_horizon_seconds)


def _compute_burn_rate_value(assertion: TelemetryAssertion, observations: Sequence[MetricObservation]) -> float:
    projected_points = _numeric_observations(observations)
    first = projected_points[0]
    latest = projected_points[-1]
    span_seconds = (latest.observed_at - first.observed_at).total_seconds()
    first_value = _require_numeric_value(first)
    latest_value = _require_numeric_value(latest)
    slope_per_second = (first_value - latest_value) / span_seconds
    projection_horizon_seconds = float(assertion.window.days) * 24.0 * 3600.0
    return slope_per_second * projection_horizon_seconds


def _numeric_observations(observations: Sequence[MetricObservation]) -> list[MetricObservation]:
    return [
        observation
        for observation in observations
        if observation.value_num is not None and observation.quality_state != MetricQualityState.ZERO_ROWS
    ]


def _require_numeric_value(observation: MetricObservation) -> float:
    value_num = observation.value_num
    if value_num is None:
        raise ValueError("Expected numeric metric observation value")
    return float(value_num)


def _require_threshold_upper(assertion: TelemetryAssertion) -> float:
    if assertion.threshold_upper is None:
        raise ValueError("BETWEEN assertions require threshold_upper")
    return float(assertion.threshold_upper)


def _require_percent_baseline(assertion: TelemetryAssertion) -> float:
    if assertion.baseline_value is None:
        raise ValueError("Percent-change assertions require baseline_value")
    baseline_value = float(assertion.baseline_value)
    if baseline_value == 0.0:
        raise ValueError("Percent-change assertions require a non-zero baseline_value")
    return baseline_value


def _compute_delta_magnitude(
    *,
    observed_value: float,
    expected_value: float,
    tolerance_rel: float,
    tolerance_abs: float | None,
) -> float:
    tolerance = max(tolerance_abs or 0.0, abs(expected_value) * tolerance_rel, 1e-9)
    return abs(observed_value - expected_value) / tolerance


def _build_rationale(
    assertion: TelemetryAssertion,
    observed_value: float,
    expected_value: float,
    as_of: datetime,
) -> str:
    if assertion.operator == AssertionOperator.BETWEEN:
        upper_bound = _require_threshold_upper(assertion)
        return (
            f"Observed {observed_value:g} outside expected range {assertion.threshold:g}..{upper_bound:g} "
            f"at {as_of.isoformat()}"
        )
    if assertion.operator == AssertionOperator.PCT_IMPROVEMENT:
        baseline_value = _require_percent_baseline(assertion)
        improvement_pct = ((observed_value - baseline_value) / abs(baseline_value)) * 100.0
        return (
            f"Observed {observed_value:g} vs baseline {baseline_value:g} ({improvement_pct:+.1f}%) "
            f"with required improvement >= {assertion.threshold:g}% at {as_of.isoformat()}"
        )
    if assertion.operator == AssertionOperator.PCT_REGRESSION:
        baseline_value = _require_percent_baseline(assertion)
        regression_pct = ((baseline_value - observed_value) / abs(baseline_value)) * 100.0
        return (
            f"Observed {observed_value:g} vs baseline {baseline_value:g} (regression {regression_pct:.1f}%) "
            f"with allowed regression <= {assertion.threshold:g}% at {as_of.isoformat()}"
        )
    if assertion.operator in {AssertionOperator.FORECAST_GTE, AssertionOperator.FORECAST_LTE}:
        horizon_days = assertion.window.days
        comparator = ">=" if assertion.operator == AssertionOperator.FORECAST_GTE else "<="
        return (
            f"Projected value {observed_value:g} over the next {horizon_days:g}d {comparator} expected {expected_value:g} "
            f"at {as_of.isoformat()}"
        )
    if assertion.operator in {AssertionOperator.BURN_RATE_GTE, AssertionOperator.BURN_RATE_LTE}:
        horizon_days = assertion.window.days
        comparator = ">=" if assertion.operator == AssertionOperator.BURN_RATE_GTE else "<="
        return (
            f"Observed burn rate {observed_value:g} units per {horizon_days:g}d {comparator} expected {expected_value:g} "
            f"at {as_of.isoformat()}"
        )
    return (
        f"Observed {observed_value:g} {assertion.operator.value} expected {expected_value:g} "
        f"at {as_of.isoformat()}"
    )
