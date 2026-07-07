from __future__ import annotations

from time import monotonic

from src.core.m365_discovery_support import (
    MatchAliasRule,
    RegistryIdCandidate,
    build_registry_search_queries,
    candidate_match_score,
    normalize_match_text,
    rank_registry_id_candidates,
    use_match_aliases,
)
from src.core.m365_identifiers import normalize_thread_id
from src.m365.agency_bridge import AgencyBridge
from src.m365.teams_reader import TeamsReader
from src.m365.workiq_calendar_discovery import WorkIQCalendarDiscovery
from src.m365.workiq_mail_discovery import WorkIQMailDiscovery


# P4-22: allow the WorkIQ CLI fallback (see workiq_calendar_discovery.py for the
# workiq.exe exit-code-1 rationale). 300s query timeout covers the 40-250s CLI tail;
# 900s artifact budget lets all 3 attempts run (~300s x 3 per artifact).
_DISCOVERY_QUERY_TIMEOUT_SECONDS = 300
_DISCOVERY_ARTIFACT_BUDGET_SECONDS = 900
_DISCOVERY_MAX_QUERY_ATTEMPTS = 3


def discover_meeting_id_candidates(
    display_name: str,
    *,
    limit: int,
    topics: tuple[str, ...] = (),
    owner_aliases: tuple[str, ...] = (),
    bridge: AgencyBridge | None = None,
    match_aliases: tuple[MatchAliasRule, ...] = (),
) -> tuple[RegistryIdCandidate, ...]:
    bridge = bridge or AgencyBridge()
    discovery = WorkIQCalendarDiscovery.from_bridge(bridge)
    with use_match_aliases(match_aliases):
        return discovery.discover_candidates(
            display_name,
            limit=limit,
            topics=topics,
            owner_aliases=owner_aliases,
        )


def discover_thread_id_candidates(
    display_name: str,
    *,
    limit: int,
    topics: tuple[str, ...] = (),
    bridge: AgencyBridge | None = None,
    match_aliases: tuple[MatchAliasRule, ...] = (),
) -> tuple[RegistryIdCandidate, ...]:
    bridge = bridge or AgencyBridge()
    reader = TeamsReader(bridge)
    scored_candidates: list[tuple[RegistryIdCandidate, float]] = []
    started_at = monotonic()
    with use_match_aliases(match_aliases):
        expected = normalize_match_text(display_name)
        for attempt_index, query in enumerate(build_registry_search_queries(display_name, topics=topics), start=1):
            if attempt_index > _DISCOVERY_MAX_QUERY_ATTEMPTS:
                break
            if monotonic() - started_at >= _DISCOVERY_ARTIFACT_BUDGET_SECONDS:
                break
            page = reader.search_messages(
                channel="all",
                query=query,
                limit=limit,
                timeout_seconds=_DISCOVERY_QUERY_TIMEOUT_SECONDS,
                allow_cli_fallback=True,
            )
            found_exact = False
            for record in page.records:
                discovered_id = normalize_thread_id(
                    record.thread_id or record.conversation_id or record.web_url or record.source_id
                )
                if discovered_id is None:
                    continue
                label = record.channel or record.title or discovered_id
                exact_match = normalize_match_text(label) == expected
                score = 1.0 if exact_match else max(
                    candidate_match_score(display_name, label),
                    candidate_match_score(query, label),
                )
                if not exact_match and score < 0.35:
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


def discover_email_thread_candidates(
    display_name: str,
    *,
    limit: int,
    topics: tuple[str, ...] = (),
    owner_aliases: tuple[str, ...] = (),
    bridge: AgencyBridge | None = None,
    match_aliases: tuple[MatchAliasRule, ...] = (),
) -> tuple[RegistryIdCandidate, ...]:
    bridge = bridge or AgencyBridge()
    discovery = WorkIQMailDiscovery.from_bridge(bridge)
    with use_match_aliases(match_aliases):
        return discovery.discover_candidates(
            display_name,
            limit=limit,
            topics=topics,
            owner_aliases=owner_aliases,
        )
