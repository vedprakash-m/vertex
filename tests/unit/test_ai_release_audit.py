"""ADF-W2.8 (specs/arch-data-fix.md Section 8.9.4, Appendix A.2): tests for
the AI Release Audit lifecycle -- QG-29's real implementation."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.quality_gates.ai_release_audit import (
    AIReleaseAuditError,
    AIRunState,
    ApplicationReceipt,
    ReleaseTerminal,
    assert_ai_output_released_or_raise,
    evaluate_ai_release_gate,
    is_ai_output_released,
    new_ai_run_id,
    record_ai_application_receipt,
    record_ai_release_decision,
    record_ai_run_lifecycle,
    released_terminal_for_run,
)
from src.core.ledger.event_log import read_events


def test_new_ai_run_id_is_unique() -> None:
    assert new_ai_run_id() != new_ai_run_id()


def test_no_terminal_decision_means_not_released(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ai_run_id = new_ai_run_id()
    assert released_terminal_for_run(ai_run_id, program_id="xpf", programs_root=programs_root) is None
    assert is_ai_output_released(ai_run_id, program_id="xpf", programs_root=programs_root) is False


def test_released_terminal_recorded_and_readable(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ai_run_id = new_ai_run_id()
    record_ai_release_decision(
        program_id="xpf", ai_run_id=ai_run_id, terminal=ReleaseTerminal.RELEASED,
        reason="all validators passed", validator_finding_count=0,
        released_content_hash="abc123", programs_root=programs_root,
    )
    assert released_terminal_for_run(ai_run_id, program_id="xpf", programs_root=programs_root) is ReleaseTerminal.RELEASED
    assert is_ai_output_released(ai_run_id, program_id="xpf", programs_root=programs_root) is True


def test_rejected_terminal_is_not_released(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ai_run_id = new_ai_run_id()
    record_ai_release_decision(
        program_id="xpf", ai_run_id=ai_run_id, terminal=ReleaseTerminal.REJECTED,
        reason="semantic validator found a prohibited claim", validator_finding_count=1,
        programs_root=programs_root,
    )
    assert is_ai_output_released(ai_run_id, program_id="xpf", programs_root=programs_root) is False


def test_assert_ai_output_released_or_raise_passes_when_released(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ai_run_id = new_ai_run_id()
    record_ai_release_decision(
        program_id="xpf", ai_run_id=ai_run_id, terminal=ReleaseTerminal.RELEASED,
        reason="ok", validator_finding_count=0, programs_root=programs_root,
    )
    assert_ai_output_released_or_raise(ai_run_id, program_id="xpf", programs_root=programs_root)  # must not raise


def test_assert_ai_output_released_or_raise_blocks_when_no_terminal(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ai_run_id = new_ai_run_id()
    with pytest.raises(AIReleaseAuditError, match="no terminal decision recorded"):
        assert_ai_output_released_or_raise(ai_run_id, program_id="xpf", programs_root=programs_root)


def test_assert_ai_output_released_or_raise_blocks_when_discarded(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ai_run_id = new_ai_run_id()
    record_ai_release_decision(
        program_id="xpf", ai_run_id=ai_run_id, terminal=ReleaseTerminal.DISCARDED,
        reason="schema validation failed twice", validator_finding_count=0, programs_root=programs_root,
    )
    with pytest.raises(AIReleaseAuditError, match="terminal=discarded"):
        assert_ai_output_released_or_raise(ai_run_id, program_id="xpf", programs_root=programs_root)


def test_evaluate_ai_release_gate_matches_qg29(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ai_run_id = new_ai_run_id()
    evaluation = evaluate_ai_release_gate(ai_run_id, program_id="xpf", programs_root=programs_root)
    assert evaluation.gate_id == "QG-29"
    assert evaluation.passed is False
    assert evaluation.forceable is False


def test_most_recent_terminal_wins_when_multiple_recorded(tmp_path: Path) -> None:
    """A retried/re-evaluated run can record more than one terminal over
    time (e.g. fallback then a later successful release); the most recent
    one is authoritative."""
    programs_root = tmp_path / "programs"
    ai_run_id = new_ai_run_id()
    record_ai_release_decision(
        program_id="xpf", ai_run_id=ai_run_id, terminal=ReleaseTerminal.FALLBACK,
        reason="first attempt used deterministic fallback", validator_finding_count=0, programs_root=programs_root,
    )
    record_ai_release_decision(
        program_id="xpf", ai_run_id=ai_run_id, terminal=ReleaseTerminal.RELEASED,
        reason="retry succeeded", validator_finding_count=0, programs_root=programs_root,
    )
    assert released_terminal_for_run(ai_run_id, program_id="xpf", programs_root=programs_root) is ReleaseTerminal.RELEASED


def test_different_ai_run_ids_are_isolated(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    released_run = new_ai_run_id()
    other_run = new_ai_run_id()
    record_ai_release_decision(
        program_id="xpf", ai_run_id=released_run, terminal=ReleaseTerminal.RELEASED,
        reason="ok", validator_finding_count=0, programs_root=programs_root,
    )
    assert is_ai_output_released(released_run, program_id="xpf", programs_root=programs_root) is True
    assert is_ai_output_released(other_run, program_id="xpf", programs_root=programs_root) is False


def test_record_ai_run_lifecycle_emits_valid_event(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ai_run_id = new_ai_run_id()
    record_ai_run_lifecycle(
        program_id="xpf", ai_run_id=ai_run_id, feature="decision_brief_advisor",
        state=AIRunState.PLANNED, prompt_version="v1", policy_version="p1",
        programs_root=programs_root,
    )
    events = read_events("xpf", programs_root=programs_root)
    lifecycle_events = [e for e in events if e.event_type == "ai.run_lifecycle.v1"]
    assert len(lifecycle_events) == 1
    assert lifecycle_events[0].payload["ai_run_id"] == ai_run_id
    assert lifecycle_events[0].payload["state"] == "planned"
    assert lifecycle_events[0].payload["feature"] == "decision_brief_advisor"


def test_record_ai_application_receipt_emits_valid_event(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ai_run_id = new_ai_run_id()
    record_ai_application_receipt(
        program_id="xpf", ai_run_id=ai_run_id, receipt=ApplicationReceipt.RENDERED,
        artifact_ref="narratives/issue_079/exec_summary.md", programs_root=programs_root,
    )
    events = read_events("xpf", programs_root=programs_root)
    receipt_events = [e for e in events if e.event_type == "ai.application_receipt.v1"]
    assert len(receipt_events) == 1
    assert receipt_events[0].payload["receipt"] == "rendered"
    assert receipt_events[0].payload["artifact_ref"] == "narratives/issue_079/exec_summary.md"


def test_full_lifecycle_sequence_end_to_end(tmp_path: Path) -> None:
    """A realistic sequence: plan -> request -> respond -> schema-validate
    -> semantically-validate -> release -> render receipt."""
    programs_root = tmp_path / "programs"
    ai_run_id = new_ai_run_id()

    for state in AIRunState:
        record_ai_run_lifecycle(
            program_id="xpf", ai_run_id=ai_run_id, feature="test_feature",
            state=state, prompt_version="v1", policy_version="p1", programs_root=programs_root,
        )

    with pytest.raises(AIReleaseAuditError):
        assert_ai_output_released_or_raise(ai_run_id, program_id="xpf", programs_root=programs_root)

    record_ai_release_decision(
        program_id="xpf", ai_run_id=ai_run_id, terminal=ReleaseTerminal.RELEASED,
        reason="all checks passed", validator_finding_count=0,
        released_content_hash="deadbeef", programs_root=programs_root,
    )
    assert_ai_output_released_or_raise(ai_run_id, program_id="xpf", programs_root=programs_root)

    record_ai_application_receipt(
        program_id="xpf", ai_run_id=ai_run_id, receipt=ApplicationReceipt.APPLIED,
        proposal_id="prop-1", programs_root=programs_root,
    )

    events = read_events("xpf", programs_root=programs_root)
    assert sum(1 for e in events if e.event_type == "ai.run_lifecycle.v1") == 5
    assert sum(1 for e in events if e.event_type == "ai.release_decision.v1") == 1
    assert sum(1 for e in events if e.event_type == "ai.application_receipt.v1") == 1
