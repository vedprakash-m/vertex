"""RELO-D19 replacement for ungoverned FR-SG-37 proposal YAML."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.commands.confirm_stages import optimization_proposals
from src.commands.confirm_stages.optimization_proposals import write_optimization_proposals
from src.core.governed_feedback_lifecycle import (
    ProposalStatus,
    load_feedback_observations,
    load_feedback_proposals,
    reject_feedback_proposal,
)


def _streak(dimension: str, override_value: str, streak_count: int = 3):
    return SimpleNamespace(
        dimension=dimension,
        override_value=override_value,
        original_value="Low",
        streak_count=streak_count,
    )


def test_no_streaks_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(optimization_proposals, "get_override_streaks", lambda *a, **k: ())
    write_optimization_proposals(
        "acme",
        edition_id="acme_weekly",
        issue_number=1,
        approved_by="synthetic-operator",
        programs_root=tmp_path,
    )
    assert load_feedback_observations("acme", programs_root=tmp_path) == ()
    assert load_feedback_proposals("acme", programs_root=tmp_path) == ()


def test_writes_proposal_for_streak(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        optimization_proposals,
        "get_override_streaks",
        lambda *a, **k: (_streak("Velocity", "High", 4),),
    )
    write_optimization_proposals(
        "acme",
        edition_id="acme_weekly",
        issue_number=2,
        approved_by="synthetic-operator",
        programs_root=tmp_path,
    )
    proposals = load_feedback_proposals("acme", programs_root=tmp_path)
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.subject == "derivation-adjustment:Velocity:High"
    assert proposal.proposed_value.value == "High"
    assert proposal.status is ProposalStatus.PROPOSED
    assert not (tmp_path / "acme" / "_feedback" / "derivation_adjustments.yaml").exists()
    observations = load_feedback_observations("acme", programs_root=tmp_path)
    assert len(observations) == 1
    assert observations[0].approved_by == "synthetic-operator"


def test_idempotent_does_not_duplicate_pending(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        optimization_proposals,
        "get_override_streaks",
        lambda *a, **k: (_streak("Velocity", "High"),),
    )
    write_optimization_proposals(
        "acme", edition_id="acme_weekly", issue_number=2,
        approved_by="synthetic-operator", programs_root=tmp_path,
    )
    write_optimization_proposals(
        "acme", edition_id="acme_weekly", issue_number=3,
        approved_by="synthetic-operator", programs_root=tmp_path,
    )
    assert len(load_feedback_proposals("acme", programs_root=tmp_path)) == 1


def test_appends_new_distinct_proposal(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        optimization_proposals,
        "get_override_streaks",
        lambda *a, **k: (_streak("Velocity", "High"),),
    )
    write_optimization_proposals(
        "acme", edition_id="acme_weekly", issue_number=2,
        approved_by="synthetic-operator", programs_root=tmp_path,
    )
    monkeypatch.setattr(
        optimization_proposals,
        "get_override_streaks",
        lambda *a, **k: (_streak("Risk", "Blocked"),),
    )
    write_optimization_proposals(
        "acme", edition_id="acme_weekly", issue_number=3,
        approved_by="synthetic-operator", programs_root=tmp_path,
    )
    assert {
        proposal.subject for proposal in load_feedback_proposals("acme", programs_root=tmp_path)
    } == {
        "derivation-adjustment:Velocity:High",
        "derivation-adjustment:Risk:Blocked",
    }


def test_rejected_existing_does_not_block_new_version(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        optimization_proposals,
        "get_override_streaks",
        lambda *a, **k: (_streak("Velocity", "High"),),
    )
    write_optimization_proposals(
        "acme", edition_id="acme_weekly", issue_number=2,
        approved_by="synthetic-operator", programs_root=tmp_path,
    )
    initial = load_feedback_proposals("acme", programs_root=tmp_path)[0]
    reject_feedback_proposal(
        "acme",
        proposal_id=initial.proposal_id,
        rejected_by="synthetic-operator",
        reason="needs new evidence",
        rejected_at=initial.created_at,
        programs_root=tmp_path,
    )
    write_optimization_proposals(
        "acme", edition_id="acme_weekly", issue_number=4,
        approved_by="synthetic-operator", programs_root=tmp_path,
    )
    proposals = load_feedback_proposals("acme", programs_root=tmp_path)
    assert len(proposals) == 2
    assert max(proposal.version for proposal in proposals) == 2
