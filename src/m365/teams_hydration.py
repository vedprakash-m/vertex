from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.config_loader import PROGRAMS_ROOT
from src.core.exceptions import QueryError
from src.core.integration_types import (
    ChannelConfig,
    ChannelRegistration,
    DiscoveryCompleteness,
    HydrationMode,
    HydrationResult,
    IntegrationError,
    MeetingEvent,
    ProviderCapability,
    RunContext,
    TeamsHydrationOutput,
    ThreadMessage,
)
from src.core.models_v2 import Program, Workstream
from src.m365.agency_bridge import AgencyBridge
from src.m365.graph_calendar_client import CalendarEventRecord, GraphCalendarClient
from src.m365.teams_reader import TeamsMessageRecord, TeamsReader


@dataclass(frozen=True, slots=True)
class TeamsHydrationConfig:
    batch_size: int = 50


class TeamsHydrationProvider:
    def __init__(
        self,
        calendar_client: GraphCalendarClient,
        teams_reader: TeamsReader,
    ) -> None:
        self._calendar_client = calendar_client
        self._teams_reader = teams_reader

    @classmethod
    def from_program(
        cls,
        program: Program,
        channel_config: ChannelConfig,
        workstreams: tuple[Workstream, ...],
        *,
        programs_root: Path = PROGRAMS_ROOT,
    ) -> tuple["TeamsHydrationProvider", TeamsHydrationConfig]:
        del program, workstreams, programs_root
        bridge = AgencyBridge()
        calendar_client = GraphCalendarClient(bridge)
        teams_reader = TeamsReader(bridge)
        return cls(calendar_client, teams_reader), TeamsHydrationConfig()

    @property
    def channel(self) -> str:
        return "teams"

    @property
    def capability(self) -> ProviderCapability:
        return ProviderCapability(
            channel="teams",
            discovery_modes=(DiscoveryCompleteness.FULL, DiscoveryCompleteness.INCREMENTAL),
            hydration_modes=(HydrationMode.FULL,),
            supports_since=False,
            max_batch_size=50,
            rate_limit_rpm=60,
            retry_max_attempts=3,
            retry_backoff_seconds=2.0,
            privacy_class="internal_content",
            timeout_seconds=30,
        )

    def hydrate(
        self,
        registrations: tuple[ChannelRegistration, ...],
        since: datetime,
        program_id: str,
        config: TeamsHydrationConfig,
        *,
        mode: HydrationMode = HydrationMode.FULL,
        run_ctx: RunContext,
    ) -> HydrationResult[TeamsHydrationOutput]:
        del mode  # Teams has only FULL mode
        errors: list[IntegrationError] = []
        meeting_events: list[MeetingEvent] = []
        thread_messages: list[ThreadMessage] = []
        hydrated_ref_ids: list[tuple[str, str]] = []
        failed_ref_ids: list[tuple[str, str]] = []
        api_call_count = 0

        for reg in registrations:
            if reg.ref_kind == "meeting_series":
                try:
                    events, calls = self._hydrate_meeting_series(reg, since)
                    meeting_events.extend(events)
                    hydrated_ref_ids.append((reg.ref_id, reg.ref_kind))
                    api_call_count += calls
                except (QueryError, RuntimeError, ValueError) as exc:
                    errors.append(IntegrationError(
                        source="teams",
                        stage="hydration",
                        message=f"Failed to hydrate meeting_series {reg.ref_id}: {exc}",
                        retryable=True,
                    ))
                    failed_ref_ids.append((reg.ref_id, reg.ref_kind))
            elif reg.ref_kind == "teams_chat":
                try:
                    messages, calls = self._hydrate_teams_chat(reg, since)
                    thread_messages.extend(messages)
                    hydrated_ref_ids.append((reg.ref_id, reg.ref_kind))
                    api_call_count += calls
                except (QueryError, RuntimeError, ValueError) as exc:
                    errors.append(IntegrationError(
                        source="teams",
                        stage="hydration",
                        message=f"Failed to hydrate teams_chat {reg.ref_id}: {exc}",
                        retryable=True,
                    ))
                    failed_ref_ids.append((reg.ref_id, reg.ref_kind))

        return HydrationResult(
            channel="teams",
            resources=TeamsHydrationOutput(
                meeting_events=tuple(meeting_events),
                thread_messages=tuple(thread_messages),
            ),
            api_call_count=api_call_count,
            errors=tuple(errors),
            hydrated_ref_ids=tuple(hydrated_ref_ids),
            failed_ref_ids=tuple(failed_ref_ids),
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _hydrate_meeting_series(
        self,
        reg: ChannelRegistration,
        since: datetime,
    ) -> tuple[list[MeetingEvent], int]:
        display_name = (reg.metadata or {}).get("display_name") or reg.ref_title or reg.ref_id
        page = self._calendar_client.search_events(query=str(display_name), limit=25)
        events: list[MeetingEvent] = []
        since_str = since.isoformat()
        for record in page.records:
            if record.start_at and record.start_at < since_str:
                continue
            events.append(_calendar_record_to_meeting_event(record, reg.ref_id, reg.workstream_ids, reg.work_item_ids))
        return events, 1

    def _hydrate_teams_chat(
        self,
        reg: ChannelRegistration,
        since: datetime,
    ) -> tuple[list[ThreadMessage], int]:
        channel_name = (reg.metadata or {}).get("display_name") or reg.ref_title or reg.ref_id
        since_str = since.isoformat()
        page = self._teams_reader.search_messages(
            channel=str(channel_name),
            query=str(channel_name),
            since=since_str,
            limit=50,
        )
        messages: list[ThreadMessage] = []
        for record in page.records:
            if not record.source_id:
                continue
            messages.append(_teams_record_to_thread_message(record, reg.ref_id, reg.workstream_ids, reg.work_item_ids))
        return messages, 1


def _calendar_record_to_meeting_event(
    record: CalendarEventRecord,
    series_id: str,
    workstream_ids: tuple[str, ...],
    work_item_ids: tuple[int, ...],
) -> MeetingEvent:
    started_at = _parse_dt(record.start_at) or datetime.now(timezone.utc)
    ended_at = _parse_dt(record.end_at)
    return MeetingEvent(
        event_id=record.source_id or record.meeting_id or series_id,
        series_id=series_id,
        thread_id=record.meeting_id,
        title=record.subject,
        started_at=started_at,
        ended_at=ended_at,
        organizer=record.organizer,
        workstream_ids=workstream_ids,
        work_item_ids=work_item_ids,
    )


def _teams_record_to_thread_message(
    record: TeamsMessageRecord,
    thread_id: str,
    workstream_ids: tuple[str, ...],
    work_item_ids: tuple[int, ...],
) -> ThreadMessage:
    return ThreadMessage(
        message_id=record.source_id or thread_id,
        thread_id=thread_id,
        sender=record.sender,
        sent_at=_parse_dt(record.sent_at) or datetime.now(timezone.utc),
        text=record.preview or "",
        permalink=record.web_url,
        workstream_ids=workstream_ids,
        work_item_ids=work_item_ids,
    )


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        from datetime import datetime as dt
        parsed = dt.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed
    except (ValueError, TypeError):
        return None
