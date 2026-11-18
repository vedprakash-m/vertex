"""RELO-W4.12: deterministic, read-only exception queue for the cockpit.

Ranks existing signals only; computes nothing new and writes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.core.action_tracker import assess_action_staleness
from src.core.models_v2 import ActionItem, RiskEntry, RiskImpact, RiskProbability, RiskStatus
from src.core.risk_register_engine import assess_risk_staleness

_ACTIVE_RISK_STATUSES = {RiskStatus.OPEN, RiskStatus.ESCALATED}


@dataclass(frozen=True, slots=True)
class ExceptionItem:
    kind: str
    subject_id: str
    severity_rank: int
    age_days: int
    reason: str
    evidence: str


def build_exception_queue(
    risks: tuple[RiskEntry, ...], actions: tuple[ActionItem, ...], as_of: date
) -> tuple[ExceptionItem, ...]:
    """Rank: needs_input (0) > overdue_action (1) > stale_risk (2); ties by oldest, then id."""
    items: list[ExceptionItem] = []
    for risk in risks:
        review_date = risk.last_reviewed_date or risk.identified_date
        age = (as_of - review_date).days
        evidence = f"risk_register.yaml#{risk.id}"
        if risk.status in _ACTIVE_RISK_STATUSES and (
            risk.probability is RiskProbability.UNASSESSED or risk.impact is RiskImpact.UNASSESSED
        ):
            items.append(ExceptionItem("needs_input", risk.id, 0, age, "Risk level not assessed (Needs Input).", evidence))
        elif assess_risk_staleness(risk, as_of):
            reason = (
                f"Risk last reviewed {risk.last_reviewed_date.isoformat()}."
                if risk.last_reviewed_date is not None
                else f"Risk identified {risk.identified_date.isoformat()}; review date unavailable."
            )
            items.append(ExceptionItem(
                "stale_risk", risk.id, 2, age, reason, evidence
            ))
    for action in assess_action_staleness(actions, as_of):
        assert action.due_date is not None
        items.append(ExceptionItem(
            "overdue_action", action.id, 1, (as_of - action.due_date).days,
            f"Action due {action.due_date.isoformat()} is overdue.", f"actions.jsonl#{action.id}",
        ))
    return tuple(sorted(items, key=lambda item: (item.severity_rank, -item.age_days, item.subject_id)))


__all__ = ["ExceptionItem", "build_exception_queue"]
