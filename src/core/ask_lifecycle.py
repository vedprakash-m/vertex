from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from src.core.models_v2 import DecisionAsk, ResurfacingPolicy


class DecisionAskLifecycleStage(str, Enum):
    WATCH = "watch"
    NUDGE = "nudge"
    ESCALATE = "escalate"


@dataclass(frozen=True, slots=True)
class DecisionAskLifecycleProposal:
    ask: DecisionAsk
    age_days: int
    inactive_days: int
    stage: DecisionAskLifecycleStage
    is_expired: bool
    command: str
    proposed_action: str


def evaluate_decision_ask_lifecycle(
    ask: DecisionAsk,
    *,
    as_of: datetime,
) -> DecisionAskLifecycleProposal | None:
    if ask.status != "open":
        return None

    today = as_of.date()
    age_days = max(0, (today - ask.ask_date).days)
    last_touch_date = ask.last_touched_at.date() if ask.last_touched_at is not None else ask.ask_date
    inactive_days = max(0, (today - max(ask.ask_date, last_touch_date)).days)
    policy = ask.resurfacing_policy or ResurfacingPolicy()
    is_expired = ask.expiry_date is not None and ask.expiry_date <= today

    if is_expired or inactive_days >= policy.escalate_days:
        return DecisionAskLifecycleProposal(
            ask=ask,
            age_days=age_days,
            inactive_days=inactive_days,
            stage=DecisionAskLifecycleStage.ESCALATE,
            is_expired=is_expired,
            command=f"vertex escalate --edition {ask.edition_id} --decision-ask {ask.id} --dry-run",
            proposed_action="Preview an escalation note for the next triage cycle and decide whether to send it.",
        )
    if inactive_days >= policy.nudge_days:
        return DecisionAskLifecycleProposal(
            ask=ask,
            age_days=age_days,
            inactive_days=inactive_days,
            stage=DecisionAskLifecycleStage.NUDGE,
            is_expired=is_expired,
            command=f"vertex decisions nudge --program {ask.program_id} --id {ask.id} --dry-run",
            proposed_action="Stage a follow-up nudge draft so the owner can be reminded before the next publish cycle.",
        )
    if inactive_days >= policy.watch_days:
        return DecisionAskLifecycleProposal(
            ask=ask,
            age_days=age_days,
            inactive_days=inactive_days,
            stage=DecisionAskLifecycleStage.WATCH,
            is_expired=is_expired,
            command=f"vertex decisions aging --program {ask.program_id} --min-age-days {policy.watch_days}",
            proposed_action="Keep the ask in the watch queue and confirm the owner/date still look current.",
        )
    return None


def build_decision_ask_lifecycle_proposals(
    decision_asks: tuple[DecisionAsk, ...],
    *,
    as_of: datetime,
    minimum_stage: DecisionAskLifecycleStage | None = None,
) -> tuple[DecisionAskLifecycleProposal, ...]:
    proposals = [
        proposal
        for proposal in (
            evaluate_decision_ask_lifecycle(ask, as_of=as_of)
            for ask in decision_asks
        )
        if proposal is not None
    ]
    if minimum_stage is not None:
        minimum_rank = _stage_rank(minimum_stage)
        proposals = [proposal for proposal in proposals if _stage_rank(proposal.stage) >= minimum_rank]
    proposals.sort(
        key=lambda proposal: (
            -_stage_rank(proposal.stage),
            -proposal.inactive_days,
            -proposal.age_days,
            proposal.ask.issue_number,
            proposal.ask.id,
        )
    )
    return tuple(proposals)


def format_decision_ask_lifecycle_line(
    proposal: DecisionAskLifecycleProposal,
    *,
    include_command: bool = False,
) -> str:
    ask = proposal.ask
    owner = ask.owner_alias or "unassigned"
    refs = ", ".join(ask.entity_refs) if ask.entity_refs else "no linked refs"
    parts = [
        proposal.stage.value,
        f"{proposal.age_days} day(s) open",
    ]
    if proposal.inactive_days != proposal.age_days:
        parts.append(f"{proposal.inactive_days} day(s) inactive")
    if proposal.is_expired and ask.expiry_date is not None:
        parts.append(f"expired {ask.expiry_date.isoformat()}")
    parts.extend(
        [
            f"Issue #{ask.issue_number:03d} {ask.id}",
            f"owner {owner}",
            f"refs {refs}",
        ]
    )
    if ask.affected_milestone_ids:
        parts.append(f"milestones {', '.join(ask.affected_milestone_ids)}")
    parts.append(ask.text)
    if include_command:
        parts.append(f"Approve: {proposal.command}")
    return " | ".join(parts)


def _stage_rank(stage: DecisionAskLifecycleStage) -> int:
    return {
        DecisionAskLifecycleStage.WATCH: 1,
        DecisionAskLifecycleStage.NUDGE: 2,
        DecisionAskLifecycleStage.ESCALATE: 3,
    }[stage]