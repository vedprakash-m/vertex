"""ADF-W5.12: src/core/proposal_autonomy_ladder.py."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.maturity_engine import ProposalClassCounters, load_earned_autonomy_state
from src.core.proposal_audit import record_proposal_event
from src.core.proposal_autonomy_ladder import (
    PROPOSAL_CLASSES,
    advance_proposal_class_autonomy,
    compute_proposal_class_counters,
    demote_proposal_class_explicit,
    evaluate_promotion,
    promote_proposal_class_explicit,
    resolve_ceiling,
)

_NOW = datetime(2026, 7, 13, tzinfo=timezone.utc)


def test_proposal_classes_match_proposal_audit_types() -> None:
    assert set(PROPOSAL_CLASSES) == {
        "risk", "meeting_action", "top_three", "governance_decision_brief", "dependency_blast_radius",
    }


def test_l0_promotes_to_l1_with_no_evidence_needed() -> None:
    result = evaluate_promotion(
        "risk", current_level="l0", counters=ProposalClassCounters(), ceiling="l2"
    )
    assert result.action == "promoted"
    assert result.proposed_level == "l1"


def test_l1_stays_unchanged_below_minimum_population() -> None:
    counters = ProposalClassCounters(proposals=3, accepted=3)
    result = evaluate_promotion("risk", current_level="l1", counters=counters, ceiling="l2")
    assert result.action == "unchanged"
    assert result.proposed_level == "l1"


def test_l1_promotes_to_l2_with_sufficient_population_and_acceptance() -> None:
    counters = ProposalClassCounters(proposals=12, accepted=11, rejected=1)
    result = evaluate_promotion("risk", current_level="l1", counters=counters, ceiling="l2")
    assert result.action == "promoted"
    assert result.proposed_level == "l2"


def test_l1_stays_unchanged_when_acceptance_rate_too_low_for_l2_but_above_demotion_floor() -> None:
    # 8/12 = 0.667: above the 0.60 demotion floor, below the 0.90 L2 promotion floor.
    counters = ProposalClassCounters(proposals=12, accepted=8, rejected=4)
    result = evaluate_promotion("risk", current_level="l1", counters=counters, ceiling="l2")
    assert result.action == "unchanged"
    assert result.proposed_level == "l1"


def test_never_promotes_past_governance_ceiling() -> None:
    counters = ProposalClassCounters(proposals=12, accepted=12)
    result = evaluate_promotion("risk", current_level="l2", counters=counters, ceiling="l2")
    assert result.action == "unchanged"
    assert "ceiling" in result.reason


def test_l2_never_auto_promotes_to_l3() -> None:
    counters = ProposalClassCounters(proposals=100, accepted=100)
    result = evaluate_promotion("risk", current_level="l2", counters=counters, ceiling="l4")
    assert result.action == "unchanged"
    assert "human-gated" in result.reason


def test_auto_demotes_on_low_acceptance_rate_with_sufficient_population() -> None:
    counters = ProposalClassCounters(proposals=12, accepted=3, rejected=9)
    result = evaluate_promotion("risk", current_level="l2", counters=counters, ceiling="l2")
    assert result.action == "demoted"
    assert result.proposed_level == "l1"


def test_no_demotion_below_minimum_population_even_with_bad_acceptance() -> None:
    counters = ProposalClassCounters(proposals=2, accepted=0, rejected=2)
    result = evaluate_promotion("risk", current_level="l2", counters=counters, ceiling="l2")
    assert result.action != "demoted"


def test_compute_proposal_class_counters_aggregates_audit_trail(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    record_proposal_event(program_id="xpf", proposal_type="risk", proposal_id="r1", event="proposed", programs_root=programs_root, at=_NOW)
    record_proposal_event(program_id="xpf", proposal_type="risk", proposal_id="r1", event="approved", programs_root=programs_root, at=_NOW)
    record_proposal_event(program_id="xpf", proposal_type="risk", proposal_id="r2", event="proposed", programs_root=programs_root, at=_NOW)
    record_proposal_event(program_id="xpf", proposal_type="risk", proposal_id="r2", event="rejected", programs_root=programs_root, at=_NOW)
    record_proposal_event(program_id="xpf", proposal_type="meeting_action", proposal_id="m1", event="approved", programs_root=programs_root, at=_NOW)

    counters = compute_proposal_class_counters("xpf", "risk", programs_root=programs_root)
    assert counters.proposals == 2
    assert counters.accepted == 1
    assert counters.rejected == 1
    assert counters.edited == 0
    assert counters.reversals == 0


def test_advance_proposal_class_autonomy_persists_state(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    evaluation = advance_proposal_class_autonomy("xpf", "risk", now=_NOW, programs_root=programs_root)
    assert evaluation.action == "promoted"
    assert evaluation.proposed_level == "l1"

    state = load_earned_autonomy_state("xpf", programs_root=programs_root)
    assert state is not None
    assert state.proposal_classes["risk"].level == "l1"


def test_advance_is_idempotent_once_at_ceiling(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    for i in range(12):
        record_proposal_event(program_id="xpf", proposal_type="risk", proposal_id=f"r{i}", event="proposed", programs_root=programs_root, at=_NOW)
        record_proposal_event(program_id="xpf", proposal_type="risk", proposal_id=f"r{i}", event="approved", programs_root=programs_root, at=_NOW)

    advance_proposal_class_autonomy("xpf", "risk", now=_NOW, programs_root=programs_root)  # l0->l1
    advance_proposal_class_autonomy("xpf", "risk", now=_NOW, programs_root=programs_root)  # l1->l2
    third = advance_proposal_class_autonomy("xpf", "risk", now=_NOW, programs_root=programs_root)  # ceiling
    assert third.action == "unchanged"
    assert third.proposed_level == "l2"


def test_promote_explicit_respects_ceiling(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    with pytest.raises(ValueError, match="exceeds governance ceiling"):
        promote_proposal_class_explicit("xpf", "risk", "l4", "testing", now=_NOW, programs_root=programs_root)


def test_demote_explicit_never_goes_below_l0(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    entry = demote_proposal_class_explicit("xpf", "risk", "manual test demotion", now=_NOW, programs_root=programs_root)
    assert entry.level == "l0"


def test_resolve_ceiling_defaults_when_unconfigured(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    assert resolve_ceiling("risk", program_id="xpf", programs_root=programs_root) == "l2"


def test_other_proposal_classes_unaffected_by_one_class_evaluation(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    advance_proposal_class_autonomy("xpf", "risk", now=_NOW, programs_root=programs_root)
    state = load_earned_autonomy_state("xpf", programs_root=programs_root)
    assert "meeting_action" not in state.proposal_classes
