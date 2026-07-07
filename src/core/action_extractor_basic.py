from __future__ import annotations

from datetime import date
import re

from src.core.action_tracker import build_action_id
from src.core.models_v2 import ActionItem, ActionSourceType, ActionStatus, Signal, SignalClass
from src.core.signal_classification import signal_class
from src.core.signal_sentence_utils import candidate_sentences


ACTION_HINTS = ("action:", "follow up with", "follow-up with", "will deliver by", "need to", "todo:", "next step:")

_ISO_DATE_PATTERN = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_MONTH_DATE_PATTERN = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{1,2})(?:,\s*(20\d{2}))?\b",
    re.IGNORECASE,
)
_WI_REF_PATTERN = re.compile(r"\bWI:(\d+)\b", re.IGNORECASE)
_EMAIL_PATTERN = re.compile(r"\b([a-z0-9._-]+)@[a-z0-9.-]+\b", re.IGNORECASE)
_AT_ALIAS_PATTERN = re.compile(r"@([a-z][a-z0-9._-]+)", re.IGNORECASE)
_OWNER_PATTERN = re.compile(
    r"\b(?:owner|follow up with|follow-up with|ask|ping|assign(?:ed)? to)\s+([a-z][a-z0-9._-]+(?:@[a-z0-9.-]+)?)\b",
    re.IGNORECASE,
)
_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def extract_actions_from_signals(
    signals: tuple[Signal, ...],
    program_id: str,
) -> tuple[ActionItem, ...]:
    actions: list[ActionItem] = []
    seen_ids: set[str] = set()
    for signal in _prioritize_action_signals(signals):
        for sentence in candidate_sentences(signal.text):
            if not _looks_like_action(sentence):
                continue
            owner_alias = _infer_owner_alias(sentence, signal)
            due_date = _extract_due_date(sentence, signal.timestamp.date())
            linked_work_item_ids = _extract_work_item_ids(sentence, signal)
            action_id = build_action_id(
                program_id,
                text=sentence,
                owner_alias=owner_alias,
                due_date=due_date,
                source_signal_id=signal.id,
                workstream_id=signal.workstream_id,
                linked_work_item_ids=linked_work_item_ids,
            )
            if action_id in seen_ids:
                continue
            seen_ids.add(action_id)
            actions.append(
                ActionItem(
                    id=action_id,
                    program_id=program_id,
                    text=sentence,
                    owner_alias=owner_alias,
                    due_date=due_date,
                    status=ActionStatus.PROPOSED,
                    source_signal_id=signal.id,
                    source_type=ActionSourceType.SIGNAL,
                    linked_work_item_ids=linked_work_item_ids,
                    linked_claim_id=None,
                    linked_risk_id=None,
                    workstream_id=signal.workstream_id,
                    created_at=signal.timestamp,
                    resolved_at=None,
                    resolution_note=None,
                )
            )
    return tuple(actions)


_ACTION_SIGNAL_PRIORITIES = {
    SignalClass.MITIGATION: 0,
    SignalClass.DECISION: 1,
    SignalClass.DEPENDENCY: 2,
    SignalClass.RISK: 3,
    SignalClass.RCA: 4,
    SignalClass.STATUS: 5,
}


def _prioritize_action_signals(signals: tuple[Signal, ...]) -> tuple[Signal, ...]:
    return tuple(
        sorted(
            signals,
            key=lambda signal: (
                _ACTION_SIGNAL_PRIORITIES[signal_class(signal)],
                -signal.timestamp.timestamp(),
            ),
        )
    )
def _looks_like_action(text: str) -> bool:
    lowered = text.lower()
    return any(hint in lowered for hint in ACTION_HINTS)


def _extract_due_date(text: str, reference_date: date) -> date | None:
    iso_match = _ISO_DATE_PATTERN.search(text)
    if iso_match is not None:
        year, month, day = (int(part) for part in iso_match.groups())
        return date(year, month, day)
    month_match = _MONTH_DATE_PATTERN.search(text)
    if month_match is None:
        return None
    month_name, day_text, year_text = month_match.groups()
    year = int(year_text) if year_text is not None else reference_date.year
    return date(year, _MONTHS[month_name.lower()], int(day_text))


def _extract_work_item_ids(text: str, signal: Signal) -> tuple[int, ...]:
    seen: list[int] = []
    for match in _WI_REF_PATTERN.findall(text):
        work_item_id = int(match)
        if work_item_id not in seen:
            seen.append(work_item_id)
    for ref in signal.entity_refs:
        if not ref.upper().startswith("WI:"):
            continue
        try:
            work_item_id = int(ref.split(":", 1)[1])
        except ValueError:
            continue
        if work_item_id not in seen:
            seen.append(work_item_id)
    return tuple(seen)


def _infer_owner_alias(text: str, signal: Signal) -> str:
    for pattern in (_OWNER_PATTERN, _AT_ALIAS_PATTERN, _EMAIL_PATTERN):
        match = pattern.search(text)
        if match is None:
            continue
        value = match.group(1) if pattern is not _AT_ALIAS_PATTERN else match.group(1)
        alias = _normalize_alias(value)
        if alias is not None:
            return alias
    if signal.metadata is not None:
        sender_alias = signal.metadata.get("sender_alias")
        if isinstance(sender_alias, str):
            alias = _normalize_alias(sender_alias)
            if alias is not None:
                return alias
    return "unknown"


def _normalize_alias(value: str | None) -> str | None:
    if value is None:
        return None
    alias = value.strip().lower()
    if "@" in alias:
        alias = alias.split("@", 1)[0]
    alias = re.sub(r"[^a-z0-9._-]", "", alias)
    return alias or None