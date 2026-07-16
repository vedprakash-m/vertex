"""ADF-W5.12 (Appendix A.8): the proposal_classes block additively
extending src/core/maturity_engine.py's earned_autonomy_state.yaml."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.core.maturity_engine import (
    EarnedAutonomyState,
    ProposalClassAutonomyState,
    ProposalClassCounters,
    load_earned_autonomy_state,
    try_advance_earned_autonomy,
    write_proposal_class_state,
)

_NOW = datetime(2026, 7, 13, tzinfo=timezone.utc)


def test_absent_proposal_classes_block_defaults_to_empty(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    assert load_earned_autonomy_state("xpf", programs_root=programs_root) is None


def test_write_proposal_class_state_round_trips(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    entry = ProposalClassAutonomyState(
        level="l2",
        promoted_at=_NOW,
        demoted_at=None,
        last_change_reason="test promotion",
        evidence_window_start=_NOW,
        counters=ProposalClassCounters(proposals=12, accepted=11, rejected=1),
        sample_rate=0.5,
    )
    write_proposal_class_state("xpf", "risk", entry, programs_root=programs_root)

    state = load_earned_autonomy_state("xpf", programs_root=programs_root)
    assert state is not None
    assert state.proposal_classes["risk"] == entry


def test_write_proposal_class_state_preserves_other_classes(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    entry_a = ProposalClassAutonomyState(
        level="l1", promoted_at=_NOW, demoted_at=None, last_change_reason="a",
        evidence_window_start=_NOW,
    )
    entry_b = ProposalClassAutonomyState(
        level="l2", promoted_at=_NOW, demoted_at=None, last_change_reason="b",
        evidence_window_start=_NOW,
    )
    write_proposal_class_state("xpf", "risk", entry_a, programs_root=programs_root)
    write_proposal_class_state("xpf", "meeting_action", entry_b, programs_root=programs_root)

    state = load_earned_autonomy_state("xpf", programs_root=programs_root)
    assert state.proposal_classes["risk"].level == "l1"
    assert state.proposal_classes["meeting_action"].level == "l2"


def test_write_proposal_class_state_preserves_existing_global_tier_fields(tmp_path: Path) -> None:
    """FR-SG-39's earned_tier/maturity_score must survive a proposal_classes
    write untouched -- these are a materially different, older mechanism."""
    programs_root = tmp_path / "programs"
    try_advance_earned_autonomy("xpf", programs_root=programs_root)  # initializes earned_tier=0
    before = load_earned_autonomy_state("xpf", programs_root=programs_root)

    entry = ProposalClassAutonomyState(
        level="l1", promoted_at=_NOW, demoted_at=None, last_change_reason="x",
        evidence_window_start=_NOW,
    )
    write_proposal_class_state("xpf", "risk", entry, programs_root=programs_root)

    after = load_earned_autonomy_state("xpf", programs_root=programs_root)
    assert after.earned_tier == before.earned_tier
    assert after.maturity_score == before.maturity_score
    assert after.proposal_classes["risk"].level == "l1"


def test_default_counters_are_all_zero() -> None:
    counters = ProposalClassCounters()
    assert counters.proposals == counters.accepted == counters.edited == 0
    assert counters.rejected == counters.reversals == counters.material_errors == 0
