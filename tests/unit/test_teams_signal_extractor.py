"""Unit tests for TeamsSignalExtractor."""
from __future__ import annotations

from datetime import datetime, timezone

from src.core.integration_types import MeetingEvent, TeamsHydrationOutput, ThreadMessage
from src.core.teams_signal_extractor import TeamsSignalExtractor

_NOW = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)


def _meeting_event(
    *,
    workstream_ids: tuple[str, ...] = ("ws-a",),
    work_item_ids: tuple[int, ...] = (),
) -> MeetingEvent:
    return MeetingEvent(
        event_id="evt-001",
        series_id="series-abc",
        thread_id="thread-xyz",
        title="Weekly Sync",
        started_at=_NOW,
        ended_at=None,
        organizer="pm@example.com",
        workstream_ids=workstream_ids,
        work_item_ids=work_item_ids,
    )


def _thread_message(
    *,
    workstream_ids: tuple[str, ...] = ("ws-a",),
    work_item_ids: tuple[int, ...] = (),
) -> ThreadMessage:
    return ThreadMessage(
        message_id="msg-001",
        thread_id="thread-chat",
        sender="user@example.com",
        sent_at=_NOW,
        text="Deployment complete",
        workstream_ids=workstream_ids,
        work_item_ids=work_item_ids,
    )


def test_extract_meeting_event_emits_signal() -> None:
    output = TeamsHydrationOutput(meeting_events=(_meeting_event(),), thread_messages=())
    result = TeamsSignalExtractor().extract(output, "prog1")

    assert len(result.signals) == 1
    sig = result.signals[0]
    assert sig.source == "teams"
    assert sig.program_id == "prog1"
    assert sig.workstream_id == "ws-a"
    assert sig.entity_refs == ("teams:series-abc", "WS:ws-a")


def test_extract_thread_message_emits_signal() -> None:
    output = TeamsHydrationOutput(meeting_events=(), thread_messages=(_thread_message(),))
    result = TeamsSignalExtractor().extract(output, "prog1")

    assert len(result.signals) == 1
    sig = result.signals[0]
    assert sig.source == "teams"
    assert sig.entity_refs == ("teams:thread-chat", "WS:ws-a")
    assert sig.text == "Deployment complete"


def test_extract_thread_message_preserves_explicit_work_item_refs() -> None:
    output = TeamsHydrationOutput(
        meeting_events=(),
        thread_messages=(
            ThreadMessage(
                message_id="msg-002",
                thread_id="thread-chat",
                sender="user@example.com",
                sent_at=_NOW,
                text="Need follow-up on WI:12345 before rollout.",
                workstream_ids=("ws-a",),
            ),
        ),
    )
    result = TeamsSignalExtractor().extract(output, "prog1")

    assert result.signals[0].entity_refs == ("teams:thread-chat", "WS:ws-a", "WI:12345")


def test_extract_thread_message_preserves_configured_work_item_refs_without_text_mentions() -> None:
    output = TeamsHydrationOutput(
        meeting_events=(),
        thread_messages=(_thread_message(work_item_ids=(12345, 23456)),),
    )
    result = TeamsSignalExtractor().extract(output, "prog1")

    assert result.signals[0].entity_refs == ("teams:thread-chat", "WS:ws-a", "WI:12345", "WI:23456")


def test_extract_thread_message_splits_atomic_work_item_fragments() -> None:
    output = TeamsHydrationOutput(
        meeting_events=(),
        thread_messages=(
            ThreadMessage(
                message_id="msg-atomic",
                thread_id="thread-chat",
                sender="user@example.com",
                sent_at=_NOW,
                text="Bug 12345 remains blocked on SCHIE.\nTask 67890 mitigation owner confirmed.",
                workstream_ids=("ws-a",),
            ),
        ),
    )
    result = TeamsSignalExtractor().extract(output, "prog1")

    assert len(result.signals) == 2
    assert tuple(signal.entity_refs for signal in result.signals) == (
        ("teams:thread-chat", "WS:ws-a", "WI:12345"),
        ("teams:thread-chat", "WS:ws-a", "WI:67890"),
    )
    assert tuple(signal.metadata["message_id"] for signal in result.signals) == (
        "msg-atomic:seg:0",
        "msg-atomic:seg:1",
    )
    assert all(signal.metadata["parent_message_id"] == "msg-atomic" for signal in result.signals)
    assert result.signals[0].raw_ref != result.signals[1].raw_ref
    assert result.signals[0].raw_ref.endswith(":seg:0")
    assert result.signals[1].raw_ref.endswith(":seg:1")
    assert all(signal.raw_ref.startswith("teams/message/") for signal in result.signals)


def test_extract_fans_out_per_workstream() -> None:
    output = TeamsHydrationOutput(
        meeting_events=(_meeting_event(workstream_ids=("ws-a", "ws-b")),),
        thread_messages=(),
    )
    result = TeamsSignalExtractor().extract(output, "prog1")

    assert len(result.signals) == 2
    assert {s.workstream_id for s in result.signals} == {"ws-a", "ws-b"}
    assert {s.entity_refs for s in result.signals} == {
        ("teams:series-abc", "WS:ws-a"),
        ("teams:series-abc", "WS:ws-b"),
    }


def test_extract_empty_output_returns_empty_signals() -> None:
    output = TeamsHydrationOutput(meeting_events=(), thread_messages=())
    result = TeamsSignalExtractor().extract(output, "prog1")

    assert result.signals == ()
    assert result.errors == ()
    assert result.channel == "teams"


def test_extract_no_workstream_ids_uses_none_workstream() -> None:
    event = MeetingEvent(
        event_id="e1",
        series_id="s1",
        thread_id=None,
        title="Q1 Review",
        started_at=_NOW,
        ended_at=None,
        workstream_ids=(),  # no workstreams
    )
    output = TeamsHydrationOutput(meeting_events=(event,), thread_messages=())
    result = TeamsSignalExtractor().extract(output, "prog1")

    assert len(result.signals) == 1
    assert result.signals[0].workstream_id is None
    assert result.signals[0].entity_refs == ("teams:s1",)


def test_extract_meeting_event_entity_ref_fallback_to_event_id() -> None:
    event = MeetingEvent(
        event_id="evt-fallback",
        series_id=None,  # no series_id
        thread_id=None,
        title="One-off meeting",
        started_at=_NOW,
        ended_at=None,
        workstream_ids=("ws-x",),
    )
    output = TeamsHydrationOutput(meeting_events=(event,), thread_messages=())
    result = TeamsSignalExtractor().extract(output, "prog1")

    assert result.signals[0].entity_refs == ("teams:evt-fallback", "WS:ws-x")


def test_extract_meeting_event_preserves_explicit_work_item_refs_from_title_or_summary() -> None:
    event = MeetingEvent(
        event_id="evt-wi",
        series_id="series-wi",
        thread_id=None,
        title="Review WI:45678 follow-up",
        started_at=_NOW,
        ended_at=None,
        summary="Leadership approved task 56789 mitigation.",
        workstream_ids=("ws-x",),
    )
    output = TeamsHydrationOutput(meeting_events=(event,), thread_messages=())
    result = TeamsSignalExtractor().extract(output, "prog1")

    assert tuple(signal.entity_refs for signal in result.signals) == (
        ("teams:series-wi", "WS:ws-x", "WI:45678"),
        ("teams:series-wi", "WS:ws-x", "WI:56789"),
    )
    assert tuple(signal.metadata["event_id"] for signal in result.signals) == (
        "evt-wi:seg:0",
        "evt-wi:seg:1",
    )
    assert all(signal.metadata["parent_event_id"] == "evt-wi" for signal in result.signals)


def test_extract_meeting_event_deduplicates_configured_and_text_work_item_refs() -> None:
    event = MeetingEvent(
        event_id="evt-config",
        series_id="series-config",
        thread_id=None,
        title="Review WI:45678 follow-up",
        started_at=_NOW,
        ended_at=None,
        summary=None,
        workstream_ids=("ws-x",),
        work_item_ids=(45678, 56789),
    )
    output = TeamsHydrationOutput(meeting_events=(event,), thread_messages=())
    result = TeamsSignalExtractor().extract(output, "prog1")

    assert len(result.signals) == 1
    assert result.signals[0].entity_refs == ("teams:series-config", "WS:ws-x", "WI:45678", "WI:56789")


def test_extract_signal_ids_are_deterministic() -> None:
    output = TeamsHydrationOutput(meeting_events=(_meeting_event(),), thread_messages=())
    result1 = TeamsSignalExtractor().extract(output, "prog1")
    result2 = TeamsSignalExtractor().extract(output, "prog1")

    assert result1.signals[0].id == result2.signals[0].id
