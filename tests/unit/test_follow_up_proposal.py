"""ADF-W3.6: unit tests for src/core/follow_up_proposal.py."""

from __future__ import annotations

from datetime import date

import pytest

from src.core.follow_up_proposal import (
    FollowUpProposalError,
    approve_follow_up_proposal,
    generate_deterministic_follow_up,
    reject_follow_up_proposal,
)
from src.core.meeting_action import MeetingAction, approve_meeting_action


def _approved_action(*, owner_alias: str | None = "alex", due_date: date | None = date(2026, 8, 1)) -> MeetingAction:
    staged = MeetingAction(
        id="action-1", program_id="xpf", meeting_ref="meeting-1", commitment="Ship the deployment doc",
        owner_alias=owner_alias, due_date=due_date, linked_work_item_id=1001, blocks=(),
        source_span="Action: Ship the deployment doc | owner=alex | due=2026-08-01 | wi=1001",
        extraction_method="deterministic", status="staged",
    )
    return approve_meeting_action(staged, approved_by="pm@example.com")


def test_generate_from_approved_action_with_owner_and_due_date() -> None:
    action = _approved_action()
    proposal = generate_deterministic_follow_up(action, evidence_link="https://ado/1001")

    assert proposal.action_id == "action-1"
    assert proposal.recipient_alias == "alex"
    assert "Ship the deployment doc" in proposal.current_gap
    assert "2026-08-01" in proposal.current_gap
    assert "meeting-1" in proposal.why_it_matters
    assert "Ship the deployment doc" in proposal.requested_action
    assert proposal.due_date == date(2026, 8, 1)
    assert proposal.evidence_link == "https://ado/1001"
    assert proposal.generation_method == "deterministic"
    assert proposal.status == "staged"


def test_generate_from_action_without_due_date_omits_due_text() -> None:
    action = _approved_action(due_date=None)
    proposal = generate_deterministic_follow_up(action)
    assert "(due" not in proposal.current_gap
    assert proposal.due_date is None


def test_generate_from_staged_action_raises() -> None:
    staged = MeetingAction(
        id="action-2", program_id="xpf", meeting_ref="meeting-1", commitment="Do something",
        owner_alias="alex", due_date=None, linked_work_item_id=None, blocks=(),
        source_span="Action: Do something", extraction_method="deterministic", status="staged",
    )
    with pytest.raises(FollowUpProposalError, match="not 'approved'"):
        generate_deterministic_follow_up(staged)


def test_generate_without_owner_raises() -> None:
    action = _approved_action(owner_alias=None)
    with pytest.raises(FollowUpProposalError, match="no owner_alias"):
        generate_deterministic_follow_up(action)


def test_approve_staged_proposal() -> None:
    action = _approved_action()
    proposal = generate_deterministic_follow_up(action)
    approved = approve_follow_up_proposal(proposal)
    assert approved.status == "approved"


def test_approve_rejected_proposal_raises() -> None:
    action = _approved_action()
    proposal = reject_follow_up_proposal(generate_deterministic_follow_up(action), reason="already resolved")
    with pytest.raises(FollowUpProposalError, match="rejected"):
        approve_follow_up_proposal(proposal)


def test_reject_records_reason() -> None:
    action = _approved_action()
    proposal = reject_follow_up_proposal(generate_deterministic_follow_up(action), reason="already resolved offline")
    assert proposal.status == "rejected"
    assert proposal.rejection_reason == "already resolved offline"


def test_evidence_link_defaults_to_empty_string() -> None:
    action = _approved_action()
    proposal = generate_deterministic_follow_up(action)
    assert proposal.evidence_link == ""
