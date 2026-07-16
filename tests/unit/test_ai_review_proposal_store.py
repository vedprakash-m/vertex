from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src.core.ai_review_proposal_store import (
    AIReviewProposalStoreError,
    load_proposal,
    load_proposals,
    stage_proposal,
)
from src.core.dependency_blast_radius import DependencyBlastRadiusProposal
from src.core.governance_decision_brief import GovernanceDecisionBriefProposal, GovernanceDecisionOption
from src.core.meeting_action import MeetingAction
from src.core.models_v2 import RiskCategory, RiskImpact, RiskProbability
from src.core.risk_proposal import RiskProposal
from src.core.top_three_candidates import TopThreeCandidateProposal


def _risk_proposal(**overrides: object) -> RiskProposal:
    defaults: dict[str, object] = dict(
        id="risk-proposal-1",
        program_id="acme",
        candidate_risk_id="risk-candidate-1",
        causal_title="Vendor SDK delay blocks integration milestone",
        why_it_matters="The integration milestone cannot complete without the vendor SDK.",
        probability=RiskProbability.LIKELY,
        impact=RiskImpact.HIGH,
        category=RiskCategory.EXTERNAL,
        mitigation="Escalate to vendor account team.",
        owner_alias="jordanr",
        by_when=date(2026, 8, 1),
        fallback="Build an internal shim if vendor slips past July.",
        evidence_refs=("signal-1", "signal-2"),
        ai_run_id="ai-run-1",
        proposed_at=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return RiskProposal(**defaults)  # type: ignore[arg-type]


def _meeting_action(**overrides: object) -> MeetingAction:
    defaults: dict[str, object] = dict(
        id="action-1",
        program_id="acme",
        meeting_ref="2026-07-01-standup",
        commitment="Send the updated timeline to stakeholders.",
        owner_alias="jordanr",
        due_date=date(2026, 7, 10),
        linked_work_item_id=1234,
        blocks=("WI:5678",),
        source_span="Action: Send the updated timeline | owner=jordanr | due=2026-07-10 | wi=1234 | blocks=WI:5678",
        extraction_method="deterministic",
        proposed_at=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return MeetingAction(**defaults)  # type: ignore[arg-type]


def _top_three(**overrides: object) -> TopThreeCandidateProposal:
    defaults: dict[str, object] = dict(
        id="top3-1",
        program_id="acme",
        item_id="risk-candidate-1",
        reason="Highest-severity open risk this cycle.",
        evidence_refs=("signal-1",),
        urgency="high",
        decision_or_action_needed="Escalate vendor timeline.",
        owner_alias="jordanr",
        confidence="high",
        ai_run_id="ai-run-2",
        proposed_at=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return TopThreeCandidateProposal(**defaults)  # type: ignore[arg-type]


def _governance_brief(**overrides: object) -> GovernanceDecisionBriefProposal:
    defaults: dict[str, object] = dict(
        id="brief-1",
        program_id="acme",
        decision_ask_id="ask-1",
        decision="Should we switch vendors?",
        context="Vendor has slipped twice this quarter.",
        options=(
            GovernanceDecisionOption(label="Switch vendors", tradeoffs="6-week onboarding delay."),
            GovernanceDecisionOption(label="Stay with current vendor", tradeoffs="Continued schedule risk."),
        ),
        recommendation="Switch vendors given repeated slips.",
        consequences_of_delay="Milestone slips a further 4 weeks.",
        owner_alias="jordanr",
        due_date=date(2026, 7, 15),
        evidence_refs=("signal-3",),
        ai_run_id="ai-run-3",
        proposed_at=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return GovernanceDecisionBriefProposal(**defaults)  # type: ignore[arg-type]


def _blast_radius(**overrides: object) -> DependencyBlastRadiusProposal:
    defaults: dict[str, object] = dict(
        id="blast-1",
        program_id="acme",
        dependency_id="dep-1",
        next_proving_event="Vendor integration test on 2026-07-20.",
        blast_radius_narrative="A missed vendor delivery cascades into the GA milestone.",
        evidence_refs=("signal-4",),
        ai_run_id="ai-run-4",
        proposed_at=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return DependencyBlastRadiusProposal(**defaults)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("proposal_type", "factory"),
    [
        ("risk", _risk_proposal),
        ("meeting_action", _meeting_action),
        ("top_three", _top_three),
        ("governance_decision_brief", _governance_brief),
        ("dependency_blast_radius", _blast_radius),
    ],
)
def test_stage_and_load_round_trips_every_field(tmp_path: Path, proposal_type: str, factory) -> None:
    programs_root = tmp_path / "programs"
    proposal = factory()

    stage_proposal("acme", proposal_type, proposal, programs_root=programs_root)  # type: ignore[arg-type]
    loaded = load_proposals("acme", proposal_type=proposal_type, programs_root=programs_root)  # type: ignore[arg-type]

    assert len(loaded) == 1
    assert loaded[0] == proposal


def test_load_proposals_filters_by_status(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    staged = _risk_proposal(id="risk-staged")
    approved = _risk_proposal(id="risk-approved", status="approved", decided_at=datetime(2026, 7, 2, tzinfo=timezone.utc))

    stage_proposal("acme", "risk", staged, programs_root=programs_root)
    stage_proposal("acme", "risk", approved, programs_root=programs_root)

    pending_only = load_proposals("acme", proposal_type="risk", status_filter={"staged"}, programs_root=programs_root)
    assert {proposal.id for proposal in pending_only} == {"risk-staged"}


def test_re_staging_same_id_supersedes_prior_status_without_losing_history(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    staged = _meeting_action(id="action-1")
    approved = _meeting_action(id="action-1", status="approved", decided_at=datetime(2026, 7, 2, tzinfo=timezone.utc))

    stage_proposal("acme", "meeting_action", staged, programs_root=programs_root)
    stage_proposal("acme", "meeting_action", approved, programs_root=programs_root)

    loaded = load_proposals("acme", proposal_type="meeting_action", programs_root=programs_root)
    assert len(loaded) == 1
    assert loaded[0].status == "approved"

    # The raw JSONL file still has both appended lines (append-only history).
    raw_path = programs_root / "acme" / "journal" / "ai_review_proposals.jsonl"
    assert len(raw_path.read_text(encoding="utf-8").splitlines()) == 2


def test_load_proposals_without_type_filter_returns_all_five_types(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    stage_proposal("acme", "risk", _risk_proposal(), programs_root=programs_root)
    stage_proposal("acme", "meeting_action", _meeting_action(), programs_root=programs_root)
    stage_proposal("acme", "top_three", _top_three(), programs_root=programs_root)
    stage_proposal("acme", "governance_decision_brief", _governance_brief(), programs_root=programs_root)
    stage_proposal("acme", "dependency_blast_radius", _blast_radius(), programs_root=programs_root)

    loaded = load_proposals("acme", programs_root=programs_root)
    assert len(loaded) == 5


def test_load_proposal_returns_single_match_by_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    stage_proposal("acme", "risk", _risk_proposal(id="risk-x"), programs_root=programs_root)

    found = load_proposal("acme", "risk", "risk-x", programs_root=programs_root)
    assert found is not None
    assert found.id == "risk-x"

    missing = load_proposal("acme", "risk", "does-not-exist", programs_root=programs_root)
    assert missing is None


def test_stage_proposal_rejects_unknown_proposal_type(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    with pytest.raises(AIReviewProposalStoreError):
        stage_proposal("acme", "not_a_real_type", _risk_proposal(), programs_root=programs_root)  # type: ignore[arg-type]


def test_stage_proposal_rejects_program_id_mismatch(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposal = _risk_proposal(program_id="other-program")
    with pytest.raises(AIReviewProposalStoreError):
        stage_proposal("acme", "risk", proposal, programs_root=programs_root)


def test_load_proposals_returns_empty_tuple_when_no_file_exists(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    assert load_proposals("acme", programs_root=programs_root) == ()
