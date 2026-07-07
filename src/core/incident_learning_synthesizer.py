from __future__ import annotations

from dataclasses import dataclass
import re

from src.core.models import Confidence
from src.core.models_v2 import IncidentEntry


@dataclass(frozen=True, slots=True)
class IncidentRefPattern:
    ref: str
    incident_refs: tuple[str, ...]
    summary_text: str
    workstream_id: str | None
    entry_count: int
    max_severity: int | None
    confidence: Confidence
    signal_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class IncidentClassPattern:
    class_label: str
    incident_refs: tuple[str, ...]
    summary_text: str
    workstream_ids: tuple[str, ...]
    entry_count: int
    max_severity: int | None
    confidence: Confidence
    signal_ids: frozenset[str]
    linked_refs: tuple[str, ...]


_STOPWORDS = {
    "a", "again", "and", "assumptions", "by", "cache", "due", "for", "from", "hidden", "icm",
    "in", "incomplete", "into", "of", "on", "the", "to", "under", "were", "with",
}


def build_incident_ref_patterns(entries: tuple[IncidentEntry, ...]) -> tuple[IncidentRefPattern, ...]:
    references: dict[str, list[IncidentEntry]] = {}
    for entry in entries:
        refs = tuple(
            dict.fromkeys(
                normalize_incident_ref(ref)
                for ref in entry.ado_entity_refs
                if normalize_incident_ref(ref)
            )
        )
        if not refs:
            continue
        for ref in refs:
            references.setdefault(ref, []).append(entry)

    patterns: list[IncidentRefPattern] = []
    for ref, linked_entries in sorted(references.items(), key=lambda item: (-len(item[1]), item[0])):
        ordered_entries = sorted(linked_entries, key=lambda entry: (entry.observed_at, entry.incident_id, entry.signal_id))
        summaries = _collect_unique_summaries(ordered_entries)
        if not summaries:
            continue
        severities = [entry.severity for entry in ordered_entries if entry.severity is not None]
        patterns.append(
            IncidentRefPattern(
                ref=ref,
                incident_refs=tuple(f"IcM {entry.incident_id}" for entry in ordered_entries),
                summary_text="; ".join(summaries[:2]),
                workstream_id=next((entry.workstream_id for entry in ordered_entries if entry.workstream_id), None),
                entry_count=len(ordered_entries),
                max_severity=min(severities) if severities else None,
                confidence=max((entry.confidence for entry in ordered_entries), key=confidence_rank),
                signal_ids=frozenset(entry.signal_id for entry in ordered_entries),
            )
        )
    return tuple(patterns[:3])


def build_incident_class_patterns(entries: tuple[IncidentEntry, ...]) -> tuple[IncidentClassPattern, ...]:
    groups: list[list[IncidentEntry]] = []
    for entry in sorted(entries, key=lambda value: (value.observed_at, value.incident_id, value.signal_id)):
        tokens = _summary_tokens(entry.belief_change_summary)
        if not tokens:
            continue
        target_group: list[IncidentEntry] | None = None
        best_overlap = 0.0
        for group in groups:
            overlap = _token_overlap(tokens, _summary_tokens(group[0].belief_change_summary))
            if overlap >= 0.5 and overlap > best_overlap:
                best_overlap = overlap
                target_group = group
        if target_group is None:
            groups.append([entry])
        else:
            target_group.append(entry)

    patterns: list[IncidentClassPattern] = []
    for group in groups:
        distinct_incidents = {entry.incident_id for entry in group}
        distinct_refs = {
            normalize_incident_ref(ref)
            for entry in group
            for ref in entry.ado_entity_refs
            if normalize_incident_ref(ref)
        }
        if len(group) < 3 or (len(distinct_incidents) < 3 and len(distinct_refs) < 2):
            continue
        summaries = _collect_unique_summaries(tuple(group))
        if not summaries:
            continue
        workstream_ids = tuple(dict.fromkeys(entry.workstream_id for entry in group if entry.workstream_id))
        severities = [entry.severity for entry in group if entry.severity is not None]
        patterns.append(
            IncidentClassPattern(
                class_label=_derive_class_label(group),
                incident_refs=tuple(f"IcM {entry.incident_id}" for entry in group),
                summary_text="; ".join(summaries[:2]),
                workstream_ids=workstream_ids,
                entry_count=len(group),
                max_severity=min(severities) if severities else None,
                confidence=max((entry.confidence for entry in group), key=confidence_rank),
                signal_ids=frozenset(entry.signal_id for entry in group),
                linked_refs=tuple(sorted(distinct_refs)),
            )
        )
    patterns.sort(key=lambda item: (-item.entry_count, item.class_label))
    return tuple(patterns[:2])


def normalize_incident_ref(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        return ""
    prefix, separator, suffix = normalized.partition(":")
    if not separator:
        return normalized.upper()
    return f"{prefix.upper()}:{suffix}"


def normalize_incident_learning_summary(summary: str) -> str:
    cleaned = re.sub(r"^IcM\s+\d+\s*:\s*", "", summary.strip(), flags=re.IGNORECASE)
    return cleaned.rstrip(".")


def confidence_rank(value: Confidence) -> int:
    ranks = {
        Confidence.NONE: 0,
        Confidence.LOW: 1,
        Confidence.MEDIUM: 2,
        Confidence.HIGH: 3,
    }
    return ranks[value]


def _collect_unique_summaries(entries: tuple[IncidentEntry, ...] | tuple[IncidentEntry] | list[IncidentEntry]) -> list[str]:
    summaries: list[str] = []
    seen_summaries: set[str] = set()
    for entry in entries:
        summary = normalize_incident_learning_summary(entry.belief_change_summary)
        if not summary or summary in seen_summaries:
            continue
        seen_summaries.add(summary)
        summaries.append(summary)
    return summaries


def _summary_tokens(summary: str) -> set[str]:
    normalized = normalize_incident_learning_summary(summary)
    normalized = re.sub(r"WI:\d+", " ", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized.lower())
    return {
        token
        for token in normalized.split()
        if len(token) >= 4 and token not in _STOPWORDS and not token.isdigit()
    }


def _token_overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    union = len(left | right)
    return intersection / union if union else 0.0


def _derive_class_label(entries: list[IncidentEntry]) -> str:
    token_counts: dict[str, int] = {}
    for entry in entries:
        for token in _summary_tokens(entry.belief_change_summary):
            token_counts[token] = token_counts.get(token, 0) + 1
    top_tokens = [token for token, _ in sorted(token_counts.items(), key=lambda item: (-item[1], item[0]))[:3]]
    if top_tokens:
        return " ".join(top_tokens)
    return "recurring incident class"