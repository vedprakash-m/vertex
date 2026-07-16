"""ADF-W5.8 (Section 8.2.5): ``value_baseline_expired_or_incomparable``
detection logic -- the last open alert category.

Tests the pure comparison detector over a ``ValueCockpitSummary``; the cockpit
wiring (best-effort alert emission) is covered by
``test_cockpit_value_baseline_alert.py``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.core.cockpit_models import (
    TimeSavingsCertification,
    ValueCockpitSummary,
    ValueConfidence,
    ValueMetric,
)
from src.core.value_baseline_freshness_detector import (
    DEFAULT_MAX_AGE_DAYS,
    DEFAULT_MIN_MATCHED_PAIRS,
    build_value_baseline_alert_message,
    detect_value_baseline_freshness,
)

_NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)


def _metric(
    metric_id: str,
    *,
    confidence: ValueConfidence = ValueConfidence.MEASURED,
    baseline_value: float | int | None = None,
    delta_value: float | int | None = None,
    period_end: datetime | None = None,
    period_start: datetime | None = None,
) -> ValueMetric:
    end = period_end or _NOW
    start = period_start or (end - timedelta(days=7))
    return ValueMetric(
        metric_id=metric_id,
        program_id="test-prog",
        edition_id=None,
        scope="program_aggregate",
        label=f"Test metric {metric_id}",
        value=100.0,
        unit="seconds",
        confidence=confidence,
        baseline_value=baseline_value,
        delta_value=delta_value,
        formula_version="test.v1",
        evidence_refs=(),
        period_start=start,
        period_end=end,
    )


def _summary(
    metrics: tuple[ValueMetric, ...] = (),
    cert: TimeSavingsCertification | None = None,
) -> ValueCockpitSummary:
    return ValueCockpitSummary(metrics=metrics, time_savings_certification=cert)


# ---------------------------------------------------------------------------
# Empty / non-measured summaries
# ---------------------------------------------------------------------------


class TestNothingToEvaluate:
    def test_empty_metrics_is_not_degraded(self) -> None:
        finding = detect_value_baseline_freshness(_summary(), observed_at=_NOW)
        assert not finding.is_degraded
        assert finding.affected_metric_ids == ()

    def test_only_calibrated_metrics_are_not_evaluated(self) -> None:
        summary = _summary(metrics=(_metric("m1", confidence=ValueConfidence.CALIBRATED),))
        finding = detect_value_baseline_freshness(summary, observed_at=_NOW)
        assert not finding.is_degraded

    def test_only_proxy_metrics_are_not_evaluated(self) -> None:
        summary = _summary(metrics=(_metric("m1", confidence=ValueConfidence.PROXY),))
        finding = detect_value_baseline_freshness(summary, observed_at=_NOW)
        assert not finding.is_degraded

    def test_only_unavailable_metrics_are_not_evaluated(self) -> None:
        summary = _summary(metrics=(_metric("m1", confidence=ValueConfidence.UNAVAILABLE),))
        finding = detect_value_baseline_freshness(summary, observed_at=_NOW)
        assert not finding.is_degraded


# ---------------------------------------------------------------------------
# Expired
# ---------------------------------------------------------------------------


class TestExpired:
    def test_measured_metric_within_freshness_is_not_expired(self) -> None:
        summary = _summary(metrics=(_metric("m1", period_end=_NOW - timedelta(days=30)),))
        finding = detect_value_baseline_freshness(summary, observed_at=_NOW)
        assert not finding.is_degraded

    def test_measured_metric_just_inside_cutoff(self) -> None:
        summary = _summary(
            metrics=(_metric("m1", period_end=_NOW - timedelta(days=DEFAULT_MAX_AGE_DAYS - 1)),)
        )
        finding = detect_value_baseline_freshness(summary, observed_at=_NOW)
        assert not finding.is_degraded

    def test_measured_metric_at_exact_cutoff_is_not_expired(self) -> None:
        # Exactly at the budget boundary is NOT expired (the comparison is <, not <=)
        summary = _summary(
            metrics=(_metric("m1", period_end=_NOW - timedelta(days=DEFAULT_MAX_AGE_DAYS)),)
        )
        finding = detect_value_baseline_freshness(summary, observed_at=_NOW)
        assert not finding.is_degraded

    def test_measured_metric_past_cutoff_is_expired(self) -> None:
        summary = _summary(
            metrics=(_metric("m1", period_end=_NOW - timedelta(days=DEFAULT_MAX_AGE_DAYS + 5)),)
        )
        finding = detect_value_baseline_freshness(summary, observed_at=_NOW)
        assert finding.is_degraded
        assert finding.reason == "expired"
        assert "m1" in finding.affected_metric_ids

    def test_custom_max_age_days(self) -> None:
        summary = _summary(metrics=(_metric("m1", period_end=_NOW - timedelta(days=10)),))
        # 10 days old, default 90 -> not expired
        assert not detect_value_baseline_freshness(summary, observed_at=_NOW).is_degraded
        # 10 days old, custom 5 -> expired
        finding = detect_value_baseline_freshness(summary, observed_at=_NOW, max_age_days=5)
        assert finding.is_degraded

    def test_expired_detail_mentions_relabel(self) -> None:
        summary = _summary(
            metrics=(_metric("m1", period_end=_NOW - timedelta(days=120)),)
        )
        finding = detect_value_baseline_freshness(summary, observed_at=_NOW)
        assert "calibrated" in finding.detail.lower()
        assert "re-measure" in finding.detail.lower()


# ---------------------------------------------------------------------------
# Incomparable
# ---------------------------------------------------------------------------


class TestIncomparableMetric:
    def test_degenerate_period_span_is_incomparable(self) -> None:
        # period_start == period_end: a "measurement" with no observation span
        end = _NOW - timedelta(days=30)
        metric = _metric(
            "m1",
            baseline_value=200.0,
            delta_value=100.0,
            period_start=end,
            period_end=end,
        )
        finding = detect_value_baseline_freshness(_summary(metrics=(metric,)), observed_at=_NOW)
        assert finding.is_degraded
        assert finding.reason == "incomparable"

    def test_measured_comparison_with_real_span_is_fine(self) -> None:
        end = _NOW - timedelta(days=30)
        metric = _metric(
            "m1",
            baseline_value=200.0,
            delta_value=100.0,
            period_start=end - timedelta(days=7),
            period_end=end,
        )
        finding = detect_value_baseline_freshness(_summary(metrics=(metric,)), observed_at=_NOW)
        assert not finding.is_degraded


class TestIncomparableCertification:
    def _cert(self, *, manual: int, vertex: int) -> TimeSavingsCertification:
        return TimeSavingsCertification(
            schema_version="1",
            program_id="test-prog",
            edition_id="xpf_weekly",
            workflow="weekly_issue",
            manual_sample_count=manual,
            vertex_sample_count=vertex,
            manual_median_active_seconds=600.0,
            vertex_median_active_seconds=120.0,
            savings_ratio=0.80,
            confidence_interval_low=0.75,
        )

    def test_cert_below_min_pairs_is_incomparable(self) -> None:
        finding = detect_value_baseline_freshness(
            _summary(cert=self._cert(manual=5, vertex=8)), observed_at=_NOW
        )
        assert finding.is_degraded
        assert finding.reason == "incomparable"
        assert any("time_savings_certification" in mid for mid in finding.affected_metric_ids)

    def test_cert_at_min_pairs_is_comparable(self) -> None:
        finding = detect_value_baseline_freshness(
            _summary(cert=self._cert(manual=8, vertex=8)), observed_at=_NOW
        )
        assert not finding.is_degraded

    def test_cert_above_min_pairs_is_comparable(self) -> None:
        finding = detect_value_baseline_freshness(
            _summary(cert=self._cert(manual=15, vertex=12)), observed_at=_NOW
        )
        assert not finding.is_degraded

    def test_custom_min_matched_pairs(self) -> None:
        # 5 pairs: incomparable at default (8), comparable at custom (3)
        cert = self._cert(manual=5, vertex=5)
        assert detect_value_baseline_freshness(
            _summary(cert=cert), observed_at=_NOW
        ).is_degraded
        assert not detect_value_baseline_freshness(
            _summary(cert=cert), observed_at=_NOW, min_matched_pairs=3
        ).is_degraded


# ---------------------------------------------------------------------------
# Combined expired + incomparable
# ---------------------------------------------------------------------------


class TestCombined:
    def test_expired_and_incomparable_reports_both(self) -> None:
        expired_metric = _metric("expired1", period_end=_NOW - timedelta(days=120))
        end = _NOW - timedelta(days=30)
        incomparable_metric = _metric(
            "incomp1",
            baseline_value=200.0,
            delta_value=100.0,
            period_start=end,
            period_end=end,
        )
        finding = detect_value_baseline_freshness(
            _summary(metrics=(expired_metric, incomparable_metric)), observed_at=_NOW
        )
        assert finding.is_degraded
        assert "expired1" in finding.affected_metric_ids
        assert "incomp1" in finding.affected_metric_ids


# ---------------------------------------------------------------------------
# Timezone handling
# ---------------------------------------------------------------------------


class TestTimezoneHandling:
    def test_naive_observed_at_treated_as_utc(self) -> None:
        naive_now = _NOW.replace(tzinfo=None)
        summary = _summary(metrics=(_metric("m1", period_end=_NOW - timedelta(days=120)),))
        finding = detect_value_baseline_freshness(summary, observed_at=naive_now)
        assert finding.is_degraded

    def test_naive_period_end_treated_as_utc(self) -> None:
        metric = _metric("m1", period_end=(_NOW - timedelta(days=120)).replace(tzinfo=None))
        finding = detect_value_baseline_freshness(_summary(metrics=(metric,)), observed_at=_NOW)
        assert finding.is_degraded


# ---------------------------------------------------------------------------
# Alert message builder
# ---------------------------------------------------------------------------


class TestAlertMessage:
    def test_build_message_returns_message_and_command(self) -> None:
        summary = _summary(metrics=(_metric("m1", period_end=_NOW - timedelta(days=120)),))
        finding = detect_value_baseline_freshness(summary, observed_at=_NOW)
        message, next_command = build_value_baseline_alert_message(finding)
        assert "expired" in message.lower()
        assert "{program}" in next_command

    def test_build_message_asserts_on_non_degraded(self) -> None:
        finding = detect_value_baseline_freshness(_summary(), observed_at=_NOW)
        with pytest.raises(AssertionError):
            build_value_baseline_alert_message(finding)
