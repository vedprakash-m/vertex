"""ADF-W4.7: unit tests for src/core/top_three_candidates.py."""

from __future__ import annotations

from datetime import date

import pytest

from src.core.top_three_candidates import (
    TopThreeCandidateError,
    TopThreeCandidateProposal,
    approve_top_three_candidate,
    reject_top_three_candidate,
    to_top3_now_entry,
)


def _proposal(*, status: str = "staged") -> TopThreeCandidateProposal:
    return TopThreeCandidateProposal(
        id="top3-candidate-1",
        program_id="xpf",
        item_id="risk-1",
        reason="Vendor delay threatens the Q3 milestone.",
        evidence_refs=("risk-1",),
        urgency="high",
        decision_or_action_needed="Escalate to vendor management this week.",
        owner_alias="alex",
        confidence="high",
        ai_run_id="run-1",
        status=status,  # type: ignore[arg-type]
    )


def test_approve_staged_candidate() -> None:
    approved = approve_top_three_candidate(_proposal())
    assert approved.status == "approved"


def test_approve_rejected_candidate_raises() -> None:
    rejected = reject_top_three_candidate(_proposal(), reason="not actually top priority")
    with pytest.raises(TopThreeCandidateError, match="rejected"):
        approve_top_three_candidate(rejected)


def test_reject_records_reason() -> None:
    rejected = reject_top_three_candidate(_proposal(), reason="duplicate of another candidate")
    assert rejected.status == "rejected"
    assert rejected.rejection_reason == "duplicate of another candidate"


def test_to_top3_now_entry_requires_approval() -> None:
    with pytest.raises(TopThreeCandidateError, match="not 'approved'"):
        to_top3_now_entry(_proposal(status="staged"))


def test_to_top3_now_entry_produces_real_entry() -> None:
    approved = approve_top_three_candidate(_proposal())
    entry = to_top3_now_entry(approved, by_date=date(2026, 8, 1), ado_link="https://ado/123")
    assert entry.type == "proposed"
    assert "Vendor delay threatens the Q3 milestone." in entry.text
    assert "Escalate to vendor management this week." in entry.text
    assert entry.owner == "alex"
    assert entry.ado_link == "https://ado/123"
    assert entry.by_date == date(2026, 8, 1)


def test_to_top3_now_entry_owner_defaults_to_empty_string() -> None:
    proposal = approve_top_three_candidate(
        TopThreeCandidateProposal(
            id="top3-candidate-2", program_id="xpf", item_id="risk-2", reason="x",
            evidence_refs=("risk-2",), urgency="low", decision_or_action_needed="y",
            owner_alias=None, confidence="low", ai_run_id="run-2",
        )
    )
    entry = to_top3_now_entry(proposal)
    assert entry.owner == ""
