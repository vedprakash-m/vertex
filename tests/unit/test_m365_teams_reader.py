from __future__ import annotations

from src.core.exceptions import QueryError
from src.m365.teams_reader import TeamsReader


class _FakeBridge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.workiq_questions: list[str] = []
        self.responses: list[dict[str, object] | Exception | None] = []
        self.workiq_responses: list[dict[str, object] | Exception | None] = []

    def invoke_mcp_tool(self, server: str, tool: str, args: dict[str, object], *, timeout_seconds: int | None = None):
        self.calls.append((server, tool, args))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

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


def test_teams_reader_prefers_workiq_ask_json() -> None:
    bridge = _FakeBridge()
    bridge.workiq_responses.append(
        {
            "response": '{"messages":[{"id":"msg-1","channel":"xInfraSWPM: Acme Weekly","from":{"user":{"displayName":"Vertex Maintainer"}},"createdDateTime":"2026-05-07T17:00:00Z","webUrl":"https://teams.microsoft.com/l/message/1","body":{"content":"Deployment velocity is slipping."}}],"next_cursor":"teams-2"}'
        }
    )
    reader = TeamsReader(bridge)

    page = reader.search_messages(channel="xInfraSWPM: Acme Weekly", query="deployment velocity", since="2026-05-01")

    assert len(bridge.workiq_questions) == 1
    assert bridge.calls == []
    assert page.records[0].preview == "Deployment velocity is slipping."
    assert page.next_cursor == "teams-2"


def test_teams_reader_searches_messages_via_workiq_ask() -> None:
    bridge = _FakeBridge()
    bridge.workiq_responses.append(
        {
            "messages": [
                {
                    "id": "msg-1",
                    "channel": "xInfraSWPM: Acme Weekly",
                    "from": {"user": {"displayName": "Vertex Maintainer"}},
                    "createdDateTime": "2026-05-07T17:00:00Z",
                    "webUrl": "https://teams.microsoft.com/l/message/1",
                    "body": {"content": "Deployment velocity is slipping."},
                }
            ],
            "nextPageToken": "teams-2",
        }
    )
    reader = TeamsReader(bridge)

    page = reader.search_messages(channel="xInfraSWPM: Acme Weekly", query="deployment velocity", since="2026-05-01")

    assert len(bridge.workiq_questions) == 1
    assert bridge.calls == []
    assert len(page.records) == 1
    assert page.records[0].sender == "Vertex Maintainer"
    assert page.records[0].preview == "Deployment velocity is slipping."
    assert page.next_cursor == "teams-2"


def test_teams_reader_retries_retryable_errors() -> None:
    bridge = _FakeBridge()
    bridge.workiq_responses.extend([QueryError("429"), {"messages": [{"id": "msg-2", "snippet": "Recovered"}]}])
    sleep_calls: list[float] = []
    reader = TeamsReader(bridge, sleep_func=lambda delay: sleep_calls.append(delay))

    page = reader.search_messages(channel="xInfraSWPM", query="newsletter")

    assert len(bridge.workiq_questions) == 2
    assert page.records[0].source_id == "msg-2"
    assert sleep_calls


def test_teams_reader_returns_empty_page_when_workiq_returns_none() -> None:
    """NL-only contract: TeamsReader never falls back to invoke_mcp_tool."""
    bridge = _FakeBridge()
    # workiq_responses is empty → ask_workiq returns None
    reader = TeamsReader(bridge)

    page = reader.search_messages(channel="general", query="deployment", since=None)

    assert page.records == ()
    assert page.next_cursor is None
    assert bridge.calls == []
