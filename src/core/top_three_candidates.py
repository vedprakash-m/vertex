"""ADF-W4.7 (specs/arch-data-fix.md Section 8.10.6): top-three candidates.

"Vertex proposes top-three items with: reason; evidence; urgency;
decision/action needed; owner; confidence. The PM accepts, rejects, or
edits them. The published `top_3_now` remains structured human-confirmed
data."

``top_3_now`` (``src/core/overrides_store.py::Top3NowEntry``) already
exists as the published, human-confirmed structure -- it has no
reason/evidence/urgency/confidence fields, because it was never meant to
carry a proposal's reasoning, only the confirmed result. ``TopThreeCandidateProposal``
is the proposal-shaped type that carries Section 8.10.6's full schema; only
an approved one may become a ``Top3NowEntry``, mirroring
``RiskProposal``/``MeetingAction``'s "proposal is distinct from the record
it may become, and only human approval bridges them" pattern from
ADF-W4.5/ADF-W3.5.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

from src.core.overrides_store import Top3NowEntry
from src.core.proposal_audit import record_proposal_event

Urgency = Literal["high", "medium", "low"]
Confidence = Literal["high", "medium", "low"]
TopThreeCandidateStatus = Literal["staged", "approved", "rejected"]


class TopThreeCandidateError(Exception):
    """Raised when a TopThreeCandidateProposal cannot be converted."""


@dataclass(frozen=True, slots=True)
class TopThreeCandidateProposal:
    id: str
    program_id: str
    item_id: str
    reason: str
    evidence_refs: tuple[str, ...]
    urgency: Urgency
    decision_or_action_needed: str
    owner_alias: str | None
    confidence: Confidence
    ai_run_id: str
    status: TopThreeCandidateStatus = "staged"
    rejection_reason: str | None = None
    # ADF-W2.11/W4.8 (ADR-0017): additive workflow-measurement timestamps.
    proposed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    decided_at: datetime | None = None


def approve_top_three_candidate(
    proposal: TopThreeCandidateProposal, *, programs_root: Path | None = None
) -> TopThreeCandidateProposal:
    if proposal.status == "rejected":
        raise TopThreeCandidateError(
            f"TopThreeCandidateProposal {proposal.id!r} was rejected ({proposal.rejection_reason}) -- cannot approve."
        )
    decided_at = datetime.now(timezone.utc)
    record_proposal_event(
        program_id=proposal.program_id, proposal_type="top_three", proposal_id=proposal.id,
        event="approved", programs_root=programs_root, at=decided_at, proposed_at=proposal.proposed_at,
        ai_run_id=proposal.ai_run_id,
    )
    return replace(proposal, status="approved", decided_at=decided_at)


def reject_top_three_candidate(
    proposal: TopThreeCandidateProposal, *, reason: str, programs_root: Path | None = None
) -> TopThreeCandidateProposal:
    decided_at = datetime.now(timezone.utc)
    record_proposal_event(
        program_id=proposal.program_id, proposal_type="top_three", proposal_id=proposal.id,
        event="rejected", programs_root=programs_root, at=decided_at, proposed_at=proposal.proposed_at,
        ai_run_id=proposal.ai_run_id, rejection_reason=reason,
    )
    return replace(proposal, status="rejected", rejection_reason=reason, decided_at=decided_at)


def to_top3_now_entry(
    proposal: TopThreeCandidateProposal, *, by_date: date | None = None, ado_link: str = "", anchor: str = ""
) -> Top3NowEntry:
    """The PM's accept step (Section 8.10.6: "The PM accepts, rejects, or
    edits them") -- only an approved proposal may become a real,
    published ``Top3NowEntry``. The PM's own edit is not modeled here
    (they can freely construct a different ``Top3NowEntry`` by hand); this
    is the mechanical accept-as-is path."""
    if proposal.status != "approved":
        raise TopThreeCandidateError(
            f"TopThreeCandidateProposal {proposal.id!r} has status={proposal.status!r}, not 'approved' -- "
            "only a PM-approved candidate may be published to top_3_now."
        )
    return Top3NowEntry(
        type="proposed",
        text=f"{proposal.reason.strip()} — {proposal.decision_or_action_needed.strip()}",
        owner=proposal.owner_alias or "",
        ado_link=ado_link,
        anchor=anchor,
        by_date=by_date,
    )


__all__ = [
    "Confidence",
    "TopThreeCandidateError",
    "TopThreeCandidateProposal",
    "TopThreeCandidateStatus",
    "Urgency",
    "approve_top_three_candidate",
    "reject_top_three_candidate",
    "to_top3_now_entry",
]
