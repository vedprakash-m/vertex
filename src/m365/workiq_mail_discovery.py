from __future__ import annotations

from difflib import SequenceMatcher
from time import monotonic

from src.core.m365_discovery_support import (
    RegistryIdCandidate,
    build_registry_search_queries,
    candidate_match_score,
    normalize_match_text,
    rank_registry_id_candidates,
    tokenize_match_text,
)
from src.core.m365_identifiers import normalize_thread_id
from src.m365.agency_bridge import AgencyBridge
from src.m365.graph_mail_client import GraphMailClient, MailRecord


# P4-22: allow the WorkIQ CLI fallback (see workiq_calendar_discovery.py for the
# workiq.exe exit-code-1 rationale). 300s query timeout covers the 40-250s CLI tail;
# 900s artifact budget lets all 3 attempts run (~300s x 3 per artifact).
_DISCOVERY_QUERY_TIMEOUT_SECONDS = 300
_DISCOVERY_ARTIFACT_BUDGET_SECONDS = 900
_DISCOVERY_MAX_QUERY_ATTEMPTS = 3
_DISCOVERY_MIN_SCORE = 0.35


class WorkIQMailDiscovery:
    def __init__(self, *, mail_client: GraphMailClient) -> None:
        self._mail_client = mail_client

    @classmethod
    def from_bridge(cls, bridge: AgencyBridge) -> "WorkIQMailDiscovery":
        return cls(mail_client=GraphMailClient(bridge))

    def discover_candidates(
        self,
        display_name: str,
        *,
        limit: int,
        topics: tuple[str, ...] = (),
        owner_aliases: tuple[str, ...] = (),
    ) -> tuple[RegistryIdCandidate, ...]:
        expected = normalize_match_text(display_name)
        normalized_owners = _normalize_owner_aliases(owner_aliases)
        scored_candidates: list[tuple[RegistryIdCandidate, float]] = []
        started_at = monotonic()
        for attempt_index, query in enumerate(build_registry_search_queries(display_name, topics=topics), start=1):
            if attempt_index > _DISCOVERY_MAX_QUERY_ATTEMPTS:
                break
            if monotonic() - started_at >= _DISCOVERY_ARTIFACT_BUDGET_SECONDS:
                break
            page = self._mail_client.search_emails(
                query=query,
                limit=limit,
                timeout_seconds=_DISCOVERY_QUERY_TIMEOUT_SECONDS,
                allow_cli_fallback=True,
            )
            found_exact = False
            for record in page.records:
                raw_id = record.thread_id or record.conversation_id
                if raw_id is None:
                    continue
                discovered_id = normalize_thread_id(raw_id)
                if discovered_id is None:
                    continue
                label = record.subject or discovered_id
                exact_match = normalize_match_text(label) == expected
                score = 1.0 if exact_match else _mail_match_score(
                    display_name=display_name,
                    query=query,
                    topics=topics,
                    owner_aliases=normalized_owners,
                    record=record,
                )
                if not exact_match and score < _DISCOVERY_MIN_SCORE:
                    continue
                scored_candidates.append(
                    (
                        RegistryIdCandidate(
                            discovered_id=discovered_id,
                            label=label,
                            source_url=record.web_url,
                            exact_match=exact_match,
                            match_score=score,
                        ),
                        score,
                    )
                )
                found_exact = found_exact or exact_match
            if found_exact:
                break
        return rank_registry_id_candidates(scored_candidates)


def _mail_match_score(
    *,
    display_name: str,
    query: str,
    topics: tuple[str, ...],
    owner_aliases: tuple[str, ...],
    record: MailRecord,
) -> float:
    subject = record.subject
    preview = record.preview
    subject_score = max(
        candidate_match_score(display_name, subject),
        candidate_match_score(query, subject),
        _sequence_similarity(display_name, subject),
    )
    topic_score = _keyword_overlap(topics, (subject, preview))
    owner_score = _owner_overlap(owner_aliases, record)
    preview_score = max(
        candidate_match_score(display_name, preview),
        candidate_match_score(query, preview),
        _sequence_similarity(display_name, preview),
    )
    score = (
        subject_score * 0.72
        + topic_score * 0.14
        + owner_score * 0.08
        + preview_score * 0.06
    )
    return min(score, 0.98)


def _keyword_overlap(keywords: tuple[str, ...], candidate_texts: tuple[str | None, ...]) -> float:
    normalized_keywords = {
        token
        for keyword in keywords
        for token in tokenize_match_text(keyword, drop_generic=True)
        if token
    }
    if not normalized_keywords:
        return 0.0
    normalized_candidate_tokens = {
        token
        for value in candidate_texts
        for token in tokenize_match_text(value, drop_generic=True)
        if token
    }
    if not normalized_candidate_tokens:
        return 0.0
    overlap = normalized_keywords & normalized_candidate_tokens
    return len(overlap) / len(normalized_keywords)


def _normalize_owner_aliases(owner_aliases: tuple[str, ...]) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for owner in owner_aliases:
        normalized = normalize_match_text(owner)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return tuple(ordered)


def _owner_overlap(owner_aliases: tuple[str, ...], record: MailRecord) -> float:
    if not owner_aliases:
        return 0.0
    observed_people = {
        normalize_match_text(value)
        for value in (record.sender, *record.recipients)
        if normalize_match_text(value)
    }
    if not observed_people:
        return 0.0
    for owner in owner_aliases:
        owner_tokens = set(tokenize_match_text(owner, drop_generic=False))
        for person in observed_people:
            person_tokens = set(tokenize_match_text(person, drop_generic=False))
            if not owner_tokens or not person_tokens:
                continue
            if owner_tokens <= person_tokens or person_tokens <= owner_tokens:
                return 1.0
    return 0.0


def _sequence_similarity(expected: str, candidate: str | None) -> float:
    normalized_expected = normalize_match_text(expected)
    normalized_candidate = normalize_match_text(candidate)
    if not normalized_expected or not normalized_candidate:
        return 0.0
    return SequenceMatcher(None, normalized_expected, normalized_candidate).ratio()
