from __future__ import annotations

from src.core.exceptions import QueryError
from src.m365.agency_bridge import AgencyCapabilities
from src.m365.graph_calendar_client import GraphCalendarClient


class _FakeBridge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.workiq_questions: list[str] = []
        self.responses: list[dict[str, object] | Exception | None] = []
        self.workiq_responses: list[dict[str, object] | Exception | None] = []
        self.capabilities = AgencyCapabilities()

    def invoke_mcp_tool(self, server: str, tool: str, args: dict[str, object], *, timeout_seconds: int | None = None):
        del timeout_seconds
        self.calls.append((server, tool, args))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def probe(self) -> AgencyCapabilities:
        return self.capabilities

    def ask_workiq(self, question: str, *, timeout_seconds: int | None = None, allow_cli_fallback: bool = True):
        del timeout_seconds
        self.allow_cli_fallback = allow_cli_fallback
        self.workiq_questions.append(question)
        if not self.workiq_responses:
            return None
        response = self.workiq_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_graph_calendar_client_prefers_workiq_ask_json() -> None:
    bridge = _FakeBridge()
    bridge.workiq_responses.append(
        {
            "response": '{"events":[{"id":"event-1","subject":"Acme Weekly","organizer":{"emailAddress":{"address":"operator@example.com"}},"start":{"dateTime":"2026-05-08T17:00:00Z"},"end":{"dateTime":"2026-05-08T18:00:00Z"},"webUrl":"https://outlook.office.com/calendar/item/1"}],"next_cursor":"next-1"}'
        }
    )
    client = GraphCalendarClient(bridge)

    page = client.search_events(query="Acme Weekly", limit=5)

    assert len(bridge.workiq_questions) == 1
    assert bridge.calls == []
    assert page.records[0].source_id == "event-1"
    assert page.next_cursor == "next-1"


def test_graph_calendar_client_searches_events_via_bluebird() -> None:
    bridge = _FakeBridge()
    bridge.responses.append(
        {
            "events": [
                {
                    "id": "event-1",
                    "subject": "Acme Weekly",
                    "organizer": {"emailAddress": {"address": "operator@example.com"}},
                    "start": {"dateTime": "2026-05-08T17:00:00Z"},
                    "end": {"dateTime": "2026-05-08T18:00:00Z"},
                    "location": {"displayName": "Teams"},
                    "webUrl": "https://outlook.office.com/calendar/item/1",
                }
            ],
            "cursor": "next-1",
        }
    )
    client = GraphCalendarClient(bridge)

    page = client.search_events(query="Acme Weekly", limit=5)

    assert bridge.calls == [("workiq", "get_meetings", {"query": "Acme Weekly", "limit": 5})]
    assert len(page.records) == 1
    assert page.records[0].source_id == "event-1"
    assert page.records[0].organizer == "operator@example.com"
    assert page.next_cursor == "next-1"


def test_graph_calendar_client_retries_retryable_errors() -> None:
    bridge = _FakeBridge()
    bridge.responses.extend([QueryError("503"), {"events": [{"id": "event-2", "subject": "Retry success"}]}])
    sleep_calls: list[float] = []
    client = GraphCalendarClient(bridge, sleep_func=lambda delay: sleep_calls.append(delay))

    page = client.search_events(query="retry me")

    assert len(bridge.calls) == 2
    assert page.records[0].source_id == "event-2"
    assert sleep_calls


def test_graph_calendar_client_falls_back_to_get_meetings_when_calendar_search_unavailable() -> None:
    bridge = _FakeBridge()
    bridge.responses.extend(
        [
            {
                "meetings": [
                    {
                        "meetingId": "meeting-789",
                        "subject": "Adventure Ramp Weekly Sync",
                    }
                ]
            },
        ]
    )
    client = GraphCalendarClient(bridge)

    page = client.search_events(query="Adventure Ramp Weekly Sync", limit=5)

    assert bridge.calls == [
        ("workiq", "get_meetings", {"query": "Adventure Ramp Weekly Sync", "limit": 5}),
    ]
    assert page.records[0].meeting_id == "meeting-789"


def test_graph_calendar_client_preserves_meeting_id_and_join_url_fields() -> None:
    bridge = _FakeBridge()
    bridge.responses.append(
        {
            "events": [
                {
                    "meetingId": "meeting-123",
                    "subject": "Acme Weekly",
                    "joinWebUrl": "https://teams.microsoft.com/l/meeting/details?meetingId=meeting-123",
                },
                {
                    "subject": "Contoso Weekly Review",
                    "onlineMeeting": {
                        "id": "meeting-456",
                        "joinUrl": "https://teams.microsoft.com/l/meeting/details?meetingId=meeting-456",
                    },
                },
            ]
        }
    )
    client = GraphCalendarClient(bridge)

    page = client.search_events(query="weekly")

    assert page.records[0].source_id == "meeting-123"
    assert page.records[0].meeting_id == "meeting-123"
    assert page.records[0].web_url == "https://teams.microsoft.com/l/meeting/details?meetingId=meeting-123"
    assert page.records[1].meeting_id == "meeting-456"
    assert page.records[1].web_url == "https://teams.microsoft.com/l/meeting/details?meetingId=meeting-456"


def test_graph_calendar_client_prefers_get_meetings_when_tool_inventory_lacks_calendar_search() -> None:
    bridge = _FakeBridge()
    bridge.capabilities = AgencyCapabilities(
        available=True,
        has_workiq=True,
        server_tools={"workiq": ("search_emails", "get_meetings")},
    )
    bridge.responses.append(
        {
            "meetings": [
                {
                    "meetingId": "meeting-999",
                    "subject": "Contoso Weekly Review",
                }
            ]
        }
    )
    client = GraphCalendarClient(bridge)

    page = client.search_events(query="Contoso Weekly Review", limit=5)

    assert bridge.calls == [("workiq", "get_meetings", {"query": "Contoso Weekly Review", "limit": 5})]
    assert page.records[0].meeting_id == "meeting-999"


def test_graph_calendar_client_harvests_meeting_link_from_prose_answer() -> None:
    # ask_work_iq frequently answers in prose rather than JSON; the durable meeting link
    # embedded in that prose must still surface as a record so discovery can recover the id.
    bridge = _FakeBridge()
    bridge.workiq_responses.append(
        {
            "response": (
                "I found one recurring meeting that matches:\n"
                "- Acme Weekly Ops Review (organized by Alex Vance): "
                "https://teams.microsoft.com/l/meetup-join/19:meeting_abc123@thread.v2/0\n"
                "Let me know if you want details."
            )
        }
    )
    client = GraphCalendarClient(bridge)

    page = client.search_events(query="Acme Weekly Ops Review", limit=5)

    assert len(page.records) == 1
    assert page.records[0].web_url == "https://teams.microsoft.com/l/meetup-join/19:meeting_abc123@thread.v2/0"
    assert "Acme Weekly Ops Review" in (page.records[0].subject or "")


def test_graph_calendar_client_parses_attendees_and_recurring_metadata() -> None:
    bridge = _FakeBridge()
    bridge.responses.append(
        {
            "events": [
                {
                    "id": "event-1",
                    "subject": "Acme Weekly",
                    "seriesMasterId": "series-1",
                    "type": "occurrence",
                    "recurrence": {"pattern": {"type": "weekly"}},
                    "attendees": [
                        {"emailAddress": {"address": "operator@example.com", "name": "Operator"}},
                        {"emailAddress": {"name": "Eng Lead"}},
                    ],
                }
            ]
        }
    )
    client = GraphCalendarClient(bridge)

    page = client.search_events(query="Acme Weekly")

    assert page.records[0].attendees == ("operator@example.com", "Eng Lead")
    assert page.records[0].is_recurring is True
