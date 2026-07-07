from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Iterator
import re

if TYPE_CHECKING:
    from src.core.m365_registry_store import M365RegistryArtifact


@dataclass(frozen=True, slots=True)
class RegistryIdCandidate:
    discovered_id: str
    label: str
    source_url: str | None
    exact_match: bool
    match_score: float = 0.0


_MATCH_SEPARATOR_RE = re.compile(r"[^a-z0-9]+")
# Truly generic meeting/chat nouns — program-agnostic, safe to keep in core.
_GENERIC_MATCH_TOKENS = frozenset({"weekly", "review", "sync", "meeting", "meetings", "chat", "channel", "series"})

# Match-normalization aliases (e.g. "Contoso"/"Contoso" → "Direct Drive on Northwind") are
# PROGRAM-SPECIFIC and MUST NOT be hardcoded in core (Vertex is a generic platform for any
# TPM/EM program). They are sourced from per-program config (`workstreams.yaml` aliases)
# and injected for the duration of a discovery pass via `use_match_aliases`. The default
# is empty so the core matcher carries zero program knowledge.
MatchAliasRule = tuple[re.Pattern[str], str]
_ACTIVE_MATCH_ALIASES: ContextVar[tuple[MatchAliasRule, ...]] = ContextVar(
    "vertex_active_match_aliases", default=()
)


def _alias_tokens(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(token for token in _MATCH_SEPARATOR_RE.split(value.lower()) if token)


def compile_match_aliases(rules: Iterable[tuple[str, Iterable[str]]]) -> tuple[MatchAliasRule, ...]:
    """Compile per-program ``(canonical_name, alias_variants)`` pairs into substitution rules.

    Each alias variant is rewritten to the canonical name during match normalization, so a
    program's abbreviations live in its own config rather than in core logic. Variants are
    matched flexibly across separators (``contoso``/``contoso``/``dd pf`` all match).
    """

    compiled: list[MatchAliasRule] = []
    seen: set[str] = set()
    for canonical, variants in rules:
        replacement = " ".join(_alias_tokens(canonical))
        if not replacement:
            continue
        for variant in variants:
            tokens = _alias_tokens(variant)
            if not tokens:
                continue
            key = "-".join(tokens)
            if key in seen or " ".join(tokens) == replacement:
                continue
            seen.add(key)
            body = r"[\s\-/]*".join(re.escape(token) for token in tokens)
            compiled.append((re.compile(rf"\b{body}\b"), replacement))
    return tuple(compiled)


def build_workstream_match_aliases(workstreams: Iterable[object]) -> tuple[MatchAliasRule, ...]:
    """Derive match-normalization aliases from each workstream's configured aliases.

    Reuses the per-program ``workstreams.yaml`` ``aliases`` already authored for each
    program; no program-specific knowledge lives in core. Workstreams without aliases
    (or without the attribute) contribute nothing.
    """

    rules: list[tuple[str, tuple[str, ...]]] = []
    for workstream in workstreams:
        name = getattr(workstream, "name", None) or getattr(workstream, "id", None)
        aliases = getattr(workstream, "aliases", ()) or ()
        if not name or not aliases:
            continue
        rules.append((str(name), tuple(str(alias) for alias in aliases)))
    return compile_match_aliases(rules)


@contextmanager
def use_match_aliases(aliases: Iterable[MatchAliasRule]) -> Iterator[None]:
    """Activate per-program match aliases for the duration of a discovery pass."""

    token = _ACTIVE_MATCH_ALIASES.set(tuple(aliases))
    try:
        yield
    finally:
        _ACTIVE_MATCH_ALIASES.reset(token)


def normalize_match_text(value: str | None) -> str:
    if value is None:
        return ""
    normalized = value.lower()
    for pattern, replacement in _ACTIVE_MATCH_ALIASES.get():
        normalized = pattern.sub(replacement, normalized)
    normalized = _MATCH_SEPARATOR_RE.sub(" ", normalized)
    return " ".join(normalized.split())


def tokenize_match_text(value: str | None, *, drop_generic: bool) -> tuple[str, ...]:
    normalized = normalize_match_text(value)
    if not normalized:
        return ()
    tokens = tuple(token for token in normalized.split() if token)
    if not drop_generic:
        return tokens
    return tuple(token for token in tokens if token not in _GENERIC_MATCH_TOKENS)


def match_overlap_score(expected: str | None, candidate: str | None, *, drop_generic: bool) -> float:
    expected_tokens = tokenize_match_text(expected, drop_generic=drop_generic)
    candidate_tokens = tokenize_match_text(candidate, drop_generic=drop_generic)
    if not expected_tokens or not candidate_tokens:
        return 0.0
    overlap = len(set(expected_tokens) & set(candidate_tokens))
    if overlap == 0:
        return 0.0
    precision = overlap / len(set(candidate_tokens))
    recall = overlap / len(set(expected_tokens))
    return (2 * precision * recall) / (precision + recall)


def candidate_match_score(expected: str | None, candidate: str | None) -> float:
    expected_normalized = normalize_match_text(expected)
    candidate_normalized = normalize_match_text(candidate)
    if not expected_normalized or not candidate_normalized:
        return 0.0
    if expected_normalized == candidate_normalized:
        return 1.0
    score = max(
        match_overlap_score(expected, candidate, drop_generic=False),
        match_overlap_score(expected, candidate, drop_generic=True),
    )
    if expected_normalized in candidate_normalized or candidate_normalized in expected_normalized:
        score = max(score, 0.8)
    return min(score, 0.95)


def build_registry_search_queries(
    display_name: str,
    *,
    topics: tuple[str, ...],
) -> tuple[str, ...]:
    queries: list[str] = []
    seen: set[str] = set()

    def add_query(value: str | None) -> None:
        if value is None:
            return
        text = " ".join(value.split())
        normalized = normalize_match_text(text)
        if not normalized or len(normalized) < 3 or normalized in seen:
            return
        seen.add(normalized)
        queries.append(text)

    add_query(display_name)
    informative_phrase = " ".join(tokenize_match_text(display_name, drop_generic=True))
    if informative_phrase and normalize_match_text(informative_phrase) != normalize_match_text(display_name):
        add_query(informative_phrase)
    for topic in topics:
        add_query(topic)
        if len(queries) >= 5:
            break
    return tuple(queries)


def rank_registry_id_candidates(
    scored_candidates: list[tuple[RegistryIdCandidate, float]],
) -> tuple[RegistryIdCandidate, ...]:
    ranked_by_id: dict[str, tuple[RegistryIdCandidate, float]] = {}
    for candidate, score in scored_candidates:
        current = ranked_by_id.get(candidate.discovered_id)
        if current is None:
            ranked_by_id[candidate.discovered_id] = (candidate, score)
            continue
        current_candidate, current_score = current
        if (
            candidate.exact_match and not current_candidate.exact_match
            or (candidate.exact_match == current_candidate.exact_match and score > current_score)
            or (
                candidate.exact_match == current_candidate.exact_match
                and abs(score - current_score) < 1e-9
                and candidate.source_url
                and not current_candidate.source_url
            )
        ):
            ranked_by_id[candidate.discovered_id] = (candidate, score)

    ordered = sorted(
        ranked_by_id.values(),
        key=lambda item: (
            not item[0].exact_match,
            -item[1],
            normalize_match_text(item[0].label),
            item[0].discovered_id,
        ),
    )
    return tuple(candidate for candidate, _score in ordered)


def registry_keywords_for_workstream(
    workstream_id: str,
    registry_artifacts: tuple["M365RegistryArtifact", ...],
    *,
    feedback_events: tuple[Any, ...] = (),
    as_of: datetime | None = None,
) -> tuple[str, ...]:
    from src.core.m365_registry_store import (
        _artifact_meets_auto_promotion_confidence_gate,
        describe_current_m365_registry_promotion_blockers,
    )

    keywords: list[str] = []
    seen: set[str] = set()
    for artifact in registry_artifacts:
        if artifact.inferred_workstream != workstream_id:
            continue
        if not artifact.pm_confirmed and not _artifact_meets_auto_promotion_confidence_gate(artifact):
            continue
        if "recent_rejection" in describe_current_m365_registry_promotion_blockers(
            artifact,
            feedback_events=feedback_events,
            as_of=as_of,
        ):
            continue
        candidates = ((artifact.display_name,) if artifact.display_name is not None else ()) + artifact.topics
        for candidate in candidates:
            normalized = " ".join(str(candidate).split())
            lowered = normalized.lower()
            if not normalized or lowered in seen:
                continue
            seen.add(lowered)
            keywords.append(normalized)
    return tuple(keywords)


def build_m365_discovery_queries(
    *,
    workstreams: tuple[Any, ...],
    registry_artifacts: tuple["M365RegistryArtifact", ...],
    feedback_events: tuple[Any, ...] = (),
    as_of: datetime | None = None,
) -> tuple[str, ...]:
    queries: list[str] = []
    for workstream in workstreams:
        query = build_m365_discovery_query_for_workstream(
            workstream=workstream,
            registry_artifacts=registry_artifacts,
            feedback_events=feedback_events,
            as_of=as_of,
        )
        if query and query not in queries:
            queries.append(query)
    return tuple(queries)


def build_m365_discovery_query_for_workstream(
    *,
    workstream: Any,
    registry_artifacts: tuple["M365RegistryArtifact", ...],
    feedback_events: tuple[Any, ...] = (),
    as_of: datetime | None = None,
) -> str | None:
    terms: list[str] = []
    seen: set[str] = set()
    signal_sources = getattr(workstream, "signal_sources", None)
    keywords = signal_sources.workiq_keywords if signal_sources is not None else ()
    if not keywords:
        keywords = registry_keywords_for_workstream(
            str(workstream.id),
            registry_artifacts,
            feedback_events=feedback_events,
            as_of=as_of,
        )

    seeded_display_names: list[str] = []
    if signal_sources is not None:
        seeded_display_names.extend(
            meeting.display_name
            for meeting in signal_sources.teams_meeting_series
            if meeting.display_name and meeting.series_id is None
        )
        seeded_display_names.extend(
            chat.display_name
            for chat in signal_sources.teams_chats
            if chat.display_name and chat.thread_id is None
        )
        seeded_display_names.extend(
            thread.display_name
            for thread in signal_sources.email_threads
            if thread.display_name and thread.thread_id is None
        )
        seeded_display_names.extend(subject for subject in signal_sources.email_subject_filters if subject)

    for candidate in (
        *seeded_display_names,
        *keywords,
        str(workstream.name),
        *workstream.aliases,
        *(term for term in (workstream.pm_owner, workstream.eng_owner, workstream.alternate_owner) if term),
    ):
        normalized = candidate.strip()
        if not normalized:
            continue
        lowered = normalized.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        terms.append(f'"{normalized}"' if " " in normalized else normalized)
    dri_email = getattr(workstream, "dri_email", None)
    if dri_email and "@" in dri_email:
        alias = dri_email.split("@", 1)[0].strip()
        if alias and alias.lower() not in seen:
            seen.add(alias.lower())
            terms.append(alias)
    if not terms:
        return None
    return " OR ".join(terms[:8])


def build_m365_discovery_query(
    *,
    workstreams: tuple[Any, ...],
    registry_artifacts: tuple["M365RegistryArtifact", ...],
    feedback_events: tuple[Any, ...] = (),
    as_of: datetime | None = None,
) -> str | None:
    queries = build_m365_discovery_queries(
        workstreams=workstreams,
        registry_artifacts=registry_artifacts,
        feedback_events=feedback_events,
        as_of=as_of,
    )
    return queries[0] if queries else None
