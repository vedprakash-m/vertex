"""ADF-W4.7 remainder (specs/arch-data-fix.md Section 8.10.7): decision
briefs.

"A real decision brief contains: decision; context; options; tradeoffs;
recommendation; consequences of delay; owner; due date; evidence. This
remains distinct from editorial proposal review."

That closing sentence is the naming decision this module makes explicit:
`src/core/decision_brief_engine.py`'s `DecisionBrief`/`DecisionItem`
already exist under the "decision brief" name, but for a materially
different purpose -- reviewing whether to accept/revise/reject a proposed
newsletter section-text revision (`SectionRevisionProposal`), with no
options/tradeoffs/consequences-of-delay concept at all. Section 8.10.7's
schema is a governance decision brief: should the program actually make
some decision, with real options and a recommendation. Reusing the name
"DecisionBrief" for both would be exactly the collision Section 8.10.7
itself warns against ("remains distinct from editorial proposal review"),
so this module's type is named ``GovernanceDecisionBriefProposal``,
deliberately not "DecisionBrief."

The input is the pre-existing ``DecisionAsk`` type (already extracted from
authored narratives by ``claim_extractor.py`` -- "a decision that needs to
be made" is exactly what a `DecisionAsk` already represents), not a new
tracking concept.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

from src.core.models_v2 import DecisionAsk
from src.core.proposal_audit import record_proposal_event

GovernanceDecisionStatus = Literal["staged", "approved", "rejected"]


class GovernanceDecisionBriefError(Exception):
    """Raised when a GovernanceDecisionBriefProposal cannot be assembled."""


@dataclass(frozen=True, slots=True)
class GovernanceDecisionOption:
    label: str
    tradeoffs: str


@dataclass(frozen=True, slots=True)
class GovernanceDecisionRequest:
    program_id: str
    decision_ask_id: str
    decision_text: str
    evidence_texts: tuple[str, ...]
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GovernanceDecisionBriefProposal:
    id: str
    program_id: str
    decision_ask_id: str
    decision: str
    context: str
    options: tuple[GovernanceDecisionOption, ...]
    recommendation: str
    consequences_of_delay: str
    owner_alias: str | None
    due_date: date | None
    evidence_refs: tuple[str, ...]
    ai_run_id: str
    status: GovernanceDecisionStatus = "staged"
    rejection_reason: str | None = None
    # ADF-W2.11/W4.8 (ADR-0017): additive workflow-measurement timestamps.
    proposed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    decided_at: datetime | None = None


def assemble_governance_decision_request(
    ask: DecisionAsk, *, evidence_texts: tuple[str, ...] = ()
) -> GovernanceDecisionRequest:
    """Only an ``"open"`` DecisionAsk is eligible -- a resolved or deferred
    ask already has its outcome recorded elsewhere and proposing a fresh
    brief for it would be stale by construction."""
    if ask.status != "open":
        raise GovernanceDecisionBriefError(
            f"DecisionAsk {ask.id!r} has status={ask.status!r}, not 'open' -- only an open decision "
            "ask is eligible for a governance decision brief."
        )
    return GovernanceDecisionRequest(
        program_id=ask.program_id,
        decision_ask_id=ask.id,
        decision_text=ask.text,
        evidence_texts=evidence_texts,
        evidence_refs=ask.entity_refs,
    )


def approve_governance_decision_brief(
    proposal: GovernanceDecisionBriefProposal, *, programs_root: Path | None = None
) -> GovernanceDecisionBriefProposal:
    if proposal.status == "rejected":
        raise GovernanceDecisionBriefError(
            f"GovernanceDecisionBriefProposal {proposal.id!r} was rejected ({proposal.rejection_reason}) -- cannot approve."
        )
    decided_at = datetime.now(timezone.utc)
    record_proposal_event(
        program_id=proposal.program_id, proposal_type="governance_decision_brief", proposal_id=proposal.id,
        event="approved", programs_root=programs_root, at=decided_at, proposed_at=proposal.proposed_at,
        ai_run_id=proposal.ai_run_id,
    )
    return replace(proposal, status="approved", decided_at=decided_at)


def reject_governance_decision_brief(
    proposal: GovernanceDecisionBriefProposal, *, reason: str, programs_root: Path | None = None
) -> GovernanceDecisionBriefProposal:
    decided_at = datetime.now(timezone.utc)
    record_proposal_event(
        program_id=proposal.program_id, proposal_type="governance_decision_brief", proposal_id=proposal.id,
        event="rejected", programs_root=programs_root, at=decided_at, proposed_at=proposal.proposed_at,
        ai_run_id=proposal.ai_run_id, rejection_reason=reason,
    )
    return replace(proposal, status="rejected", rejection_reason=reason, decided_at=decided_at)


__all__ = [
    "GovernanceDecisionBriefError",
    "GovernanceDecisionBriefProposal",
    "GovernanceDecisionOption",
    "GovernanceDecisionRequest",
    "GovernanceDecisionStatus",
    "approve_governance_decision_brief",
    "assemble_governance_decision_request",
    "reject_governance_decision_brief",
]
