"""ADF-W4.7 remainder: unit tests for src/core/governance_decision_brief.py."""

from __future__ import annotations

from datetime import date

import pytest

from src.core.governance_decision_brief import (
    GovernanceDecisionBriefError,
    GovernanceDecisionBriefProposal,
    GovernanceDecisionOption,
    approve_governance_decision_brief,
    assemble_governance_decision_request,
    reject_governance_decision_brief,
)
from src.core.models_v2 import DecisionAsk


def _open_ask(*, ask_id: str = "ask-1") -> DecisionAsk:
    return DecisionAsk(
        id=ask_id,
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=100,
        text="Should we escalate the vendor delay to leadership?",
        entity_refs=("sig-1", "sig-2"),
        ask_date=date(2026, 7, 1),
        owner_alias="alex",
    )


def _proposal(*, status: str = "staged") -> GovernanceDecisionBriefProposal:
    return GovernanceDecisionBriefProposal(
        id="governance-decision-1",
        program_id="acme",
        decision_ask_id="ask-1",
        decision="Escalate the vendor delay to leadership.",
        context="Vendor X has slipped its delivery date twice.",
        options=(
            GovernanceDecisionOption(
                label="Escalate now",
                tradeoffs="Faster resolution, may strain the relationship.",
                evidence_refs=("sig-1",),
            ),
            GovernanceDecisionOption(
                label="Wait one more sprint",
                tradeoffs="Preserves the relationship, risks the milestone.",
                unknowns=("Whether the vendor's next slip risk is lower this sprint.",),
            ),
        ),
        recommendation="Escalate now given the milestone risk.",
        consequences_of_delay="The Q3 milestone slips further with each week of delay.",
        owner_alias="alex",
        due_date=date(2026, 7, 15),
        evidence_refs=("sig-1",),
        ai_run_id="run-1",
        status=status,  # type: ignore[arg-type]
        follow_up_action="Send the escalation email to leadership by Friday.",
    )


def test_assemble_request_from_open_ask() -> None:
    request = assemble_governance_decision_request(_open_ask(), evidence_texts=("Vendor confirmed delay.",))
    assert request.decision_ask_id == "ask-1"
    assert request.decision_text == "Should we escalate the vendor delay to leadership?"
    assert request.evidence_refs == ("sig-1", "sig-2")
    assert request.evidence_texts == ("Vendor confirmed delay.",)


def test_assemble_request_rejects_resolved_ask() -> None:
    from dataclasses import replace

    resolved = replace(_open_ask(), status="resolved")
    with pytest.raises(GovernanceDecisionBriefError, match="not 'open'"):
        assemble_governance_decision_request(resolved)


def test_approve_staged_proposal() -> None:
    approved = approve_governance_decision_brief(_proposal())
    assert approved.status == "approved"


def test_approve_rejected_proposal_raises() -> None:
    rejected = reject_governance_decision_brief(_proposal(), reason="not material enough")
    with pytest.raises(GovernanceDecisionBriefError, match="rejected"):
        approve_governance_decision_brief(rejected)


def test_reject_records_reason() -> None:
    rejected = reject_governance_decision_brief(_proposal(), reason="duplicate of ask-9")
    assert rejected.status == "rejected"
    assert rejected.rejection_reason == "duplicate of ask-9"


def test_proposal_carries_at_least_two_options() -> None:
    proposal = _proposal()
    assert len(proposal.options) == 2
    assert proposal.options[0].label != proposal.options[1].label


def test_option_evidence_and_unknowns_default_to_empty() -> None:
    # RELO-W4.6: additive fields default to empty so pre-existing construction
    # sites (that only pass label/tradeoffs) still work unchanged.
    option = GovernanceDecisionOption(label="Do nothing", tradeoffs="No cost, risk persists.")
    assert option.evidence_refs == ()
    assert option.unknowns == ()


def test_follow_up_action_defaults_to_empty_string() -> None:
    # RELO-W4.6: defaulted, not dataclass-required -- "required" is enforced
    # at the generation/release boundary, not at construction.
    proposal = _proposal()
    assert proposal.follow_up_action == "Send the escalation email to leadership by Friday."
    default_proposal = GovernanceDecisionBriefProposal(
        id="governance-decision-2", program_id="acme", decision_ask_id="ask-2",
        decision="d", context="c", options=proposal.options, recommendation="r",
        consequences_of_delay="cod", owner_alias=None, due_date=None,
        evidence_refs=(), ai_run_id="run-2",
    )
    assert default_proposal.follow_up_action == ""
