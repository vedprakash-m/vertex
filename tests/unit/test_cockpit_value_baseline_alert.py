"""ADF-W5.8 (Section 8.2.5): ``value_baseline_expired_or_incomparable``
cockpit wiring.

Verifies the best-effort emission helper in ``src/commands/cockpit.py``
evaluates the freshly-built snapshot's ``value_summary`` measured metrics and
emits an entity-scoped alert when a measured baseline is expired or
incomparable. The comparison logic is covered by
``test_value_baseline_freshness_detector.py``; these tests verify the wiring.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.commands.cockpit import _emit_value_baseline_freshness_alert_best_effort
from src.core.alerts import read_alerts
from src.core.cockpit_models import (
    CockpitSnapshot,
    EconomicsCockpitSummary,
    IntelligenceCockpitSummary,
    ProgramCockpitSummary,
    ReliabilityCockpitSummary,
    SourceCockpitSummary,
    TimeSavingsCertification,
    ValueCockpitSummary,
    ValueConfidence,
    ValueMetric,
    finalize_cockpit_snapshot,
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
        program_id="xpf",
        edition_id="xpf_weekly",
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


def _snapshot(*, value_summary: ValueCockpitSummary, generated_at: datetime = _NOW) -> CockpitSnapshot:
    snap = CockpitSnapshot(
        schema_version="1", program_id="xpf", edition_id="xpf_weekly",
        generated_at=generated_at, as_of=generated_at,
        program_summary=ProgramCockpitSummary(
            overall_risk="green", readiness_percent=50, blocker_count=0,
            top_three_candidates=(), next_action=None,
        ),
        source_summary=SourceCockpitSummary(
            required_healthy=5, required_total=5, stale_sources=(), degraded_sources=(),
            manual_sources=(), newest_watermarks={},
        ),
        intelligence_summary=IntelligenceCockpitSummary(
            lineage_coverage=0.8, verification_coverage=0.3,
            extraction_quality=(), contradiction_count=0,
        ),
        economics_summary=EconomicsCockpitSummary(
            frontier_avoidance=0.6, frontier_cost_usd=1.5, cache_hit_rate=0.2, context_tokens_in=100
        ),
        value_summary=value_summary,
        reliability_summary=ReliabilityCockpitSummary(
            outbox_pending=0, uncertain_remote_state=0, dead_letter_count=0,
            duplicate_preventions=0, audit_coverage=None,
        ),
        findings=(),
        input_hash="",
    )
    return finalize_cockpit_snapshot(snap)


def _programs_root(tmp_path: Path) -> Path:
    programs_root = tmp_path / "programs"
    (programs_root / "xpf").mkdir(parents=True)
    return programs_root


# ---------------------------------------------------------------------------
# Expired
# ---------------------------------------------------------------------------


def test_expired_measured_metric_emits_alert(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    metric = _metric("report_wall_time_seconds", period_end=_NOW - timedelta(days=120))
    snapshot = _snapshot(value_summary=ValueCockpitSummary(metrics=(metric,), time_savings_certification=None))

    _emit_value_baseline_freshness_alert_best_effort("xpf", snapshot, programs_root=programs_root)

    alerts = read_alerts("xpf", programs_root=programs_root)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.category == "value_baseline_expired_or_incomparable"
    assert alert.entity_type == "value_summary"
    assert alert.entity_id == "xpf"
    assert alert.severity == "warn"
    assert "expired" in alert.message.lower()
    assert "report_wall_time_seconds" in alert.message
    assert "cockpit show" in alert.next_command.lower()


def test_fresh_measured_metric_emits_no_alert(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    metric = _metric("report_wall_time_seconds", period_end=_NOW - timedelta(days=30))
    snapshot = _snapshot(value_summary=ValueCockpitSummary(metrics=(metric,), time_savings_certification=None))

    _emit_value_baseline_freshness_alert_best_effort("xpf", snapshot, programs_root=programs_root)
    assert read_alerts("xpf", programs_root=programs_root) == ()


def test_calibrated_metric_never_expires(tmp_path: Path) -> None:
    # A calibrated metric is already honestly labeled; expiry does not apply.
    programs_root = _programs_root(tmp_path)
    metric = _metric(
        "report_wall_time_seconds",
        confidence=ValueConfidence.CALIBRATED,
        period_end=_NOW - timedelta(days=120),
    )
    snapshot = _snapshot(value_summary=ValueCockpitSummary(metrics=(metric,), time_savings_certification=None))

    _emit_value_baseline_freshness_alert_best_effort("xpf", snapshot, programs_root=programs_root)
    assert read_alerts("xpf", programs_root=programs_root) == ()


def test_empty_value_summary_emits_no_alert(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    snapshot = _snapshot(value_summary=ValueCockpitSummary(metrics=(), time_savings_certification=None))

    _emit_value_baseline_freshness_alert_best_effort("xpf", snapshot, programs_root=programs_root)
    assert read_alerts("xpf", programs_root=programs_root) == ()


# ---------------------------------------------------------------------------
# Incomparable certification
# ---------------------------------------------------------------------------


def _cert(*, manual: int, vertex: int) -> TimeSavingsCertification:
    return TimeSavingsCertification(
        schema_version="1",
        program_id="xpf",
        edition_id="xpf_weekly",
        workflow="weekly_issue",
        manual_sample_count=manual,
        vertex_sample_count=vertex,
        manual_median_active_seconds=600.0,
        vertex_median_active_seconds=120.0,
        savings_ratio=0.80,
        confidence_interval_low=0.75,
    )


def test_under_evidenced_certification_emits_alert(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    snapshot = _snapshot(
        value_summary=ValueCockpitSummary(
            metrics=(), time_savings_certification=_cert(manual=3, vertex=5)
        )
    )

    _emit_value_baseline_freshness_alert_best_effort("xpf", snapshot, programs_root=programs_root)

    alerts = read_alerts("xpf", programs_root=programs_root)
    assert len(alerts) == 1
    assert "incomparable" in alerts[0].message.lower()
    assert "weekly_issue" in alerts[0].message


def test_well_evidenced_certification_emits_no_alert(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    snapshot = _snapshot(
        value_summary=ValueCockpitSummary(
            metrics=(), time_savings_certification=_cert(manual=10, vertex=10)
        )
    )

    _emit_value_baseline_freshness_alert_best_effort("xpf", snapshot, programs_root=programs_root)
    assert read_alerts("xpf", programs_root=programs_root) == ()


# ---------------------------------------------------------------------------
# Idempotency / cooldown
# ---------------------------------------------------------------------------


def test_emission_is_idempotent_via_cooldown(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    metric = _metric("report_wall_time_seconds", period_end=_NOW - timedelta(days=120))
    snapshot = _snapshot(value_summary=ValueCockpitSummary(metrics=(metric,), time_savings_certification=None))

    _emit_value_baseline_freshness_alert_best_effort("xpf", snapshot, programs_root=programs_root)
    first = read_alerts("xpf", programs_root=programs_root)
    assert len(first) == 1 and first[0].occurrence_count == 1

    _emit_value_baseline_freshness_alert_best_effort("xpf", snapshot, programs_root=programs_root)
    again = read_alerts("xpf", programs_root=programs_root)
    assert len(again) == 1
    assert again[0].suppressed_count >= 1
