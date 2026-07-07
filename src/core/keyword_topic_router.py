from __future__ import annotations

from dataclasses import dataclass
import re

from src.core.m365_router_interface import IM365TopicRouter, M365ReassignCorrection
from src.core.models_v2 import Workstream


@dataclass(frozen=True, slots=True)
class M365RoutingDecision:
    workstream_id: str | None
    confidence: float
    topics: tuple[str, ...]
    confidence_source: str
    reasoning: str


@dataclass(frozen=True, slots=True)
class KeywordM365TopicRouter(IM365TopicRouter):
    def route_artifact(
        self,
        *,
        display_name: str | None,
        subject_or_title: str | None,
        participant_aliases: tuple[str, ...],
        sample_text: str | None,
        workstream_profiles: tuple[Workstream, ...],
        recent_confirmed_signals: dict[str, tuple[str, ...]] | None = None,
        recent_rejected_signals: dict[str, tuple[str, ...]] | None = None,
        recent_reassign_corrections: dict[str, tuple[M365ReassignCorrection, ...]] | None = None,
    ) -> M365RoutingDecision:
        return route_m365_artifact(
            display_name=display_name,
            subject_or_title=subject_or_title,
            participant_aliases=participant_aliases,
            sample_text=sample_text,
            workstreams=workstream_profiles,
            recent_confirmed_signals_by_workstream=recent_confirmed_signals,
            recent_rejected_signals_by_workstream=recent_rejected_signals,
            recent_reassign_corrections_by_workstream=recent_reassign_corrections,
        )


_TOKEN_PATTERN = re.compile(r"[a-z0-9][a-z0-9+._-]{1,}")
_AREA_PATH_SPLIT_PATTERN = re.compile(r"[\\/]+")
_KEYWORD_SUGGESTION_STOPWORDS = {
    "about",
    "after",
    "before",
    "confirmed",
    "from",
    "into",
    "review",
    "status",
    "thread",
    "update",
    "weekly",
    "with",
    "workstream",
}


def route_m365_artifact(
    *,
    display_name: str | None,
    subject_or_title: str | None,
    participant_aliases: tuple[str, ...],
    sample_text: str | None,
    workstreams: tuple[Workstream, ...],
    recent_confirmed_signals_by_workstream: dict[str, tuple[str, ...]] | None = None,
    recent_rejected_signals_by_workstream: dict[str, tuple[str, ...]] | None = None,
    recent_reassign_corrections_by_workstream: dict[str, tuple[M365ReassignCorrection, ...]] | None = None,
) -> M365RoutingDecision:
    if not workstreams:
        return M365RoutingDecision(
            workstream_id=None,
            confidence=0.0,
            topics=(),
            confidence_source="discovered",
            reasoning="No workstreams configured.",
        )

    haystack = _normalize_text(" ".join(part for part in (display_name, subject_or_title, sample_text) if part))
    best_workstream = workstreams[0]
    best_score = -1.0
    best_topics: tuple[str, ...] = ()
    best_reasoning = "No keyword overlap; recorded for manual review."

    for workstream in workstreams:
        score, topics, reasoning = _score_workstream(
            workstream=workstream,
            haystack=haystack,
            participant_aliases=participant_aliases,
            recent_confirmed_signals=(recent_confirmed_signals_by_workstream or {}).get(workstream.id, ()),
            recent_rejected_signals=(recent_rejected_signals_by_workstream or {}).get(workstream.id, ()),
            positive_reassign_corrections=(recent_reassign_corrections_by_workstream or {}).get(workstream.id, ()),
            negative_reassign_corrections=_prior_reassign_corrections_for_workstream(
                workstream.id,
                recent_reassign_corrections_by_workstream or {},
            ),
        )
        if score > best_score:
            best_workstream = workstream
            best_score = score
            best_topics = topics
            best_reasoning = reasoning

    if best_score <= 0.0:
        return M365RoutingDecision(
            workstream_id=best_workstream.id,
            confidence=0.0,
            topics=best_topics,
            confidence_source="discovered",
            reasoning=best_reasoning,
        )

    return M365RoutingDecision(
        workstream_id=best_workstream.id,
        confidence=min(0.95, round(best_score, 2)),
        topics=best_topics,
        confidence_source="keyword",
        reasoning=best_reasoning,
    )


def suggest_keyword_expansions(
    *,
    existing_keywords: tuple[str, ...],
    texts: tuple[str, ...],
    max_suggestions: int = 5,
    min_frequency: int = 2,
) -> tuple[str, ...]:
    normalized_existing_keywords = tuple(
        normalized
        for normalized in (_normalize_text(keyword) for keyword in existing_keywords)
        if normalized
    )
    phrase_document_frequency: dict[str, int] = {}
    unigram_document_frequency: dict[str, int] = {}

    for text in texts:
        tokens = _candidate_tokens(text)
        if not tokens:
            continue
        document_phrase_candidates: set[str] = set()
        document_unigram_candidates: set[str] = set()
        for index, token in enumerate(tokens):
            if not _phrase_is_already_configured(token, normalized_existing_keywords):
                document_unigram_candidates.add(token)
            if index + 1 >= len(tokens):
                continue
            phrase = f"{token} {tokens[index + 1]}"
            if not _phrase_is_already_configured(phrase, normalized_existing_keywords):
                document_phrase_candidates.add(phrase)
        for candidate in document_phrase_candidates:
            phrase_document_frequency[candidate] = phrase_document_frequency.get(candidate, 0) + 1
        for candidate in document_unigram_candidates:
            unigram_document_frequency[candidate] = unigram_document_frequency.get(candidate, 0) + 1

    ranked_candidates = [
        candidate
        for candidate, frequency in sorted(
            phrase_document_frequency.items(),
            key=lambda item: (-item[1], item[0]),
        )
        if frequency >= min_frequency
    ]
    if not ranked_candidates:
        ranked_candidates = [
            candidate
            for candidate, frequency in sorted(
                unigram_document_frequency.items(),
                key=lambda item: (-item[1], item[0]),
            )
            if frequency >= min_frequency
        ]
    return tuple(ranked_candidates[:max_suggestions])


def _score_workstream(
    *,
    workstream: Workstream,
    haystack: str,
    participant_aliases: tuple[str, ...],
    recent_confirmed_signals: tuple[str, ...] = (),
    recent_rejected_signals: tuple[str, ...] = (),
    positive_reassign_corrections: tuple[M365ReassignCorrection, ...] = (),
    negative_reassign_corrections: tuple[M365ReassignCorrection, ...] = (),
) -> tuple[float, tuple[str, ...], str]:
    signal_sources = workstream.signal_sources
    keywords = signal_sources.workiq_keywords if signal_sources is not None else ()
    exclude_keywords = signal_sources.workiq_exclude_keywords if signal_sources is not None else ()
    learned_keywords = suggest_keyword_expansions(existing_keywords=keywords, texts=recent_confirmed_signals)
    learned_exclude_keywords = suggest_keyword_expansions(
        existing_keywords=exclude_keywords,
        texts=recent_rejected_signals,
        min_frequency=1,
    )
    name_terms = _candidate_name_terms(workstream)
    area_path_terms = _candidate_area_path_terms(workstream)
    owner_aliases = _candidate_owner_aliases(workstream)
    normalized_participant_aliases = tuple(dict.fromkeys(alias for alias in (_normalize_alias(value) for value in participant_aliases) if alias))
    matched_keywords = tuple(keyword for keyword in keywords if _contains_phrase(haystack, keyword))
    matched_learned_keywords = tuple(keyword for keyword in learned_keywords if _contains_phrase(haystack, keyword))
    matched_learned_excluded = tuple(keyword for keyword in learned_exclude_keywords if _contains_phrase(haystack, keyword))
    matched_names = tuple(name for name in name_terms if _contains_phrase(haystack, name))
    matched_area_paths = tuple(term for term in area_path_terms if _contains_phrase(haystack, term))
    matched_participants = tuple(alias for alias in normalized_participant_aliases if alias in owner_aliases)
    excluded = tuple(keyword for keyword in exclude_keywords if _contains_phrase(haystack, keyword))
    learned_keyword_evidence_hits = _count_feedback_evidence_hits(recent_confirmed_signals, matched_learned_keywords)
    learned_exclusion_evidence_hits = _count_feedback_evidence_hits(recent_rejected_signals, matched_learned_excluded)
    positive_reassign_hits = _count_reassign_correction_hits(haystack, positive_reassign_corrections)
    negative_reassign_hits = _count_reassign_correction_hits(haystack, negative_reassign_corrections)

    score = 0.0
    if matched_keywords:
        score += 0.45 + min(0.3, 0.12 * len(matched_keywords))
    if matched_learned_keywords:
        score += 0.20 + min(0.28, 0.02 * learned_keyword_evidence_hits)
    if matched_names:
        score += min(0.25, 0.08 * len(matched_names))
    if matched_area_paths:
        score += 0.42 + min(0.24, 0.12 * len(matched_area_paths))
    if matched_participants:
        score += 0.15 + min(0.1, 0.05 * max(0, len(matched_participants) - 1))
    if positive_reassign_hits:
        score += 0.28 + min(0.18, 0.06 * positive_reassign_hits)
    if excluded:
        score = max(0.0, score - 0.25)
    if matched_learned_excluded:
        score = max(0.0, score - (0.35 + min(0.28, 0.02 * learned_exclusion_evidence_hits)))
    if negative_reassign_hits:
        score = max(0.0, score - (0.32 + min(0.18, 0.06 * negative_reassign_hits)))

    topics = tuple(
        dict.fromkeys(
            _topic_slug(keyword)
            for keyword in (*matched_keywords, *matched_learned_keywords, *matched_area_paths)
            if keyword.strip()
        )
    )
    if (
        matched_keywords
        or matched_learned_keywords
        or matched_names
        or matched_area_paths
        or matched_participants
        or matched_learned_excluded
        or positive_reassign_hits
        or negative_reassign_hits
    ):
        reasoning = (
            f"Matched keywords {matched_keywords or ()}, learned phrases {matched_learned_keywords or ()}, names {matched_names or ()}, area-path anchors {matched_area_paths or ()}, and participant aliases {matched_participants or ()}"
            + (
                f"; learned phrase evidence hits {learned_keyword_evidence_hits}"
                if matched_learned_keywords else ""
            )
            + (f"; excluded terms {excluded}" if excluded else "")
            + (
                f"; learned exclusion phrases {matched_learned_excluded}; learned exclusion evidence hits {learned_exclusion_evidence_hits}"
                if matched_learned_excluded else ""
            )
            + (
                f"; structured reassign corrections {positive_reassign_hits}"
                if positive_reassign_hits else ""
            )
            + (
                f"; prior-workstream reassign penalties {negative_reassign_hits}"
                if negative_reassign_hits else ""
            )
            + "."
        )
    else:
        reasoning = "No keyword overlap; recorded for manual review."
    return score, topics, reasoning


def _prior_reassign_corrections_for_workstream(
    workstream_id: str,
    corrections_by_workstream: dict[str, tuple[M365ReassignCorrection, ...]],
) -> tuple[M365ReassignCorrection, ...]:
    return tuple(
        correction
        for corrections in corrections_by_workstream.values()
        for correction in corrections
        if correction.prior_workstream_id == workstream_id
    )


def _count_reassign_correction_hits(
    haystack: str,
    corrections: tuple[M365ReassignCorrection, ...],
) -> int:
    hits = 0
    for correction in corrections:
        phrases = tuple(
            phrase
            for phrase in (correction.artifact_display_name, correction.reason)
            if phrase and phrase.strip()
        )
        if any(_contains_phrase(haystack, phrase) for phrase in phrases):
            hits += 1
    return hits


def _candidate_name_terms(workstream: Workstream) -> tuple[str, ...]:
    candidates: list[str] = []
    for value in (workstream.name, *workstream.aliases):
        text = value.strip()
        if not text:
            continue
        candidates.append(text)
        candidates.extend(token for token in _TOKEN_PATTERN.findall(text.lower()) if len(token) >= 4)
    for owner_val in (workstream.pm_owner, workstream.eng_owner, workstream.alternate_owner):
        if owner_val and owner_val.strip():
            candidates.append(owner_val.strip())
    if workstream.dri_email and "@" in workstream.dri_email:
        candidates.append(workstream.dri_email.split("@", 1)[0].strip())
    return tuple(dict.fromkeys(candidate for candidate in candidates if candidate))


def _candidate_owner_aliases(workstream: Workstream) -> tuple[str, ...]:
    aliases: list[str] = []
    for value in (
        workstream.pm_owner,
        workstream.eng_owner,
        workstream.accountable_owner,
        *workstream.responsible_owners,
        workstream.alternate_owner,
        workstream.dri_email,
        workstream.accountable_email,
        *workstream.consulted_owners,
        *workstream.informed_owners,
        *workstream.always_notify,
        *workstream.aliases,
    ):
        normalized = _normalize_alias(value)
        if normalized:
            aliases.append(normalized)
    return tuple(dict.fromkeys(aliases))


def _candidate_area_path_terms(workstream: Workstream) -> tuple[str, ...]:
    candidates: list[str] = []
    for area_path in workstream.area_paths:
        segments = [segment.strip() for segment in _AREA_PATH_SPLIT_PATTERN.split(area_path) if segment.strip()]
        if not segments:
            continue
        normalized_full = _normalize_text(" ".join(segments))
        if normalized_full:
            candidates.append(normalized_full)
        if len(segments) >= 4:
            penultimate = _normalize_text(segments[-2])
            trailing_pair = _normalize_text(" ".join(segments[-2:]))
            if penultimate:
                candidates.append(penultimate)
            if trailing_pair:
                candidates.append(trailing_pair)
            continue
        normalized_last = _normalize_text(segments[-1])
        if normalized_last and (len(normalized_last.replace(" ", "")) >= 4 or "-" in normalized_last):
            candidates.append(normalized_last)
    return tuple(dict.fromkeys(candidate for candidate in candidates if candidate))


def _contains_phrase(haystack: str, phrase: str) -> bool:
    normalized = _normalize_text(phrase)
    return bool(normalized) and normalized in haystack


def _candidate_tokens(value: str) -> tuple[str, ...]:
    normalized = _normalize_text(value)
    return tuple(
        token
        for token in _TOKEN_PATTERN.findall(normalized)
        if len(token) >= 4 and token not in _KEYWORD_SUGGESTION_STOPWORDS
    )


def _phrase_is_already_configured(phrase: str, existing_keywords: tuple[str, ...]) -> bool:
    return any(phrase == keyword or phrase in keyword or keyword in phrase for keyword in existing_keywords)


def _count_feedback_evidence_hits(texts: tuple[str, ...], phrases: tuple[str, ...]) -> int:
    if not texts or not phrases:
        return 0
    return sum(
        1
        for text in texts
        for phrase in phrases
        if _contains_phrase(_normalize_text(text), phrase)
    )


def _normalize_text(value: str) -> str:
    return " ".join(value.replace("\\", " ").replace("/", " ").lower().split())


def _normalize_alias(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip().lower()
    if not text:
        return None
    if "@" in text:
        return text.split("@", 1)[0].strip() or None
    return text.split()[0].strip() or None


def _topic_slug(value: str) -> str:
    return "-".join(_TOKEN_PATTERN.findall(value.lower())) or "m365"