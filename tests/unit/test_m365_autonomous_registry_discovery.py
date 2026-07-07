from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.core.keyword_topic_router import M365RoutingDecision
from src.core.m365_registry_store import load_m365_registry
from src.core.models_v2 import Workstream, WorkstreamSignalSources
from src.m365.autonomous_registry_discovery import run_m365_discovery_pass


class _FakeWorkIQBridge:
    def __init__(self, *, tool_payloads: dict[str, dict[str, Any]], workiq_responses: list[dict[str, Any] | None] | None = None) -> None:
        self._tool_payloads = tool_payloads
        self._workiq_responses: list[dict[str, Any] | None] = list(workiq_responses or [])
        self.tool_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.workiq_questions: list[str] = []

    def invoke_mcp_tool(
        self,
        server_name: str,
        tool_name: str,
        args: dict[str, Any],
        timeout_seconds: int | None = None,
    ) -> dict[str, Any] | None:
        del timeout_seconds
        self.tool_calls.append((server_name, tool_name, args))
        return self._tool_payloads.get(tool_name)

    def ask_workiq(self, question: str, *, timeout_seconds: int | None = None, allow_cli_fallback: bool = True) -> dict[str, Any] | None:
        del timeout_seconds, allow_cli_fallback
        self.workiq_questions.append(question)
        if self._workiq_responses:
            return self._workiq_responses.pop(0)
        return None

    def last_mcp_error(self) -> str | None:
        return None


class _FakeM365TopicRouter:
    def __init__(self, *, workstream_id: str, confidence: float, topics: tuple[str, ...], reasoning: str) -> None:
        self._decision = M365RoutingDecision(
            workstream_id=workstream_id,
            confidence=confidence,
            topics=topics,
            confidence_source="router",
            reasoning=reasoning,
        )

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
        recent_reassign_corrections=None,
    ) -> M365RoutingDecision:
        del (
            display_name,
            subject_or_title,
            participant_aliases,
            sample_text,
            workstream_profiles,
            recent_confirmed_signals,
            recent_rejected_signals,
            recent_reassign_corrections,
        )
        return self._decision


def test_run_m365_discovery_pass_discovers_meeting_series_from_calendar(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    workstreams = (
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(workiq_keywords=("SCHIE",)),
        ),
    )
    bridge = _FakeWorkIQBridge(
        tool_payloads={
            "search_emails": {"emails": []},
        },
        workiq_responses=[
            {
                "response": json.dumps({
                    "events": [
                        {
                            "id": "event-1",
                            "meetingId": "meeting-occurrence-1",
                            "seriesMasterId": "series-123",
                            "subject": "Acme Weekly Ops Review",
                            "organizer": {"emailAddress": {"address": "operator@example.com"}},
                            "attendees": [{"emailAddress": {"address": "owner@example.com"}}],
                            "startDateTime": "2026-05-10T09:00:00Z",
                        }
                    ]
                })
            },
        ],
    )

    discovered_artifacts, discovery_errors = run_m365_discovery_pass(
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        workstreams=workstreams,
        bridge_client=bridge,
        topic_router=_FakeM365TopicRouter(
            workstream_id="acme",
            confidence=0.87,
            topics=("SCHIE", "ops"),
            reasoning="Recurring northwind ops meeting belongs to Acme.",
        ),
        programs_root=programs_root,
        result_limit=50,
    )

    registry = load_m365_registry("acme", programs_root)

    assert discovery_errors == ()
    assert len(discovered_artifacts) == 1
    assert discovered_artifacts[0].artifact_type == "meeting_series"
    assert discovered_artifacts[0].series_id == "series-123"
    assert discovered_artifacts[0].display_name == "Acme Weekly Ops Review"
    assert len(registry.artifacts) == 1
    assert registry.artifacts[0].artifact_type == "meeting_series"
    assert registry.artifacts[0].series_id == "series-123"


def test_run_m365_discovery_pass_dedupes_same_thread_across_email_and_teams(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    workstreams = (
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(workiq_keywords=("SCHIE",)),
        ),
    )
    bridge = _FakeWorkIQBridge(
        tool_payloads={
            "search_emails": {
                "emails": [
                    {
                        "messageId": "search-mail-1",
                        "threadId": "shared-thread-1",
                        "subject": "Shared Ramp Thread",
                        "snippet": "SCHIE status moved again.",
                        "receivedDateTime": "2026-05-10T09:00:00Z",
                    }
                ]
            },
        },
    )

    discovered_artifacts, discovery_errors = run_m365_discovery_pass(
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        workstreams=workstreams,
        bridge_client=bridge,
        topic_router=_FakeM365TopicRouter(
            workstream_id="acme",
            confidence=0.72,
            topics=("SCHIE",),
            reasoning="Shared durable thread belongs to Acme.",
        ),
        programs_root=programs_root,
        result_limit=50,
    )

    registry = load_m365_registry("acme", programs_root)

    assert discovery_errors == ()
    assert len(discovered_artifacts) == 1
    assert discovered_artifacts[0].thread_id == "shared-thread-1"
    assert discovered_artifacts[0].artifact_type == "email_thread"
    assert len(registry.artifacts) == 1
    assert registry.artifacts[0].thread_id == "shared-thread-1"
    assert registry.artifacts[0].artifact_type == "email_thread"
