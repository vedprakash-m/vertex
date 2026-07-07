from __future__ import annotations

from src.core.exceptions import QueryError
from src.m365.graph_mail_client import GraphMailClient


class _FakeBridge:
    def __init__(self) -> None:
        self.tool_calls: list[tuple[str, str, dict[str, object]]] = []
        self.workiq_questions: list[str] = []
        self.tool_responses: list[dict[str, object] | Exception | None] = []
        self.workiq_responses: list[dict[str, object] | Exception | None] = []

    def invoke_mcp_tool(self, server: str, tool: str, args: dict[str, object], *, timeout_seconds: int | None = None):
        del timeout_seconds
        self.tool_calls.append((server, tool, args))
        response = self.tool_responses.pop(0)
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


def test_graph_mail_client_search_emails_prefers_workiq_ask_json() -> None:
    bridge = _FakeBridge()
    bridge.workiq_responses.append(
        {
            "response": '{"emails":[{"id":"mail-1","subject":"Acme weekly draft","threadId":"thread-1","conversationId":"conversation-1","from":{"emailAddress":{"address":"author@example.com"}},"toRecipients":[{"emailAddress":{"address":"acme_newsletter@example.com"}}],"receivedDateTime":"2026-05-07T12:00:00Z","webUrl":"https://outlook.office.com/mail/mail-1","bodyPreview":"Weekly draft body"}],"next_cursor":"cursor-2"}',
        }
    )
    client = GraphMailClient(bridge)

    page = client.search_emails(query="acme_newsletter", limit=10)

    assert bridge.tool_calls == []
    assert len(bridge.workiq_questions) == 1
    assert "acme_newsletter" in bridge.workiq_questions[0]
    assert page.source == "workiq"
    assert page.next_cursor == "cursor-2"
    assert len(page.records) == 1
    assert page.records[0].source_id == "mail-1"
    assert page.records[0].thread_id == "thread-1"
    assert page.records[0].conversation_id == "conversation-1"
    assert page.records[0].sender == "author@example.com"
    assert page.records[0].recipients == ("acme_newsletter@example.com",)


def test_graph_mail_client_search_emails_falls_back_to_legacy_tool_when_ask_returns_none() -> None:
    bridge = _FakeBridge()
    bridge.workiq_responses.append(None)
    bridge.tool_responses.append(
        {
            "emails": [
                {
                    "id": "mail-legacy",
                    "subject": "Legacy fallback",
                }
            ]
        }
    )
    client = GraphMailClient(bridge)

    page = client.search_emails(query="newsletter")

    assert len(bridge.workiq_questions) == 1
    assert bridge.tool_calls == [("workiq", "search_emails", {"query": "newsletter", "limit": 25})]
    assert page.records[0].source_id == "mail-legacy"


def test_graph_mail_client_search_threads_uses_workiq() -> None:
    bridge = _FakeBridge()
    bridge.workiq_responses.append(
        {
            "results": [
                {
                    "messageId": "thread-1",
                    "title": "Rushi feedback",
                    "sender": "rushi@example.com",
                    "snippet": "Please tighten the exec summary",
                    "link": "https://outlook.office.com/mail/thread-1",
                }
            ]
        }
    )
    client = GraphMailClient(bridge)

    page = client.search_threads(question="Find feedback from Rushi on Acme newsletter drafts")

    assert bridge.workiq_questions == ["Find feedback from Rushi on Acme newsletter drafts"]
    assert page.source == "workiq"
    assert len(page.records) == 1
    assert page.records[0].source_id == "thread-1"
    assert page.records[0].subject == "Rushi feedback"
    assert page.records[0].preview == "Please tighten the exec summary"


def test_graph_mail_client_retries_retryable_bridge_errors() -> None:
    bridge = _FakeBridge()
    bridge.workiq_responses.extend(
        [
            QueryError("429 Too Many Requests"),
            {"response": '{"emails":[{"id":"mail-2","subject":"Recovered after retry"}]}'},
        ]
    )
    sleep_calls: list[float] = []
    client = GraphMailClient(bridge, sleep_func=lambda delay: sleep_calls.append(delay))

    page = client.search_emails(query="newsletter")

    assert len(bridge.workiq_questions) == 2
    assert len(page.records) == 1
    assert page.records[0].source_id == "mail-2"
    assert sleep_calls


def test_graph_mail_client_returns_empty_page_when_bridge_has_no_result() -> None:
    bridge = _FakeBridge()
    bridge.workiq_responses.append(None)
    bridge.tool_responses.append(None)
    client = GraphMailClient(bridge)

    page = client.search_emails(query="newsletter")

    assert page.records == ()
    assert page.next_cursor is None
    assert page.source == "workiq"
