from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from src.core.exceptions import QueryError
from src.core.retry import retry_with_backoff
from src.m365.agency_bridge import AgencyBridge
from src.m365.workiq_ask_support import (
    build_calendar_search_question,
    coerce_workiq_json_payload,
    extract_source_url_contexts,
    prose_text_from_payload,
)


@dataclass(frozen=True, slots=True)
class CalendarEventRecord:
    source_id: str | None
    subject: str | None
    organizer: str | None
    start_at: str | None
    end_at: str | None
    location: str | None
    web_url: str | None
    attendees: tuple[str, ...] = ()
    meeting_id: str | None = None
    series_master_id: str | None = None
    is_recurring: bool = False


@dataclass(frozen=True, slots=True)
class CalendarEventPage:
    records: tuple[CalendarEventRecord, ...]
    next_cursor: str | None
    source: str


class GraphCalendarClient:
    """Thin calendar-search facade over Agency Bridge M365 access."""

    def __init__(
        self,
        bridge: AgencyBridge,
        *,
        max_attempts: int = 3,
        sleep_func: Callable[[float], None] | None = None,
    ) -> None:
        self._bridge = bridge
        self._max_attempts = max_attempts
        self._sleep_func = sleep_func

    def search_events(
        self,
        *,
        query: str,
        limit: int = 25,
        cursor: str | None = None,
        timeout_seconds: int | None = None,
        allow_cli_fallback: bool = True,
    ) -> CalendarEventPage:
        args: dict[str, Any] = {"query": query, "limit": limit}
        if cursor is not None:
            args["cursor"] = cursor
        payload = self._search_calendar_payload(
            args, timeout_seconds=timeout_seconds, allow_cli_fallback=allow_cli_fallback
        )
        return self._build_page(payload)

    def _search_calendar_payload(
        self,
        args: dict[str, Any],
        *,
        timeout_seconds: int | None = None,
        allow_cli_fallback: bool = True,
    ) -> dict[str, Any] | None:
        ask_workiq = getattr(self._bridge, "ask_workiq", None)
        if callable(ask_workiq):
            def _ask() -> dict[str, Any] | None:
                question = build_calendar_search_question(
                    query=str(args.get("query") or ""),
                    limit=int(args.get("limit") or 25),
                    cursor=_as_optional_str(args.get("cursor")),
                )
                try:
                    return ask_workiq(
                        question,
                        timeout_seconds=timeout_seconds,
                        allow_cli_fallback=allow_cli_fallback,
                    )
                except TypeError:
                    return ask_workiq(question)

            payload = self._retry(
                _ask
            )
            payload = coerce_workiq_json_payload(payload, root_key="events")
            if payload is not None:
                return payload
        for tool_name in self._preferred_calendar_tools():
            payload = self._retry(
                lambda tool_name=tool_name: self._bridge.invoke_mcp_tool(  # type: ignore[misc]
                    "workiq",
                    tool_name,
                    args,
                    timeout_seconds=timeout_seconds,
                )
            )
            if payload is not None:
                return payload
        return None

    def _preferred_calendar_tools(self) -> tuple[str, ...]:
        return ("get_meetings",)

    def _retry(self, callable_fn: Callable[[], dict[str, Any] | None]) -> dict[str, Any] | None:
        kwargs: dict[str, Any] = {
            "max_attempts": self._max_attempts,
            "retry_on": (ConnectionError, QueryError),
        }
        if self._sleep_func is not None:
            kwargs["sleep_func"] = self._sleep_func
        return retry_with_backoff(callable_fn, **kwargs)

    def _build_page(self, payload: dict[str, Any] | None) -> CalendarEventPage:
        if payload is None:
            return CalendarEventPage(records=(), next_cursor=None, source="workiq")

        raw_records = payload.get("events") or payload.get("items") or payload.get("results") or payload.get("meetings") or ()
        records: list[CalendarEventRecord] = []
        for raw_record in raw_records:
            if not isinstance(raw_record, dict):
                continue
            organizer = raw_record.get("organizer")
            if isinstance(organizer, dict):
                organizer = organizer.get("emailAddress", {}).get("address") or organizer.get("address") or organizer.get("name")
            location = raw_record.get("location")
            if isinstance(location, dict):
                location = location.get("displayName") or location.get("name")
            attendees = _attendee_values(raw_record.get("attendees") or raw_record.get("participants"))
            records.append(
                CalendarEventRecord(
                    source_id=_as_optional_str(raw_record.get("id") or raw_record.get("eventId") or raw_record.get("meetingId")),
                    subject=_as_optional_str(raw_record.get("subject") or raw_record.get("title")),
                    organizer=_as_optional_str(organizer),
                    start_at=_as_optional_str(_event_time_value(raw_record.get("start")) or raw_record.get("startDateTime")),
                    end_at=_as_optional_str(_event_time_value(raw_record.get("end")) or raw_record.get("endDateTime")),
                    location=_as_optional_str(location),
                    web_url=_as_optional_str(
                        raw_record.get("joinWebUrl")
                        or raw_record.get("webUrl")
                        or raw_record.get("url")
                        or raw_record.get("link")
                        or _nested_optional_str(raw_record, "onlineMeeting", "joinUrl")
                    ),
                    attendees=attendees,
                    meeting_id=_as_optional_str(raw_record.get("meetingId") or _nested_optional_str(raw_record, "onlineMeeting", "id")),
                    series_master_id=_as_optional_str(raw_record.get("seriesMasterId")),
                    is_recurring=_is_recurring_event(raw_record),
                )
            )

        if not records:
            records.extend(_records_from_prose(payload))

        next_cursor = _as_optional_str(payload.get("next_cursor") or payload.get("nextPageToken") or payload.get("cursor"))
        return CalendarEventPage(records=tuple(records), next_cursor=next_cursor, source="workiq")


def _records_from_prose(payload: dict[str, Any]) -> list[CalendarEventRecord]:
    """Synthesize calendar records from links embedded in a prose ask_work_iq answer.

    When WorkIQ answers in prose instead of JSON, the durable meeting link still carries a
    recoverable identifier. The surrounding prose line becomes the record subject so the
    discovery matcher can score it against the source display name and topics.
    """

    records: list[CalendarEventRecord] = []
    for url, context in extract_source_url_contexts(prose_text_from_payload(payload)):
        records.append(
            CalendarEventRecord(
                source_id=None,
                subject=_as_optional_str(context),
                organizer=None,
                start_at=None,
                end_at=None,
                location=None,
                web_url=url,
            )
        )
    return records


def _event_time_value(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("dateTime") or value.get("value")
    return value


def _attendee_values(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    attendees: list[str] = []
    for item in value:
        attendee = _attendee_value(item)
        if attendee is None:
            continue
        attendees.append(attendee)
    return tuple(attendees)


def _attendee_value(value: Any) -> str | None:
    if isinstance(value, dict):
        email_address = value.get("emailAddress")
        if isinstance(email_address, dict):
            return _as_optional_str(email_address.get("address") or email_address.get("name"))
        organizer = value.get("organizer")
        if isinstance(organizer, dict):
            return _as_optional_str(organizer.get("emailAddress", {}).get("address") or organizer.get("name"))
        user = value.get("user")
        if isinstance(user, dict):
            return _as_optional_str(user.get("email") or user.get("mail") or user.get("displayName"))
        return _as_optional_str(value.get("address") or value.get("name") or value.get("email"))
    return _as_optional_str(value)


def _is_recurring_event(raw_record: dict[str, Any]) -> bool:
    event_type = _as_optional_str(raw_record.get("type"))
    return (
        raw_record.get("recurrence") is not None
        or raw_record.get("seriesMasterId") not in (None, "")
        or event_type in {"seriesMaster", "occurrence", "exception"}
    )


def _as_optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _nested_optional_str(value: Any, *keys: str) -> str | None:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return _as_optional_str(current)
