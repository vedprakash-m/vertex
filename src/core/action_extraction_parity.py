"""Observe-only parity checker for the two action-extraction pipelines (RELO-W4.5 / OD-12).

Compares normalized outputs of the meeting pipeline (``MeetingAction``) and the
signal pipeline (``ActionItem``) for the same source. Pure and deterministic:
it never mutates inputs, performs no I/O, and never gates publication.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.core.meeting_action import MeetingAction
from src.core.models_v2 import ActionItem


@dataclass(frozen=True, slots=True)
class NormalizedAction:
    key: str
    owner_alias: str | None
    due_date: date | None


@dataclass(frozen=True, slots=True)
class ParityFinding:
    kind: str
    key: str
    a_value: str | None
    b_value: str | None


def _key(text: str) -> str:
    return " ".join(text.split()).lower()


def normalize_meeting_actions(actions: tuple[MeetingAction, ...]) -> tuple[NormalizedAction, ...]:
    return tuple(NormalizedAction(_key(a.commitment), a.owner_alias, a.due_date) for a in actions)


def normalize_action_items(items: tuple[ActionItem, ...]) -> tuple[NormalizedAction, ...]:
    return tuple(NormalizedAction(_key(i.text), i.owner_alias, i.due_date) for i in items)


def _index(actions: tuple[NormalizedAction, ...]) -> dict[str, NormalizedAction]:
    indexed: dict[str, NormalizedAction] = {}
    for action in actions:
        indexed.setdefault(action.key, action)
    return indexed


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def check_action_parity(
    a: tuple[NormalizedAction, ...], b: tuple[NormalizedAction, ...]
) -> tuple[ParityFinding, ...]:
    """Return findings sorted by (kind, key); owners are compared raw (``None`` != ``"unknown"``)."""
    left, right = _index(a), _index(b)
    findings: list[ParityFinding] = []
    for key in left.keys() - right.keys():
        findings.append(ParityFinding("missing_in_b", key, key, None))
    for key in right.keys() - left.keys():
        findings.append(ParityFinding("missing_in_a", key, None, key))
    for key in left.keys() & right.keys():
        x, y = left[key], right[key]
        if x.owner_alias != y.owner_alias:
            findings.append(ParityFinding("owner_mismatch", key, x.owner_alias, y.owner_alias))
        if x.due_date != y.due_date:
            findings.append(ParityFinding("due_mismatch", key, _iso(x.due_date), _iso(y.due_date)))
    return tuple(sorted(findings, key=lambda f: (f.kind, f.key)))
