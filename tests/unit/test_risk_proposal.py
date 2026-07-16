"""ADF-W4.5: unit tests for src/core/risk_proposal.py."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from src.core.models_v2 import RiskCategory, RiskEntry, RiskImpact, RiskKind, RiskProbability, RiskStatus
from src.core.risk_proposal import (
    RiskProposal,
    RiskProposalError,
    apply_risk_proposal,
    approve_risk_proposal,
    assemble_risk_proposal_request,
    reject_risk_proposal,
)


def _candidate_risk(*, risk_id: str = "risk-1") -> RiskEntry:
    return RiskEntry(
        id=risk_id,
        program_id="xpf",
        title="Vendor delay signal",
        description="Multiple signals mention a vendor delay.",
        probability=RiskProbability.UNASSESSED,
        impact=RiskImpact.UNASSESSED,
        category=RiskCategory.EXTERNAL,
        owner_alias="unassigned",
        mitigation_plan=None,
        mitigation_due_date=None,
        linked_workstream_ids=(),
        linked_work_item_ids=(),
        linked_milestone_ids=(),
        linked_claim_ids=(),
        linked_action_ids=(),
        status=RiskStatus.OPEN,
        identified_date=date(2026, 7, 1),
        identified_in_vertex_issue=None,
        last_reviewed_date=None,
        entity_refs=(),
        source_signal_ids=("sig-1", "sig-2"),
        kind=RiskKind.CANDIDATE.value,
    )


def _proposal(*, risk_id: str = "risk-1", status: str = "staged") -> RiskProposal:
    return RiskProposal(
        id="risk-proposal-1",
        program_id="xpf",
        candidate_risk_id=risk_id,
        causal_title="Vendor X's staffing shortfall is delaying delivery",
        why_it_matters="This threatens the Q3 milestone.",
        probability=RiskProbability.LIKELY,
        impact=RiskImpact.HIGH,
        category=RiskCategory.EXTERNAL,
        mitigation="Escalate to vendor management.",
        owner_alias="alex",
        by_when=date(2026, 8, 1),
        fallback="Engage a backup vendor.",
        evidence_refs=("sig-1",),
        ai_run_id="run-1",
        status=status,  # type: ignore[arg-type]
    )


def test_assemble_request_from_candidate_risk() -> None:
    risk = _candidate_risk()
    request = assemble_risk_proposal_request(risk, evidence_texts=("Vendor X reported a delay.", "Timeline slipped."))
    assert request.candidate_risk_id == "risk-1"
    assert request.evidence_refs == ("sig-1", "sig-2")
    assert request.evidence_texts == ("Vendor X reported a delay.", "Timeline slipped.")


def test_assemble_request_rejects_strategic_risk() -> None:
    strategic = replace(_candidate_risk(), kind=RiskKind.STRATEGIC.value)
    with pytest.raises(RiskProposalError, match="not 'candidate'"):
        assemble_risk_proposal_request(strategic)


def test_apply_approved_proposal_transforms_candidate_to_strategic() -> None:
    risk = _candidate_risk()
    proposal = approve_risk_proposal(_proposal())
    updated = apply_risk_proposal(risk, proposal)
    assert updated.kind == RiskKind.STRATEGIC.value
    assert updated.title == "Vendor X's staffing shortfall is delaying delivery"
    assert updated.probability == RiskProbability.LIKELY
    assert updated.impact == RiskImpact.HIGH
    assert updated.mitigation_plan == "Escalate to vendor management."
    assert updated.owner_alias == "alex"
    assert updated.mitigation_due_date == date(2026, 8, 1)


def test_apply_staged_proposal_raises() -> None:
    risk = _candidate_risk()
    proposal = _proposal(status="staged")
    with pytest.raises(RiskProposalError, match="not 'approved'"):
        apply_risk_proposal(risk, proposal)


def test_apply_proposal_to_wrong_risk_raises() -> None:
    risk = _candidate_risk(risk_id="risk-2")
    proposal = approve_risk_proposal(_proposal(risk_id="risk-1"))
    with pytest.raises(RiskProposalError, match="targets candidate"):
        apply_risk_proposal(risk, proposal)


def test_apply_proposal_to_already_strategic_risk_raises() -> None:
    strategic = replace(_candidate_risk(), kind=RiskKind.STRATEGIC.value)
    proposal = approve_risk_proposal(_proposal())
    with pytest.raises(RiskProposalError, match="existing human judgment always wins"):
        apply_risk_proposal(strategic, proposal)


# --- ADF-W2.11/W4.8 (ADR-0017): workflow-measurement instrumentation ---


def test_approve_stamps_decided_at() -> None:
    proposal = _proposal()
    assert proposal.decided_at is None
    approved = approve_risk_proposal(proposal)
    assert approved.decided_at is not None
    assert approved.decided_at >= proposal.proposed_at


def test_reject_stamps_decided_at() -> None:
    rejected = reject_risk_proposal(_proposal(), reason="not credible")
    assert rejected.decided_at is not None


def test_approve_writes_audit_trail_when_programs_root_given(tmp_path) -> None:
    from src.core.proposal_audit import read_proposal_audit

    programs_root = tmp_path / "programs"
    approve_risk_proposal(_proposal(), programs_root=programs_root)
    records = read_proposal_audit("xpf", programs_root=programs_root)
    assert len(records) == 1
    assert records[0].proposal_type == "risk"
    assert records[0].event == "approved"
    assert records[0].proposal_id == "risk-proposal-1"


def test_approve_without_programs_root_writes_nothing(tmp_path) -> None:
    from src.core.proposal_audit import read_proposal_audit

    programs_root = tmp_path / "programs"
    approve_risk_proposal(_proposal())  # no programs_root -- opt-out
    assert read_proposal_audit("xpf", programs_root=programs_root) == ()


def test_approve_rejected_proposal_raises() -> None:
    proposal = reject_risk_proposal(_proposal(), reason="not a real risk")
    with pytest.raises(RiskProposalError, match="rejected"):
        approve_risk_proposal(proposal)


def test_reject_records_reason() -> None:
    proposal = reject_risk_proposal(_proposal(), reason="duplicate of risk-9")
    assert proposal.status == "rejected"
    assert proposal.rejection_reason == "duplicate of risk-9"
