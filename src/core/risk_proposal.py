"""ADF-W4.5 (specs/arch-data-fix.md Section 8.10.1): risk proposals.

"Machine detection creates a candidate with evidence. AI may propose:
causal title; why it matters; probability; impact; category; mitigation;
owner; by-when; fallback. Human acceptance establishes judgment. Existing
human judgment always wins."

``RiskEntry`` (ADF-W4.2) cannot represent this directly -- it has no
``why_it_matters``/``fallback`` fields, and applying an AI-proposed value
straight onto a risk record would blur "AI proposed this" with "a human
assessed this." ``RiskProposal`` is its own type for exactly that reason,
mirroring ``ProgramSynthesis``/``MeetingAction``'s "proposal is a distinct
type from the record it may become" pattern from ADF-W2.9/ADF-W3.3.

This module owns the Zone-A-safe half: the type, deterministic assembly of
a request from a CANDIDATE-kind risk's own evidence, and applying an
approved proposal (only ever candidate -> strategic, never touching an
already-strategic risk's human-set fields). The AI call itself is Zone B --
see ``src/ai/risk_proposal_generator.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

from src.core.models_v2 import RiskCategory, RiskEntry, RiskImpact, RiskKind, RiskProbability, RiskStatus
from src.core.proposal_audit import record_proposal_event

RiskProposalStatus = Literal["staged", "approved", "rejected"]


@dataclass(frozen=True, slots=True)
class RiskProposalRequest:
    program_id: str
    candidate_risk_id: str
    candidate_title: str
    candidate_description: str
    evidence_texts: tuple[str, ...]
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RiskProposal:
    id: str
    program_id: str
    candidate_risk_id: str
    causal_title: str
    why_it_matters: str
    probability: RiskProbability
    impact: RiskImpact
    category: RiskCategory
    mitigation: str
    owner_alias: str | None
    by_when: date | None
    fallback: str
    evidence_refs: tuple[str, ...]
    ai_run_id: str
    status: RiskProposalStatus = "staged"
    rejection_reason: str | None = None
    # ADF-W2.11/W4.8 (ADR-0017): additive workflow-measurement timestamps.
    proposed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    decided_at: datetime | None = None


class RiskProposalError(Exception):
    """Raised when a RiskProposal cannot be assembled or applied."""


def assemble_risk_proposal_request(
    risk: RiskEntry, *, evidence_texts: tuple[str, ...] = ()
) -> RiskProposalRequest:
    """Only a CANDIDATE-kind risk is eligible -- proposing new judgment for
    an already-assessed STRATEGIC risk would contradict "existing human
    judgment always wins," and a HYGIENE finding is not a strategic risk
    at all (Section 8.10.1's three-way separation, ADF-W4.2)."""
    if risk.kind != RiskKind.CANDIDATE.value:
        raise RiskProposalError(
            f"Risk {risk.id!r} has kind={risk.kind!r}, not 'candidate' -- only a machine-derived "
            "candidate awaiting review is eligible for a risk proposal."
        )
    return RiskProposalRequest(
        program_id=risk.program_id,
        candidate_risk_id=risk.id,
        candidate_title=risk.title,
        candidate_description=risk.description,
        evidence_texts=evidence_texts,
        evidence_refs=risk.source_signal_ids,
    )


def apply_risk_proposal(risk: RiskEntry, proposal: RiskProposal) -> RiskEntry:
    """Human acceptance establishes judgment: this is the ONLY path that
    turns an AI-proposed assessment into a real risk record, and it only
    ever fires for an ``"approved"`` proposal against the exact candidate
    it was assembled from. Never applied automatically, never applied to
    an already-strategic risk (whose existing human judgment always
    wins -- Section 8.10.1, verbatim)."""
    if proposal.status != "approved":
        raise RiskProposalError(
            f"RiskProposal {proposal.id!r} has status={proposal.status!r}, not 'approved' -- "
            "only a human-approved proposal may be applied."
        )
    if risk.id != proposal.candidate_risk_id:
        raise RiskProposalError(
            f"RiskProposal {proposal.id!r} targets candidate {proposal.candidate_risk_id!r}, "
            f"not risk {risk.id!r}."
        )
    if risk.kind != RiskKind.CANDIDATE.value:
        raise RiskProposalError(
            f"Risk {risk.id!r} has kind={risk.kind!r}, not 'candidate' -- an already-strategic "
            "risk's existing human judgment always wins and is never overwritten by a proposal."
        )
    return replace(
        risk,
        kind=RiskKind.STRATEGIC.value,
        title=proposal.causal_title,
        description=proposal.why_it_matters,
        probability=proposal.probability,
        impact=proposal.impact,
        category=proposal.category,
        mitigation_plan=proposal.mitigation,
        owner_alias=proposal.owner_alias or risk.owner_alias,
        mitigation_due_date=proposal.by_when,
        status=RiskStatus.OPEN,
    )


def approve_risk_proposal(
    proposal: RiskProposal, *, programs_root: Path | None = None, reviewed: bool = True
) -> RiskProposal:
    if proposal.status == "rejected":
        raise RiskProposalError(f"RiskProposal {proposal.id!r} was rejected ({proposal.rejection_reason}) -- cannot approve.")
    decided_at = datetime.now(timezone.utc)
    record_proposal_event(
        program_id=proposal.program_id, proposal_type="risk", proposal_id=proposal.id,
        event="approved", programs_root=programs_root, at=decided_at, proposed_at=proposal.proposed_at,
        ai_run_id=proposal.ai_run_id, reviewed=reviewed,
    )
    return replace(proposal, status="approved", decided_at=decided_at)


def reject_risk_proposal(proposal: RiskProposal, *, reason: str, programs_root: Path | None = None) -> RiskProposal:
    decided_at = datetime.now(timezone.utc)
    record_proposal_event(
        program_id=proposal.program_id, proposal_type="risk", proposal_id=proposal.id,
        event="rejected", programs_root=programs_root, at=decided_at, proposed_at=proposal.proposed_at,
        ai_run_id=proposal.ai_run_id, rejection_reason=reason,
    )
    return replace(proposal, status="rejected", rejection_reason=reason, decided_at=decided_at)


__all__ = [
    "RiskProposal",
    "RiskProposalError",
    "RiskProposalRequest",
    "RiskProposalStatus",
    "apply_risk_proposal",
    "approve_risk_proposal",
    "assemble_risk_proposal_request",
    "reject_risk_proposal",
]
