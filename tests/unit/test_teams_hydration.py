"""Unit tests for TeamsHydrationProvider."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from src.core.integration_types import (
    ChannelRegistration,
    HydrationMode,
    RegistrationStatus,
    RunContext,
)
from src.m365.graph_calendar_client import CalendarEventPage, CalendarEventRecord
from src.m365.teams_hydration import TeamsHydrationConfig, TeamsHydrationProvider
from src.m365.teams_reader import TeamsMessagePage, TeamsMessageRecord

_NOW = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
_RUN_CTX = RunContext(dry_run=False, force_discovery=False, accept_shrinkage=False)


def _meeting_series_reg(
    ref_id: str = "series-abc",
    workstream_ids: tuple[str, ...] = ("ws-a",),
    work_item_ids: tuple[int, ...] = (),
) -> ChannelRegistration:
    return ChannelRegistration(
        channel="teams",
        program_id="prog1",
        provider_instance_id="default",
        ref_id=ref_id,
        ref_kind="meeting_series",
        status=RegistrationStatus.ACTIVE,
        first_discovered_at=_NOW,
        last_seen_at=_NOW,
        workstream_ids=workstream_ids,
        work_item_ids=work_item_ids,
    )


def _teams_chat_reg(
    ref_id: str = "thread-xyz",
    workstream_ids: tuple[str, ...] = ("ws-a",),
    work_item_ids: tuple[int, ...] = (),
) -> ChannelRegistration:
    return ChannelRegistration(
        channel="teams",
        program_id="prog1",
        provider_instance_id="default",
        ref_id=ref_id,
        ref_kind="teams_chat",
        status=RegistrationStatus.ACTIVE,
        first_discovered_at=_NOW,
        last_seen_at=_NOW,
        workstream_ids=workstream_ids,
        work_item_ids=work_item_ids,
    )


def _calendar_record(
    source_id: str = "evt-001",
    subject: str = "Weekly Sync",
    start_at: str = "2026-05-24T12:00:00+00:00",
) -> CalendarEventRecord:
    return CalendarEventRecord(
        source_id=source_id,
        subject=subject,
        organizer="pm@example.com",
        start_at=start_at,
        end_at="2026-05-24T13:00:00+00:00",
        location=None,
        web_url="https://teams.microsoft.com/meeting/abc",
    )


def _message_record(
    source_id: str = "msg-001",
    sent_at: str = "2026-05-24T12:30:00+00:00",
) -> TeamsMessageRecord:
    return TeamsMessageRecord(
        source_id=source_id,
        channel="Deployment Chat",
        sender="dev@example.com",
        sent_at=sent_at,
        web_url="https://teams.microsoft.com/msg/msg-001",
        preview="Deployment complete.",
    )


def _make_provider(
    calendar_records: tuple[CalendarEventRecord, ...] = (),
    message_records: tuple[TeamsMessageRecord, ...] = (),
) -> TeamsHydrationProvider:
    mock_calendar = MagicMock()
    mock_calendar.search_events.return_value = CalendarEventPage(
        records=calendar_records,
        next_cursor=None,
        source="mock",
    )
    mock_reader = MagicMock()
    mock_reader.search_messages.return_value = TeamsMessagePage(
        records=message_records,
        next_cursor=None,
        source="mock",
    )
    return TeamsHydrationProvider(mock_calendar, mock_reader)


class TestTeamsHydrationMeetingSeries:
    def test_hydrates_meeting_series_to_meeting_events(self) -> None:
        provider = _make_provider(calendar_records=(_calendar_record(),))
        reg = _meeting_series_reg(work_item_ids=(12345,))
        config = TeamsHydrationConfig()

        result = provider.hydrate((reg,), _NOW, "prog1", config, run_ctx=_RUN_CTX)

        assert len(result.resources.meeting_events) == 1
        assert result.resources.meeting_events[0].title == "Weekly Sync"
        assert result.resources.meeting_events[0].series_id == "series-abc"
        assert result.resources.meeting_events[0].workstream_ids == ("ws-a",)
        assert result.resources.meeting_events[0].work_item_ids == (12345,)

    def test_meeting_event_entity_uses_series_id(self) -> None:
        provider = _make_provider(calendar_records=(_calendar_record(source_id="evt-x"),))
        reg = _meeting_series_reg(ref_id="series-abc")
        config = TeamsHydrationConfig()

        result = provider.hydrate((reg,), _NOW, "prog1", config, run_ctx=_RUN_CTX)

        assert result.resources.meeting_events[0].series_id == "series-abc"

    def test_filters_events_before_since(self) -> None:
        old_record = _calendar_record(start_at="2026-01-01T00:00:00+00:00")
        provider = _make_provider(calendar_records=(old_record,))
        reg = _meeting_series_reg()
        config = TeamsHydrationConfig()

        # since = _NOW (2026-05-24), record is 2026-01-01 → should be filtered
        result = provider.hydrate((reg,), _NOW, "prog1", config, run_ctx=_RUN_CTX)

        assert result.resources.meeting_events == ()

    def test_hydrated_ref_ids_updated(self) -> None:
        provider = _make_provider(calendar_records=(_calendar_record(),))
        reg = _meeting_series_reg()
        config = TeamsHydrationConfig()

        result = provider.hydrate((reg,), _NOW, "prog1", config, run_ctx=_RUN_CTX)

        assert ("series-abc", "meeting_series") in result.hydrated_ref_ids

    def test_failed_ref_ids_on_api_error(self) -> None:
        mock_calendar = MagicMock()
        mock_calendar.search_events.side_effect = RuntimeError("API error")
        provider = TeamsHydrationProvider(mock_calendar, MagicMock())
        reg = _meeting_series_reg()
        config = TeamsHydrationConfig()

        result = provider.hydrate((reg,), _NOW, "prog1", config, run_ctx=_RUN_CTX)

        assert ("series-abc", "meeting_series") in result.failed_ref_ids
        assert len(result.errors) == 1
        assert "meeting_series" in result.errors[0].message


class TestTeamsHydrationTeamsChat:
    def test_hydrates_teams_chat_to_thread_messages(self) -> None:
        provider = _make_provider(message_records=(_message_record(),))
        reg = _teams_chat_reg(work_item_ids=(67890,))
        config = TeamsHydrationConfig()

        result = provider.hydrate((reg,), _NOW, "prog1", config, run_ctx=_RUN_CTX)

        assert len(result.resources.thread_messages) == 1
        msg = result.resources.thread_messages[0]
        assert msg.text == "Deployment complete."
        assert msg.thread_id == "thread-xyz"
        assert msg.workstream_ids == ("ws-a",)
        assert msg.work_item_ids == (67890,)

    def test_skips_messages_without_source_id(self) -> None:
        record = TeamsMessageRecord(
            source_id=None,  # no source_id
            channel="Chat",
            sender="user@example.com",
            sent_at="2026-05-24T12:30:00+00:00",
            web_url=None,
            preview="Hello",
        )
        provider = _make_provider(message_records=(record,))
        reg = _teams_chat_reg()
        config = TeamsHydrationConfig()

        result = provider.hydrate((reg,), _NOW, "prog1", config, run_ctx=_RUN_CTX)

        assert result.resources.thread_messages == ()

    def test_permalink_mapped_from_web_url(self) -> None:
        provider = _make_provider(message_records=(_message_record(),))
        reg = _teams_chat_reg()
        config = TeamsHydrationConfig()

        result = provider.hydrate((reg,), _NOW, "prog1", config, run_ctx=_RUN_CTX)

        assert result.resources.thread_messages[0].permalink == "https://teams.microsoft.com/msg/msg-001"


class TestTeamsHydrationMixed:
    def test_combines_meeting_events_and_thread_messages(self) -> None:
        provider = _make_provider(
            calendar_records=(_calendar_record(),),
            message_records=(_message_record(),),
        )
        regs = (_meeting_series_reg(), _teams_chat_reg())
        config = TeamsHydrationConfig()

        result = provider.hydrate(regs, _NOW, "prog1", config, run_ctx=_RUN_CTX)

        assert len(result.resources.meeting_events) == 1
        assert len(result.resources.thread_messages) == 1

    def test_empty_registrations_returns_empty_output(self) -> None:
        provider = _make_provider()
        config = TeamsHydrationConfig()

        result = provider.hydrate((), _NOW, "prog1", config, run_ctx=_RUN_CTX)

        assert result.resources.meeting_events == ()
        assert result.resources.thread_messages == ()
        assert result.errors == ()
        assert result.channel == "teams"
