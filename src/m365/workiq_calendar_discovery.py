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
from src.core.m365_identifiers import normalize_meeting_id
from src.m365.agency_bridge import AgencyBridge
from src.m365.graph_calendar_client import CalendarEventRecord, GraphCalendarClient
from src.m365.transcript_reader import TranscriptReader


# P4-22: workiq.exe exits with code 1 after every request (unconditional, not an
# error signal), so callers with allow_cli_fallback=False see every MCP call as a
# failure and silently return zero candidates. The CLI path is the reliable one, so
# these callers now allow it. The 300s query timeout safely covers the empirically
# observed 40-250s WorkIQ CLI tail latency (a 90s timeout failed silently at 91-250s).
_DISCOVERY_QUERY_TIMEOUT_SECONDS = 300
# Raised from 120 → 900 so all _DISCOVERY_MAX_QUERY_ATTEMPTS (=3) queries can actually
# run at the 300s tail (~300s x 3 = ~15 min per artifact). Run discovery sweeps in a
# background session; do not block the newsletter workflow on them.
_DISCOVERY_ARTIFACT_BUDGET_SECONDS = 900
_DISCOVERY_MAX_QUERY_ATTEMPTS = 3
_DISCOVERY_MIN_SCORE = 0.35
_TRANSCRIPT_PROBE_CAP = 2


class WorkIQCalendarDiscovery:
    def __init__(
        self,
        *,
        calendar_client: GraphCalendarClient,
        transcript_reader: TranscriptReader,
    ) -> None:
        self._calendar_client = calendar_client
        self._transcript_reader = transcript_reader

    @classmethod
    def from_bridge(cls, bridge: AgencyBridge) -> "WorkIQCalendarDiscovery":
        return cls(
            calendar_client=GraphCalendarClient(bridge),
            transcript_reader=TranscriptReader(bridge),
        )

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
        transcript_probes = 0
        transcript_titles: dict[str, str | None] = {}
        for attempt_index, query in enumerate(build_registry_search_queries(display_name, topics=topics), start=1):
            if attempt_index > _DISCOVERY_MAX_QUERY_ATTEMPTS:
                break
            if monotonic() - started_at >= _DISCOVERY_ARTIFACT_BUDGET_SECONDS:
                break
            page = self._calendar_client.search_events(
                query=query,
                limit=limit,
                timeout_seconds=_DISCOVERY_QUERY_TIMEOUT_SECONDS,
                allow_cli_fallback=True,
            )
            found_exact = False
            for record in page.records:
                discovered_id = normalize_meeting_id(
                    record.series_master_id or record.meeting_id or record.web_url or record.source_id
                )
                if discovered_id is None:
                    continue
                label = record.subject or record.meeting_id or record.series_master_id or record.source_id or discovered_id
                exact_match = normalize_match_text(label) == expected
                transcript_title = None
                if (
                    record.meeting_id
                    and transcript_probes < _TRANSCRIPT_PROBE_CAP
                    and monotonic() - started_at < _DISCOVERY_ARTIFACT_BUDGET_SECONDS
                ):
                    if record.meeting_id not in transcript_titles:
                        transcript = self._transcript_reader.get_transcript(meeting_id=record.meeting_id)
                        transcript_titles[record.meeting_id] = transcript.title if transcript is not None else None
                        transcript_probes += 1
                    transcript_title = transcript_titles.get(record.meeting_id)
                elif record.meeting_id:
                    transcript_title = transcript_titles.get(record.meeting_id)
                score = 1.0 if exact_match else _meeting_match_score(
                    display_name=display_name,
                    query=query,
                    topics=topics,
                    owner_aliases=normalized_owners,
                    label=label,
                    record=record,
                    transcript_title=transcript_title,
                )
                transcript_exact = transcript_title is not None and normalize_match_text(transcript_title) == expected
                exact_match = exact_match or transcript_exact
                if exact_match:
                    score = 1.0
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


def _meeting_match_score(
    *,
    display_name: str,
    query: str,
    topics: tuple[str, ...],
    owner_aliases: tuple[str, ...],
    label: str,
    record: CalendarEventRecord,
    transcript_title: str | None,
) -> float:
    title_score = max(
        candidate_match_score(display_name, label),
        candidate_match_score(query, label),
        _sequence_similarity(display_name, label),
    )
    topic_score = _keyword_overlap(topics, (label, transcript_title))
    owner_score = _owner_overlap(owner_aliases, record)
    recurrence_bonus = 0.06 if record.is_recurring else 0.0
    transcript_bonus = 0.0
    if transcript_title:
        transcript_bonus = max(
            candidate_match_score(display_name, transcript_title),
            _sequence_similarity(display_name, transcript_title),
        ) * 0.12
    score = (
        title_score * 0.72
        + topic_score * 0.14
        + owner_score * 0.08
        + recurrence_bonus
        + transcript_bonus
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


def _owner_overlap(owner_aliases: tuple[str, ...], record: CalendarEventRecord) -> float:
    if not owner_aliases:
        return 0.0
    observed_people = {
        normalize_match_text(value)
        for value in (record.organizer, *record.attendees)
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
