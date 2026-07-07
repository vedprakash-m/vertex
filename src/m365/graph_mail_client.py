from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from src.core.exceptions import QueryError
from src.core.retry import retry_with_backoff
from src.m365.agency_bridge import AgencyBridge
from src.m365.workiq_ask_support import (
    build_mail_search_question,
    coerce_workiq_json_payload,
    extract_source_url_contexts,
    prose_text_from_payload,
)


@dataclass(frozen=True, slots=True)
class MailRecord:
    source_id: str | None
    subject: str | None
    sender: str | None
    recipients: tuple[str, ...]
    received_at: str | None
    web_url: str | None
    preview: str | None
    thread_id: str | None = None
    conversation_id: str | None = None


@dataclass(frozen=True, slots=True)
class MailSearchPage:
    records: tuple[MailRecord, ...]
    next_cursor: str | None
    source: str


class GraphMailClient:
    """Thin mail-search facade over Agency Bridge M365 access."""

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

    def search_emails(
        self,
        *,
        query: str,
        limit: int = 25,
        cursor: str | None = None,
        timeout_seconds: int | None = None,
        allow_cli_fallback: bool = True,
    ) -> MailSearchPage:
        """Search mailbox content through WorkIQ."""

        payload = None
        ask_workiq = getattr(self._bridge, "ask_workiq", None)
        if callable(ask_workiq):
            def _ask() -> dict[str, Any] | None:
                question = build_mail_search_question(query=query, limit=limit, cursor=cursor)
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
            payload = coerce_workiq_json_payload(payload, root_key="emails")
        if payload is None:
            args: dict[str, Any] = {"query": query, "limit": limit}
            if cursor is not None:
                args["cursor"] = cursor
            payload = self._retry(
                lambda: self._bridge.invoke_mcp_tool(
                    "workiq",
                    "search_emails",
                    args,
                    timeout_seconds=timeout_seconds,
                )
            )
        return self._build_page(payload, source="workiq")

    def search_threads(self, *, question: str) -> MailSearchPage:
        """Search email threads through WorkIQ's exploratory mailbox access."""

        payload = self._retry(lambda: self._bridge.ask_workiq(question))
        return self._build_page(payload, source="workiq")

    def _retry(self, callable_fn: Callable[[], dict[str, Any] | None]) -> dict[str, Any] | None:
        kwargs: dict[str, Any] = {
            "max_attempts": self._max_attempts,
            "retry_on": (ConnectionError, QueryError),
        }
        if self._sleep_func is not None:
            kwargs["sleep_func"] = self._sleep_func
        return retry_with_backoff(callable_fn, **kwargs)

    def _build_page(self, payload: dict[str, Any] | None, *, source: str) -> MailSearchPage:
        if payload is None:
            return MailSearchPage(records=(), next_cursor=None, source=source)

        raw_records = payload.get("emails") or payload.get("items") or payload.get("results") or payload.get("messages") or ()
        records: list[MailRecord] = []
        for raw_record in raw_records:
            if not isinstance(raw_record, dict):
                continue
            records.append(
                MailRecord(
                    source_id=_as_optional_str(raw_record.get("id") or raw_record.get("messageId") or raw_record.get("emailId")),
                    subject=_as_optional_str(raw_record.get("subject") or raw_record.get("title")),
                    sender=_coerce_sender(raw_record.get("from") or raw_record.get("sender")),
                    recipients=_coerce_recipients(raw_record.get("to") or raw_record.get("toRecipients")),
                    received_at=_as_optional_str(
                        raw_record.get("receivedDateTime")
                        or raw_record.get("sentDateTime")
                        or raw_record.get("timestamp")
                    ),
                    web_url=_as_optional_str(raw_record.get("webUrl") or raw_record.get("url") or raw_record.get("link")),
                    preview=_as_optional_str(raw_record.get("bodyPreview") or raw_record.get("preview") or raw_record.get("snippet")),
                    thread_id=_as_optional_str(raw_record.get("threadId")),
                    conversation_id=_as_optional_str(raw_record.get("conversationId")),
                )
            )

        if not records:
            records.extend(_records_from_prose(payload))

        next_cursor = _as_optional_str(
            payload.get("next_cursor") or payload.get("nextPageToken") or payload.get("cursor")
        )
        return MailSearchPage(records=tuple(records), next_cursor=next_cursor, source=source)


def _records_from_prose(payload: dict[str, Any]) -> list[MailRecord]:
    """Synthesize mail records from links embedded in a prose ask_work_iq answer.

    The link is carried both as ``web_url`` and ``conversation_id`` so the discovery
    matcher can recover a durable thread identifier from a Teams/Outlook permalink while
    falling back to the link itself for a pending (PM-reviewed) candidate.
    """

    records: list[MailRecord] = []
    for url, context in extract_source_url_contexts(prose_text_from_payload(payload)):
        records.append(
            MailRecord(
                source_id=None,
                subject=_as_optional_str(context),
                sender=None,
                recipients=(),
                received_at=None,
                web_url=url,
                preview=_as_optional_str(context),
                conversation_id=url,
            )
        )
    return records


def _as_optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _coerce_sender(value: Any) -> str | None:
    if isinstance(value, dict):
        email_address = value.get("emailAddress")
        if isinstance(email_address, dict):
            return _as_optional_str(email_address.get("address") or email_address.get("name"))
        return _as_optional_str(value.get("address") or value.get("name"))
    return _as_optional_str(value)


def _coerce_recipients(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    recipients: list[str] = []
    for entry in value:
        if isinstance(entry, dict):
            email_address = entry.get("emailAddress")
            if isinstance(email_address, dict):
                address = _as_optional_str(email_address.get("address") or email_address.get("name"))
            else:
                address = _as_optional_str(entry.get("address") or entry.get("name"))
        else:
            address = _as_optional_str(entry)
        if address is not None:
            recipients.append(address)
    return tuple(recipients)
