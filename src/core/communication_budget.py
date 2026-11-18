"""Observe-only stakeholder-communication budget and cooldown evaluator (RELO-W4.7, OD-9).

Pure and deterministic: it reports findings and never blocks or mutates inputs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence

DEFAULT_MAX_ASKS = 5


@dataclass(frozen=True, slots=True)
class CommunicationBudgetFinding:
    code: str
    audience_id: str
    detail: str


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")


def evaluate_communication_budget(
    *,
    audience_id: str,
    asks: Sequence[str],
    prior_sends: Sequence[datetime],
    cooldown_days: int,
    now: datetime,
    max_asks: int = DEFAULT_MAX_ASKS,
) -> tuple[CommunicationBudgetFinding, ...]:
    if cooldown_days < 0:
        raise ValueError("cooldown_days must be >= 0")
    if max_asks < 0:
        raise ValueError("max_asks must be >= 0")
    _require_aware(now, "now")
    for sent in prior_sends:
        _require_aware(sent, "prior_sends")

    findings: list[CommunicationBudgetFinding] = []
    if len(asks) > max_asks:
        findings.append(CommunicationBudgetFinding(
            code="over_ask_budget",
            audience_id=audience_id,
            detail=f"{len(asks)} material asks/changes exceed maximum {max_asks}",
        ))
    if cooldown_days and prior_sends:
        last_sent = max(prior_sends)
        if now - last_sent < timedelta(days=cooldown_days):
            findings.append(CommunicationBudgetFinding(
                code="within_cooldown",
                audience_id=audience_id,
                detail=f"last send {last_sent.isoformat()} is within {cooldown_days}-day cooldown",
            ))
    return tuple(sorted(findings, key=lambda f: (f.code, f.audience_id, f.detail)))
