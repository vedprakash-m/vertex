"""ADF-W5.8 (Section 8.2.5): ``lineage_regression`` cockpit wiring.

Verifies the best-effort emission helper in ``src/commands/cockpit.py`` reads
the nearest retained history snapshot at/before the freshly-built snapshot's
own ``generated_at``, compares lineage coverage, and emits an entity-scoped
alert on a regression. The comparison logic itself is covered by
``test_lineage_regression_detector.py``; these tests verify the wiring.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.commands.cockpit import (
    _emit_lineage_regression_alert_best_effort,
    persist_cockpit_snapshot,
)
from src.core.alerts import read_alerts
from src.core.cockpit_models import (
    CockpitSnapshot,
    EconomicsCockpitSummary,
    IntelligenceCockpitSummary,
    ProgramCockpitSummary,
    ReliabilityCockpitSummary,
    SourceCockpitSummary,
    ValueCockpitSummary,
    finalize_cockpit_snapshot,
)

_T0 = datetime(2026, 7, 7, 10, 0, 0, tzinfo=timezone.utc)
_T1 = _T0 + timedelta(days=7)


def _snapshot(*, generated_at: datetime, lineage_coverage: float | None) -> CockpitSnapshot:
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
            lineage_coverage=lineage_coverage, verification_coverage=0.3,
            extraction_quality=(), contradiction_count=0,
        ),
        economics_summary=EconomicsCockpitSummary(
            frontier_avoidance=0.6, frontier_cost_usd=1.5, cache_hit_rate=0.2, context_tokens_in=100
        ),
        value_summary=ValueCockpitSummary(metrics=(), time_savings_certification=None),
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


def test_regression_over_budget_emits_alert(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    persist_cockpit_snapshot(_snapshot(generated_at=_T0, lineage_coverage=0.80), programs_root=programs_root)
    current = _snapshot(generated_at=_T1, lineage_coverage=0.50)

    _emit_lineage_regression_alert_best_effort("xpf", current, programs_root=programs_root)

    alerts = read_alerts("xpf", programs_root=programs_root)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.category == "lineage_regression"
    assert alert.entity_type == "cockpit_snapshot"
    assert alert.entity_id == "xpf"
    assert alert.severity == "warn"
    assert "Lineage regression" in alert.message
    assert "doctor" in alert.next_command.lower()


def test_small_drop_within_budget_emits_no_alert(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    persist_cockpit_snapshot(_snapshot(generated_at=_T0, lineage_coverage=0.80), programs_root=programs_root)
    current = _snapshot(generated_at=_T1, lineage_coverage=0.78)

    _emit_lineage_regression_alert_best_effort("xpf", current, programs_root=programs_root)
    assert read_alerts("xpf", programs_root=programs_root) == ()


def test_improved_coverage_emits_no_alert(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    persist_cockpit_snapshot(_snapshot(generated_at=_T0, lineage_coverage=0.50), programs_root=programs_root)
    current = _snapshot(generated_at=_T1, lineage_coverage=0.90)

    _emit_lineage_regression_alert_best_effort("xpf", current, programs_root=programs_root)
    assert read_alerts("xpf", programs_root=programs_root) == ()


def test_first_run_no_history_emits_no_alert(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    current = _snapshot(generated_at=_T0, lineage_coverage=0.10)

    _emit_lineage_regression_alert_best_effort("xpf", current, programs_root=programs_root)
    assert read_alerts("xpf", programs_root=programs_root) == ()


def test_current_coverage_none_emits_no_alert(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    persist_cockpit_snapshot(_snapshot(generated_at=_T0, lineage_coverage=0.80), programs_root=programs_root)
    current = _snapshot(generated_at=_T1, lineage_coverage=None)

    _emit_lineage_regression_alert_best_effort("xpf", current, programs_root=programs_root)
    assert read_alerts("xpf", programs_root=programs_root) == ()


def test_emission_is_idempotent_via_cooldown(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    persist_cockpit_snapshot(_snapshot(generated_at=_T0, lineage_coverage=0.80), programs_root=programs_root)
    current = _snapshot(generated_at=_T1, lineage_coverage=0.40)

    _emit_lineage_regression_alert_best_effort("xpf", current, programs_root=programs_root)
    first = read_alerts("xpf", programs_root=programs_root)
    assert len(first) == 1 and first[0].occurrence_count == 1

    _emit_lineage_regression_alert_best_effort("xpf", current, programs_root=programs_root)
    again = read_alerts("xpf", programs_root=programs_root)
    assert len(again) == 1
    assert again[0].suppressed_count >= 1


def test_lookup_ignores_history_after_the_snapshot_being_compared(tmp_path: Path) -> None:
    """The nearest-history lookup is bounded by ``at or before`` the current
    snapshot's own ``generated_at`` (Section 9's non-time-travel rule) -- a
    LATER persisted snapshot must never be treated as "prior" just because
    it happens to already be on disk when this comparison runs."""
    programs_root = _programs_root(tmp_path)
    persist_cockpit_snapshot(_snapshot(generated_at=_T0, lineage_coverage=0.80), programs_root=programs_root)
    # A snapshot generated AFTER _T0 but BEFORE the one being compared here.
    persist_cockpit_snapshot(_snapshot(generated_at=_T1, lineage_coverage=0.20), programs_root=programs_root)

    # Backdated compared-snapshot: its own generated_at is between T0 and T1,
    # so the nearest prior must be T0 (0.80), not T1 (0.20) which is later.
    backdated = _snapshot(generated_at=_T0 + timedelta(days=1), lineage_coverage=0.75)
    _emit_lineage_regression_alert_best_effort("xpf", backdated, programs_root=programs_root)
    # 0.80 -> 0.75 is a 5-point drop, exactly at the default budget -> no alert.
    assert read_alerts("xpf", programs_root=programs_root) == ()
