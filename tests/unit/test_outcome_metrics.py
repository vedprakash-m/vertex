"""Unit tests for src/core/outcome_metrics.py (specs/backlog.md BL-C7).

OM-4 is the one metric with a real, live data source today (BL-C3's
ai_release_audit ledger events) -- these tests exercise the real
computation. OM-1/2/5 are honestly `unavailable`; tests here pin that they
never silently promote to a stronger confidence tier (INV-ADF-11).
"""

from __future__ import annotations

from pathlib import Path

from src.core.cockpit_models import ValueConfidence
from src.core.outcome_metrics import (
    CANARY_WINDOW_START,
    CANARY_WINDOW_WEEKS,
    canary_window_status,
    compute_all_outcome_metrics,
    compute_om1_hallucination_rate,
    compute_om2_duplicate_entities,
    compute_om4_audit_coverage,
    compute_om5_operator_friction,
)
from src.core.quality_gates.ai_release_audit import (
    AIRunState,
    ApplicationReceipt,
    ReleaseTerminal,
    new_ai_run_id,
    record_ai_application_receipt,
    record_ai_release_decision,
    record_ai_run_lifecycle,
)


def test_om1_is_always_unavailable_and_never_silently_promoted(tmp_path: Path) -> None:
    result = compute_om1_hallucination_rate("acme", programs_root=tmp_path / "programs")
    assert result.confidence == ValueConfidence.UNAVAILABLE
    assert result.value is None


def test_om2_is_unavailable_but_surfaces_duplicate_preventions_count(tmp_path: Path) -> None:
    result = compute_om2_duplicate_entities("acme", programs_root=tmp_path / "programs")
    assert result.confidence == ValueConfidence.UNAVAILABLE
    assert result.value is None
    assert "0 search-before-create prevention" in result.detail


def test_om5_is_always_unavailable(tmp_path: Path) -> None:
    result = compute_om5_operator_friction("acme", programs_root=tmp_path / "programs")
    assert result.confidence == ValueConfidence.UNAVAILABLE
    assert result.value is None


def test_om4_unavailable_when_no_ai_consumption_recorded(tmp_path: Path) -> None:
    result = compute_om4_audit_coverage("acme", programs_root=tmp_path / "programs")
    assert result.confidence == ValueConfidence.UNAVAILABLE
    assert result.value is None


def test_canary_window_not_elapsed_on_start_date() -> None:
    status = canary_window_status(today=CANARY_WINDOW_START)
    assert status.start_date == CANARY_WINDOW_START
    assert status.window_weeks == CANARY_WINDOW_WEEKS
    assert status.elapsed_weeks == 0.0
    assert status.elapsed is False


def test_canary_window_elapsed_exactly_at_eight_weeks() -> None:
    from datetime import timedelta

    eight_weeks_later = CANARY_WINDOW_START + timedelta(weeks=CANARY_WINDOW_WEEKS)
    status = canary_window_status(today=eight_weeks_later)
    assert status.elapsed_weeks == CANARY_WINDOW_WEEKS
    assert status.elapsed is True


def test_canary_window_partway_reports_fractional_weeks_not_yet_elapsed() -> None:
    from datetime import timedelta

    halfway = CANARY_WINDOW_START + timedelta(weeks=4)
    status = canary_window_status(today=halfway)
    assert status.elapsed_weeks == 4.0
    assert status.elapsed is False


def test_canary_window_defaults_to_real_today_when_not_supplied() -> None:
    status = canary_window_status()
    assert status.elapsed_weeks >= 0.0


def _record_full_run(
    *, program_id: str, programs_root: Path, terminal: ReleaseTerminal, receipt: ApplicationReceipt
) -> str:
    ai_run_id = new_ai_run_id()
    record_ai_run_lifecycle(
        program_id=program_id,
        ai_run_id=ai_run_id,
        feature="claim_extractor",
        state=AIRunState.SEMANTICALLY_VALIDATED,
        prompt_version="claim_extractor.v1",
        policy_version="v1",
        programs_root=programs_root,
    )
    record_ai_release_decision(
        program_id=program_id,
        ai_run_id=ai_run_id,
        terminal=terminal,
        reason="test",
        validator_finding_count=0,
        programs_root=programs_root,
    )
    record_ai_application_receipt(
        program_id=program_id,
        ai_run_id=ai_run_id,
        receipt=receipt,
        programs_root=programs_root,
    )
    return ai_run_id


def test_om4_measures_full_coverage_when_all_consumed_runs_are_released(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _record_full_run(
        program_id="acme", programs_root=programs_root,
        terminal=ReleaseTerminal.RELEASED, receipt=ApplicationReceipt.APPLIED,
    )
    _record_full_run(
        program_id="acme", programs_root=programs_root,
        terminal=ReleaseTerminal.RELEASED, receipt=ApplicationReceipt.RENDERED,
    )

    result = compute_om4_audit_coverage("acme", programs_root=programs_root)

    assert result.confidence == ValueConfidence.MEASURED
    assert result.value == 1.0
    assert "unaudited" not in result.detail
    assert len(result.evidence_refs) == 2


def test_om4_detects_unaudited_consumption(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _record_full_run(
        program_id="acme", programs_root=programs_root,
        terminal=ReleaseTerminal.RELEASED, receipt=ApplicationReceipt.APPLIED,
    )
    # A consumed run whose terminal decision was REJECTED, not RELEASED --
    # this should never have been consumed; OM-4 must count it as unaudited.
    _record_full_run(
        program_id="acme", programs_root=programs_root,
        terminal=ReleaseTerminal.REJECTED, receipt=ApplicationReceipt.APPLIED,
    )

    result = compute_om4_audit_coverage("acme", programs_root=programs_root)

    assert result.confidence == ValueConfidence.MEASURED
    assert result.value == 0.5
    assert "1 unaudited" in result.detail


def test_om4_ignores_non_consumed_receipts(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _record_full_run(
        program_id="acme", programs_root=programs_root,
        terminal=ReleaseTerminal.DISCARDED, receipt=ApplicationReceipt.NOT_APPLIED,
    )

    result = compute_om4_audit_coverage("acme", programs_root=programs_root)

    # not_applied means nothing was consumed -- correctly unavailable, not
    # a false "0/0 -> 100%" or a false "unaudited" reading.
    assert result.confidence == ValueConfidence.UNAVAILABLE


def test_compute_all_outcome_metrics_returns_om1_2_4_5_in_order(tmp_path: Path) -> None:
    results = compute_all_outcome_metrics("acme", programs_root=tmp_path / "programs")
    assert [r.metric_id for r in results] == [
        "om1_hallucination_rate",
        "om2_duplicate_entities",
        "om4_audit_coverage",
        "om5_operator_friction",
    ]
