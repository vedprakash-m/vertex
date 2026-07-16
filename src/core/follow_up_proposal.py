"""ADF-W3.6 (specs/arch-data-fix.md Section 8.10.8): tailored follow-up
proposals.

"Nudges and review messages become audience-specific proposals: current
gap; why it matters to the recipient; exact requested action; owner and
due date; evidence link. Deterministic templates remain the fallback. AI
is used only when it adds audience adaptation or synthesis."

That framing makes the deterministic template the PRIMARY, always-correct
path -- AI is explicitly an optional enhancement ("only when it adds..."),
not a requirement for a valid follow-up proposal. This module owns the
deterministic path in full: every field of Section 8.10.8's schema, always
available, no AI call, no failure mode. It targets a `MeetingAction`
(ADF-W3.3) specifically -- Slice 3's golden workflow names "follow-up" as
the step after a reviewed action is routed to ADO (ADF-W3.5), before reply
ingestion (ADF-W3.7, not attempted).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Literal

from src.core.meeting_action import MeetingAction

FollowUpStatus = Literal["staged", "approved", "rejected"]


class FollowUpProposalError(Exception):
    """Raised when a FollowUpProposal cannot be generated or applied."""


@dataclass(frozen=True, slots=True)
class FollowUpProposal:
    id: str
    program_id: str
    action_id: str
    recipient_alias: str
    current_gap: str
    why_it_matters: str
    requested_action: str
    due_date: date | None
    evidence_link: str
    generation_method: Literal["deterministic", "llm"]
    status: FollowUpStatus = "staged"
    rejection_reason: str | None = None


def generate_deterministic_follow_up(
    action: MeetingAction, *, evidence_link: str = ""
) -> FollowUpProposal:
    """The always-available fallback path. Requires the action to already
    be reviewed (``status == "approved"``, ADF-W3.5's human-review gate)
    and to have a real owner -- a follow-up with no recipient is not a
    follow-up. Content is entirely template-derived from the action's own
    fields; nothing here is invented."""
    if action.status != "approved":
        raise FollowUpProposalError(
            f"MeetingAction {action.id!r} has status={action.status!r}, not 'approved' -- "
            "only a reviewed and approved action gets a follow-up (Section 11.4's golden "
            "workflow: reviewed ADO proposal -> follow-up)."
        )
    if not action.owner_alias:
        raise FollowUpProposalError(f"MeetingAction {action.id!r} has no owner_alias -- cannot address a follow-up.")

    due_text = f" (due {action.due_date.isoformat()})" if action.due_date else ""
    current_gap = f"No confirmed status update yet on: {action.commitment}{due_text}."
    why_it_matters = f"This was committed during meeting {action.meeting_ref} and remains unresolved."
    requested_action = f"Please confirm completion, or provide a status update and a revised date, for: {action.commitment}"

    return FollowUpProposal(
        id=f"followup-{action.id}",
        program_id=action.program_id,
        action_id=action.id,
        recipient_alias=action.owner_alias,
        current_gap=current_gap,
        why_it_matters=why_it_matters,
        requested_action=requested_action,
        due_date=action.due_date,
        evidence_link=evidence_link,
        generation_method="deterministic",
    )


def approve_follow_up_proposal(proposal: FollowUpProposal) -> FollowUpProposal:
    if proposal.status == "rejected":
        raise FollowUpProposalError(
            f"FollowUpProposal {proposal.id!r} was rejected ({proposal.rejection_reason}) -- cannot approve."
        )
    return replace(proposal, status="approved")


def reject_follow_up_proposal(proposal: FollowUpProposal, *, reason: str) -> FollowUpProposal:
    return replace(proposal, status="rejected", rejection_reason=reason)


__all__ = [
    "FollowUpProposal",
    "FollowUpProposalError",
    "FollowUpStatus",
    "approve_follow_up_proposal",
    "generate_deterministic_follow_up",
    "reject_follow_up_proposal",
]
