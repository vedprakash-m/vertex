from __future__ import annotations

import re
from uuid import NAMESPACE_URL, uuid5

from src.core.models_v2 import DecisionEntry, DecisionStatus, Signal, SignalClass
from src.core.signal_classification import signal_class
from src.core.signal_sentence_utils import candidate_sentences


_STRONG_DECISION_PATTERNS = (
    re.compile(r"\b(?:lt|leadership)\s+approved\b", re.IGNORECASE),
    re.compile(r"\bdecided to\b", re.IGNORECASE),
    re.compile(r"\bagreed to\b", re.IGNORECASE),
    re.compile(r"\bchoice is\b", re.IGNORECASE),
    re.compile(r"\bproceed with\b", re.IGNORECASE),
    re.compile(r"\bmove forward with\b", re.IGNORECASE),
    re.compile(r"\bgo with\b", re.IGNORECASE),
    re.compile(r"\bdecision:\s*", re.IGNORECASE),
    re.compile(r"\bapproved\b", re.IGNORECASE),
)
_REJECT_DECISION_HINTS = (
    "need a decision",
    "need decision",
    "pending decision",
    "decision pending",
    "await approval",
    "awaiting approval",
    "waiting for approval",
    "requires approval",
    "approval required",
    "approval pending",
    "decision tbd",
    "tbd decision",
    "to be decided",
    "leadership ask",
)
_TITLE_LIMIT = 96
_CONTEXT_LIMIT = 240


def extract_decisions_from_signals(
    signals: tuple[Signal, ...],
    *,
    program_id: str,
) -> tuple[DecisionEntry, ...]:
    extracted: list[DecisionEntry] = []
    seen_ids: set[str] = set()
    for signal in signals:
        if signal_class(signal) is not SignalClass.DECISION:
            continue
        for sentence in candidate_sentences(signal.text):
            if not _is_strong_decision_sentence(sentence):
                continue
            entry = _decision_entry_from_signal(program_id=program_id, signal=signal, sentence=sentence)
            if entry.id in seen_ids:
                continue
            seen_ids.add(entry.id)
            extracted.append(entry)
    return tuple(extracted)


def _is_strong_decision_sentence(sentence: str) -> bool:
    normalized = " ".join(sentence.split()).strip()
    if not normalized or normalized.endswith("?"):
        return False
    lowered = normalized.lower()
    if any(hint in lowered for hint in _REJECT_DECISION_HINTS):
        return False
    return any(pattern.search(normalized) for pattern in _STRONG_DECISION_PATTERNS)


def _decision_entry_from_signal(*, program_id: str, signal: Signal, sentence: str) -> DecisionEntry:
    normalized_sentence = " ".join(sentence.split()).strip()
    return DecisionEntry(
        id=_decision_entry_id(program_id=program_id, source_signal_id=signal.id, sentence=normalized_sentence),
        program_id=program_id,
        title=_decision_title(normalized_sentence),
        context=_decision_context(signal=signal, sentence=normalized_sentence),
        decision=normalized_sentence,
        rationale=None,
        alternatives_considered=(),
        decided_by=_decision_actor(signal),
        decision_date=signal.timestamp.date(),
        status=DecisionStatus.PROPOSED,
        superseded_by=None,
        linked_claim_id=None,
        linked_risk_id=None,
        linked_action_ids=(),
        workstream_id=signal.workstream_id,
        entity_refs=signal.entity_refs,
        review_by=None,
    )


def _decision_entry_id(*, program_id: str, source_signal_id: str, sentence: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"decision|{program_id.strip().lower()}|{source_signal_id}|{sentence}"))


def _decision_title(sentence: str) -> str:
    candidate = sentence.rstrip(".").strip() or "Auto-extracted proposed decision"
    if len(candidate) <= _TITLE_LIMIT:
        return candidate
    return candidate[: _TITLE_LIMIT - 3].rstrip() + "..."


def _decision_context(*, signal: Signal, sentence: str) -> str:
    provenance = f"Derived from {signal.source} signal {signal.id} on {signal.timestamp.date().isoformat()}."
    normalized_signal_text = " ".join(signal.text.split()).strip()
    if not normalized_signal_text or normalized_signal_text == sentence:
        return provenance
    if len(normalized_signal_text) > _CONTEXT_LIMIT:
        normalized_signal_text = normalized_signal_text[: _CONTEXT_LIMIT - 3].rstrip() + "..."
    return f"{provenance} Signal context: {normalized_signal_text}"


def _decision_actor(signal: Signal) -> str:
    if signal.metadata is not None:
        sender_alias = signal.metadata.get("sender_alias")
        if isinstance(sender_alias, str) and sender_alias.strip():
            return sender_alias.strip()
    return "auto-extracted"
