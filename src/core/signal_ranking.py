from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any

from src.core.models import Confidence
from src.core.models_v2 import PersonDirectory, Signal, SignalClass
from src.core.signal_classification import signal_class


@dataclass(frozen=True, slots=True)
class CorroborationStats:
    """Tracks how many distinct sources confirmed an entity_ref within one gather cycle."""
    entity_ref: str
    source_count: int
    sources: tuple[str, ...]
    has_multisource: bool = True


def compute_corroboration_stats(
    signals: tuple[Signal, ...],
) -> dict[str, CorroborationStats]:
    """
    Build a map of entity_ref -> CorroborationStats for a gather cycle's signals.
    An entity_ref is considered corroborated when ≥2 distinct sources (ADO, WorkIQ, IcM, etc.)
    appear for the same entity_ref within the signal set.
    """
    entity_sources: dict[str, set[str]] = {}
    for signal in signals:
        for ref in signal.entity_refs:
            ref_str = str(ref).strip().lower()
            if not ref_str:
                continue
            source_family = signal_source_family(signal.source)
            if ref_str not in entity_sources:
                entity_sources[ref_str] = set()
            entity_sources[ref_str].add(source_family)

    return {
        ref: CorroborationStats(
            entity_ref=ref,
            source_count=len(source_set),
            sources=tuple(sorted(source_set)),
            has_multisource=len(source_set) >= 2,
        )
        for ref, source_set in entity_sources.items()
    }


def escalate_confidence_by_corroboration(
    signal: Signal,
    stats: CorroborationStats,
) -> Signal:
    """
    If signal is MEDIUM confidence and its entity_refs have multisource corroboration,
    return a new Signal with HIGH confidence. Otherwise return the signal unchanged.
    """
    if signal.confidence != Confidence.MEDIUM:
        return signal

    if not stats.has_multisource:
        return signal

    # Build elevated metadata
    elevated_metadata: dict[str, Any] = dict(signal.metadata or {})
    elevated_metadata["corroboration_count"] = stats.source_count
    elevated_metadata["corroborated_sources"] = stats.sources
    elevated_metadata["confidence_escalated"] = True

    return Signal(
        id=signal.id,
        timestamp=signal.timestamp,
        source=signal.source,
        program_id=signal.program_id,
        workstream_id=signal.workstream_id,
        entity_refs=signal.entity_refs,
        text=signal.text,
        raw_ref=signal.raw_ref,
        confidence=Confidence.HIGH,
        metadata=elevated_metadata,
        thread_id=signal.thread_id,
        review_policy=signal.review_policy,
    )


def apply_corroboration_escalation(
    signals: tuple[Signal, ...],
    *,
    as_of: datetime | None = None,
) -> tuple[Signal, ...]:
    """
    Apply confidence escalation to all signals in a gather cycle that share
    entity_refs with corroboration from ≥2 distinct sources.
    Returns new signals (originals unchanged) with elevated confidence where applicable.
    """
    if not signals:
        return signals

    stats_map = compute_corroboration_stats(signals)

    result: list[Signal] = []
    for signal in signals:
        # Find the max corroboration for any of this signal's entity_refs
        best_stats: CorroborationStats | None = None
        for ref in signal.entity_refs:
            ref_str = str(ref).strip().lower()
            if ref_str in stats_map:
                candidate = stats_map[ref_str]
                if best_stats is None or candidate.source_count > best_stats.source_count:
                    best_stats = candidate

        if best_stats is not None:
            result.append(escalate_confidence_by_corroboration(signal, best_stats))
        else:
            result.append(signal)

    return tuple(result)


DEFAULT_SOURCE_CONFIDENCE_ORDER = ("ado", "icm", "kusto", "workiq", "ai")


_EXECUTIVE_TITLE_RE = re.compile(r"\b(cvp|vice president|vp|general manager)\b", re.IGNORECASE)
_DIRECTOR_TITLE_RE = re.compile(r"\b(director|partner|head)\b", re.IGNORECASE)
_MANAGER_TITLE_RE = re.compile(r"\b(principal|manager|lead)\b", re.IGNORECASE)
_SENIOR_TITLE_RE = re.compile(r"\b(senior|staff)\b", re.IGNORECASE)


def sort_signals_for_ai_context(
    signals: tuple[Signal, ...],
    *,
    people_directory: tuple[PersonDirectory, ...] = (),
    as_of: datetime | None = None,
    source_confidence_order: tuple[str, ...] = (),
) -> tuple[Signal, ...]:
    current_time = _ensure_utc(as_of or datetime.now(timezone.utc))
    resolved_source_order = _resolve_source_confidence_order(source_confidence_order)
    people_by_alias = {
        person.alias.strip().lower(): person
        for person in people_directory
        if person.alias.strip()
    }
    thread_depths = Counter(
        thread_key
        for signal in signals
        if (thread_key := _signal_thread_key(signal)) is not None
    )
    ordered = sorted(
        enumerate(signals),
        key=lambda entry: (
            _source_confidence_rank(entry[1].source, resolved_source_order),
            -_signal_class_priority(entry[1]),
            -_workiq_relevance_score(
                entry[1],
                people_by_alias=people_by_alias,
                thread_depths=thread_depths,
                as_of=current_time,
            ),
            -_ensure_utc(entry[1].timestamp).timestamp(),
            entry[0],
        ),
    )
    return tuple(signal for _, signal in ordered)


def _resolve_source_confidence_order(source_confidence_order: tuple[str, ...]) -> tuple[str, ...]:
    requested = tuple(
        group.strip().lower()
        for group in source_confidence_order
        if group.strip()
    )
    merged: list[str] = []
    for group in (*requested, *DEFAULT_SOURCE_CONFIDENCE_ORDER):
        if group not in merged:
            merged.append(group)
    return tuple(merged)


def _signal_class_priority(signal: Signal) -> int:
    priorities = {
        SignalClass.DECISION: 5,
        SignalClass.RISK: 4,
        SignalClass.MITIGATION: 3,
        SignalClass.RCA: 2,
        SignalClass.DEPENDENCY: 1,
        SignalClass.STATUS: 0,
    }
    return priorities[signal_class(signal)]


def _source_confidence_rank(source: str, source_confidence_order: tuple[str, ...]) -> int:
    source_group = signal_source_family(source)
    try:
        return source_confidence_order.index(source_group)
    except ValueError:
        return source_confidence_order.index("kusto") if "kusto" in source_confidence_order else len(source_confidence_order)


def signal_source_family(source: str) -> str:
    normalized = source.strip().lower()
    if normalized == "manual" or normalized.startswith("ado/") or normalized == "ado":
        return "ado"
    if normalized.startswith("icm/") or normalized == "icm":
        return "icm"
    if normalized.startswith("kusto/") or normalized == "kusto":
        return "kusto"
    if normalized.startswith("workiq/") or normalized == "workiq":
        return "workiq"
    if normalized.startswith("ai/") or normalized == "ai":
        return "ai"
    return "kusto"


def _source_confidence_group(source: str) -> str:
    return signal_source_family(source)


def _workiq_relevance_score(
    signal: Signal,
    *,
    people_by_alias: dict[str, PersonDirectory],
    thread_depths: Counter[str],
    as_of: datetime,
) -> float:
    if not _is_workiq_source(signal.source):
        return 0.0

    sender_alias = _metadata_string(signal, "sender_alias")
    seniority_score = _sender_seniority_score(sender_alias, people_by_alias)
    thread_depth_score = float(thread_depths.get(_signal_thread_key(signal) or "", 0))
    age_days = max(0.0, (_ensure_utc(as_of) - _ensure_utc(signal.timestamp)).total_seconds() / 86400.0)
    recency_score = max(0.0, 14.0 - min(age_days, 14.0)) / 14.0
    return (seniority_score * 2.0) + thread_depth_score + recency_score


def _sender_seniority_score(sender_alias: str | None, people_by_alias: dict[str, PersonDirectory]) -> float:
    if sender_alias is None:
        return 0.0
    person = people_by_alias.get(sender_alias.strip().lower())
    if person is None:
        return 0.0

    title = person.title or ""
    score = 0.5
    if _EXECUTIVE_TITLE_RE.search(title):
        score = 4.0
    elif _DIRECTOR_TITLE_RE.search(title):
        score = 3.0
    elif _MANAGER_TITLE_RE.search(title):
        score = 2.0
    elif _SENIOR_TITLE_RE.search(title):
        score = 1.0

    if person.org_chain:
        if len(person.org_chain) <= 2:
            score = max(score, 3.0)
        elif len(person.org_chain) == 3:
            score = max(score, 2.0)
    return score


def _signal_thread_key(signal: Signal) -> str | None:
    if signal.thread_id is not None and signal.thread_id.strip():
        return signal.thread_id.strip().lower()
    thread_id = _metadata_string(signal, "thread_id") or _metadata_string(signal, "conversation_id")
    if thread_id is None:
        return None
    return thread_id.strip().lower() or None


def _metadata_string(signal: Signal, key: str) -> str | None:
    if signal.metadata is None:
        return None
    value = signal.metadata.get(key)
    if isinstance(value, str) and value.strip():
        return value
    return None


def _is_workiq_source(source: str) -> bool:
    normalized = source.strip().lower()
    return normalized.startswith("workiq/") or normalized == "workiq"


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# ── FR-SG-33: signal priority, escalation detection, top_3_now candidates ───


_SOURCE_PRIORITY_WEIGHTS: dict[str, float] = {
    "ado": 1.0,
    "icm": 0.9,
    "kusto": 0.7,
    "workiq": 0.6,
    "ai": 0.3,
}

_CLASS_PRIORITY_WEIGHTS: dict[SignalClass, float] = {
    SignalClass.DECISION: 1.0,
    SignalClass.RISK: 0.9,
    SignalClass.MITIGATION: 0.7,
    SignalClass.RCA: 0.6,
    SignalClass.DEPENDENCY: 0.5,
    SignalClass.STATUS: 0.3,
}

_CONFIDENCE_WEIGHTS: dict[str, float] = {
    "high": 1.0,
    "medium": 0.7,
    "low": 0.4,
    "none": 0.1,
}

_ESCALATION_MIN_SOURCES = 2
_ESCALATION_CLASSES = {SignalClass.RISK, SignalClass.DECISION}
_RECENCY_HALF_LIFE_DAYS = 14.0


def compute_signal_priority(
    signal: Signal,
    stats_map: dict[str, CorroborationStats],
    *,
    as_of: datetime | None = None,
) -> float:
    """Composite priority score ∈ [0, 3] for ranking signals in top_3_now candidates."""
    current_time = _ensure_utc(as_of or datetime.now(timezone.utc))

    source_weight = _SOURCE_PRIORITY_WEIGHTS.get(signal_source_family(signal.source), 0.5)
    class_weight = _CLASS_PRIORITY_WEIGHTS.get(signal_class(signal), 0.3)
    confidence_weight = _CONFIDENCE_WEIGHTS.get(signal.confidence.value.lower(), 0.4)

    age_days = max(0.0, (_ensure_utc(signal.timestamp) - current_time).total_seconds() / 86400.0)
    # Recency: decays with 14-day half-life; clamp recent signals to 1.0
    recency_weight = max(0.05, 2.0 ** (age_days / _RECENCY_HALF_LIFE_DAYS)) if age_days < 0 else max(0.05, 2.0 ** -(abs(age_days) / _RECENCY_HALF_LIFE_DAYS))

    max_sources = max(
        (stats_map[str(ref).strip().lower()].source_count
         for ref in signal.entity_refs
         if str(ref).strip().lower() in stats_map),
        default=1,
    )
    corroboration_multiplier = 1.0 + 0.5 * min(max_sources - 1, 2)

    return source_weight * class_weight * confidence_weight * recency_weight * corroboration_multiplier


def detect_escalation_signals(
    signals: tuple[Signal, ...],
    stats_map: dict[str, CorroborationStats],
) -> tuple[Signal, ...]:
    """Return signals that warrant escalation: high-confidence risk/decision with ≥2-source corroboration."""
    escalation: list[Signal] = []
    for signal in signals:
        if signal.confidence.value.lower() != "high":
            continue
        if signal_class(signal) not in _ESCALATION_CLASSES:
            continue
        max_sources = max(
            (stats_map[str(ref).strip().lower()].source_count
             for ref in signal.entity_refs
             if str(ref).strip().lower() in stats_map),
            default=1,
        )
        if max_sources >= _ESCALATION_MIN_SOURCES:
            escalation.append(signal)
    return tuple(escalation)


def populate_top_3_now_candidates(
    signals: tuple[Signal, ...],
    stats_map: dict[str, CorroborationStats],
    *,
    as_of: datetime | None = None,
    n: int = 3,
) -> tuple[Signal, ...]:
    """Return up to *n* highest-priority signals as top_3_now candidates (FR-SG-33)."""
    if not signals:
        return ()
    scored = sorted(
        signals,
        key=lambda s: compute_signal_priority(s, stats_map, as_of=as_of),
        reverse=True,
    )
    return tuple(scored[:n])