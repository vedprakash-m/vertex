from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from src.core.m365_discovery_support import (
    build_m365_discovery_query,
    build_m365_discovery_queries,
    build_workstream_match_aliases,
    candidate_match_score,
    compile_match_aliases,
    normalize_match_text,
    use_match_aliases,
)
from src.core.m365_registry_store import M365RegistryArtifact, M365RoutingFeedbackEvent
from src.core.models_v2 import TeamsChat, TeamsMeetingSeries, Workstream, WorkstreamSignalSources


@dataclass(frozen=True)
class _Workstream:
    id: str
    name: str
    aliases: tuple[str, ...] = ()


def test_core_normalizer_carries_no_program_knowledge_by_default() -> None:
    # With no active aliases, the matcher must be purely generic — no hardcoded program
    # synonyms. "Contoso" must NOT silently canonicalize to "direct drive northwind".
    assert normalize_match_text("Contoso Pilot Sync") == "contoso pilot sync"
    assert normalize_match_text("Contoso Weekly Review") == "contoso weekly review"


def test_use_match_aliases_canonicalizes_only_within_scope_and_resets() -> None:
    aliases = compile_match_aliases([("Direct Drive on Northwind", ["contoso", "dd-acme"])])
    with use_match_aliases(aliases):
        # Both the dashed and the run-together variants resolve to the canonical name.
        assert normalize_match_text("Contoso Weekly Review") == "direct drive on northwind weekly review"
        assert normalize_match_text("Contoso Sync") == "direct drive on northwind sync"
    # Context resets: the alias does not leak to later normalization calls.
    assert normalize_match_text("Contoso Weekly Review") == "contoso weekly review"


def test_aliases_lift_candidate_match_score_for_abbreviations() -> None:
    aliases = compile_match_aliases([("Direct Drive on Northwind", ["contoso", "dd-acme"])])
    without = candidate_match_score("Direct Drive on Northwind", "Contoso Weekly Review")
    with use_match_aliases(aliases):
        with_alias = candidate_match_score("Direct Drive on Northwind", "Contoso Weekly Review")
    assert with_alias > without


def test_build_workstream_match_aliases_sources_from_config() -> None:
    workstreams = (
        _Workstream(id="dd_on_pf", name="Direct Drive on Northwind", aliases=("contoso", "contoso", "dd-acme")),
        _Workstream(id="acme", name="Adventure on Northwind", aliases=("adventure-pf",)),
        _Workstream(id="no_alias", name="No Alias Stream", aliases=()),
    )
    aliases = build_workstream_match_aliases(workstreams)
    with use_match_aliases(aliases):
        assert normalize_match_text("Contoso blocker") == "direct drive on northwind blocker"
        assert normalize_match_text("adventure-pf ramp") == "adventure on northwind ramp"


def test_build_m365_discovery_query_excludes_recently_rejected_registry_keywords() -> None:
    workstreams = (
        Workstream(
            id="ws1",
            name="Ramp",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(),
        ),
    )
    registry_artifacts = (
        M365RegistryArtifact(
            artifact_id="thread:named:acme-ramp-thread",
            artifact_type="email_thread",
            inferred_workstream="ws1",
            confidence=1.0,
            confidence_source="pm_confirmed",
            pm_confirmed=True,
            promoted_to_workstreams_yaml=False,
            first_seen=date(2026, 5, 1),
            last_seen=date(2026, 5, 10),
            display_name="Ramp Thread",
            thread_id="thread-1",
            topics=("SCHIE gaps",),
            routing_reasoning="Previously routed to Ramp.",
        ),
    )
    feedback_events = (
        M365RoutingFeedbackEvent(
            ts=datetime(2026, 5, 10, 7, 0, tzinfo=timezone.utc),
            artifact_id="thread:named:acme-ramp-thread",
            action="reject",
            pm_alias="operator",
        ),
    )

    query = build_m365_discovery_query(
        workstreams=workstreams,
        registry_artifacts=registry_artifacts,
        feedback_events=feedback_events,
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    assert query == "Ramp"


def test_build_m365_discovery_queries_preserve_workstream_seeded_terms() -> None:
    workstreams = (
        Workstream(
            id="dd_on_pf",
            name="Direct Drive Northwind",
            area_paths=("One\\Devices\\Contoso",),
            signal_sources=WorkstreamSignalSources(
                workiq_keywords=(
                    "Contoso pilot",
                    "pilot readiness",
                    "Kiona",
                    "AutoTSG",
                    "GFU",
                    "GFU SSD",
                    "DD performance",
                    "firmware sign-off",
                ),
                teams_meeting_series=(TeamsMeetingSeries(display_name="Contoso Weekly Review", series_id=None),),
            ),
        ),
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(
                teams_meeting_series=(
                    TeamsMeetingSeries(display_name="Acme Weekly Ops Review", series_id=None),
                    TeamsMeetingSeries(display_name="Adventure Ramp Weekly Sync", series_id=None),
                ),
                teams_chats=(TeamsChat(display_name="Acme Eng Core Chat", thread_id=None),),
            ),
        ),
    )

    queries = build_m365_discovery_queries(
        workstreams=workstreams,
        registry_artifacts=(),
        feedback_events=(),
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    assert len(queries) == 2
    assert "Contoso Weekly Review" in queries[0]
    assert "Acme Weekly Ops Review" in queries[1]
    assert "Adventure Ramp Weekly Sync" in queries[1]
    assert "Acme Eng Core Chat" in queries[1]
    assert "Adventure on Northwind" in queries[1]
