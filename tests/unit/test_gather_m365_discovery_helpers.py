from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.commands import gather
from src.core.models_v2 import (
    EmailThreadSource,
    Program,
    TeamsChat,
    TeamsMeetingSeries,
    Workstream,
    WorkstreamSignalSources,
)


@pytest.fixture
def mixed_workstreams() -> tuple[Workstream, ...]:
    return (
        Workstream(id="none", name="No sources"),
        Workstream(
            id="first",
            name="First",
            signal_sources=WorkstreamSignalSources(
                teams_meeting_series=(
                    TeamsMeetingSeries(display_name="Available", series_id="series-1"),
                    TeamsMeetingSeries(
                        display_name="Opted out",
                        series_id="series-optout",
                        include_transcripts=False,
                    ),
                    TeamsMeetingSeries(display_name="Unknown", series_id=None),
                    TeamsMeetingSeries(display_name="Empty", series_id=""),
                    TeamsMeetingSeries(display_name="Whitespace", series_id=" "),
                ),
                teams_chats=(
                    TeamsChat(display_name="Available", thread_id="chat-1"),
                    TeamsChat(display_name="Unknown", thread_id=None),
                    TeamsChat(display_name="Empty", thread_id=""),
                    TeamsChat(display_name="Whitespace", thread_id=" "),
                ),
                email_threads=(
                    EmailThreadSource(display_name="Mail", thread_id="email-1"),
                    EmailThreadSource(display_name="Duplicate", thread_id="chat-1"),
                    EmailThreadSource(display_name="Empty", thread_id=""),
                ),
            ),
        ),
        Workstream(
            id="second",
            name="Second",
            signal_sources=WorkstreamSignalSources(
                teams_meeting_series=(
                    TeamsMeetingSeries(display_name="Duplicate", series_id="series-1"),
                ),
                teams_chats=(TeamsChat(display_name="Duplicate", thread_id="chat-1"),),
            ),
        ),
        Workstream(id="empty", name="Empty sources", signal_sources=WorkstreamSignalSources()),
    )


def test_source_counts_preserve_transcript_opt_out_and_missing_id_semantics(
    mixed_workstreams: tuple[Workstream, ...],
) -> None:
    assert gather._count_transcript_series_state(()) == (0, 0)
    assert gather._count_chat_thread_state(()) == (0, 0)
    assert gather._count_transcript_series_state(mixed_workstreams) == (5, 2)
    assert gather._count_chat_thread_state(mixed_workstreams) == (5, 2)


def test_configured_ids_normalize_deduplicate_and_include_transcript_opt_out(
    mixed_workstreams: tuple[Workstream, ...],
) -> None:
    assert gather._configured_m365_artifact_ids(()) == set()
    assert gather._configured_m365_artifact_ids(mixed_workstreams) == {
        "series-1",
        "series-optout",
        "chat-1",
        "email-1",
    }


def test_configured_ids_use_current_gather_normalizer(
    monkeypatch: pytest.MonkeyPatch,
    mixed_workstreams: tuple[Workstream, ...],
) -> None:
    seen: list[str | None] = []

    def normalize(value: str | None) -> str | None:
        seen.append(value)
        return f"normalized:{value}" if value else None

    monkeypatch.setattr(gather, "normalize_thread_id", normalize)
    assert gather._configured_m365_artifact_ids(mixed_workstreams) == {
        "normalized:series-1",
        "normalized:series-optout",
        "normalized: ",
        "normalized:chat-1",
        "normalized:email-1",
    }
    assert seen == [
        "series-1",
        "series-optout",
        None,
        "",
        " ",
        "chat-1",
        None,
        "",
        " ",
        "email-1",
        "chat-1",
        "",
        "series-1",
        "chat-1",
    ]


def test_configured_id_normalizer_errors_still_propagate(
    monkeypatch: pytest.MonkeyPatch,
    mixed_workstreams: tuple[Workstream, ...],
) -> None:
    def fail(value: str | None) -> str | None:
        raise ValueError("synthetic normalizer failure")

    monkeypatch.setattr(gather, "normalize_thread_id", fail)
    with pytest.raises(ValueError, match="synthetic normalizer failure"):
        gather._configured_m365_artifact_ids(mixed_workstreams)


def test_discovery_state_receives_rebound_gather_callbacks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mixed_workstreams: tuple[Workstream, ...],
) -> None:
    captured: dict[str, Any] = {}
    marker = {"synthetic": True}

    def fake_stage(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return marker

    def transcript_count(workstreams: tuple[Workstream, ...]) -> tuple[int, int]:
        return (11, 3)

    def chat_count(workstreams: tuple[Workstream, ...]) -> tuple[int, int]:
        return (7, 2)

    def tracked_ids(*args: Any, **kwargs: Any) -> set[str]:
        return {"configured"}

    monkeypatch.setattr(gather, "_stage_build_m365_discovery_state", fake_stage)
    monkeypatch.setattr(gather, "_count_transcript_series_state", transcript_count)
    monkeypatch.setattr(gather, "_count_chat_thread_state", chat_count)
    monkeypatch.setattr(gather, "_tracked_m365_artifact_ids", tracked_ids)
    result = gather._build_m365_discovery_state(
        program_id="demo",
        programs_root=tmp_path,
        program=Program(schema_version="2.0", id="demo", name="Demo"),
        workstreams=mixed_workstreams,
        workiq_signals=(),
        gather_flags={"workiq": False},
    )
    assert result is marker
    assert captured["count_transcript_series_state"] is transcript_count
    assert captured["count_chat_thread_state"] is chat_count
    assert captured["tracked_m365_artifact_ids"] is tracked_ids


def test_tracked_ids_use_current_configured_id_callback(
    monkeypatch: pytest.MonkeyPatch,
    mixed_workstreams: tuple[Workstream, ...],
) -> None:
    monkeypatch.setattr(gather, "_configured_m365_artifact_ids", lambda workstreams: {"configured"})
    monkeypatch.setattr(gather, "tracked_registry_thread_ids", lambda artifacts, **kwargs: {"registry"})
    assert gather._tracked_m365_artifact_ids(mixed_workstreams, ()) == {"configured", "registry"}
