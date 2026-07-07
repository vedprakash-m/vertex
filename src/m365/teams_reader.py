from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from src.core.exceptions import QueryError
from src.core.retry import retry_with_backoff
from src.m365.agency_bridge import AgencyBridge
from src.m365.workiq_ask_support import (
    build_teams_search_question,
    coerce_workiq_json_payload,
    extract_source_url_contexts,
    prose_text_from_payload,
)


@dataclass(frozen=True, slots=True)
class TeamsMessageRecord:
    source_id: str | None
    channel: str | None
    sender: str | None
    sent_at: str | None
    web_url: str | None
    preview: str | None
    thread_id: str | None = None
    conversation_id: str | None = None
    title: str | None = None


@dataclass(frozen=True, slots=True)
class TeamsMessagePage:
    records: tuple[TeamsMessageRecord, ...]
    next_cursor: str | None
    source: str


class TeamsReader:
    """Thin Teams-message reader over Agency Bridge M365 access."""

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

    def search_teams(
        self,
        *,
        channel: str,
        query: str,
        since: str | None = None,
        limit: int = 25,
        cursor: str | None = None,
        timeout_seconds: int | None = None,
        allow_cli_fallback: bool = True,
    ) -> TeamsMessagePage:
        """Search Teams messages, preferring the typed Graph API path when available.

        Tries ``bridge.search_teams(**kwargs)`` first (GAP-17 typed path).
        Falls back to ``ask_workiq`` when the typed path is unavailable, returns
        no records, or raises an exception.
        """
        bridge_search_teams = getattr(self._bridge, "search_teams", None)
        if callable(bridge_search_teams):
            try:
                try:
                    raw = bridge_search_teams(
                        channel=channel, query=query, since=since, limit=limit, cursor=cursor
                    )
                except TypeError:
                    raw = bridge_search_teams(channel=channel, query=query)
            except Exception:
                raw = None
            if raw is not None:
                page = self._build_typed_page(raw)
                if page.records:
                    return page
        return self.search_messages(
            channel=channel,
            query=query,
            since=since,
            limit=limit,
            cursor=cursor,
            timeout_seconds=timeout_seconds,
            allow_cli_fallback=allow_cli_fallback,
        )

    def _build_typed_page(self, raw: Any) -> TeamsMessagePage:
        """Parse a typed Graph API payload into a TeamsMessagePage with source='graph'."""
        if isinstance(raw, list):
            items: list[Any] = raw
        elif isinstance(raw, dict):
            items = raw.get("value") or []
        else:
            items = []
        records: list[TeamsMessageRecord] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            records.append(
                TeamsMessageRecord(
                    source_id=_as_optional_str(item.get("id") or item.get("messageId")),
                    channel=_as_optional_str(item.get("channel") or item.get("teamChannel")),
                    sender=_as_optional_str(_sender_value(item.get("from") or item.get("sender"))),
                    sent_at=_as_optional_str(item.get("createdDateTime") or item.get("timestamp")),
                    web_url=_as_optional_str(item.get("webUrl") or item.get("url")),
                    preview=_as_optional_str(item.get("bodyPreview") or item.get("preview") or _body_content(item.get("body"))),
                    thread_id=_as_optional_str(item.get("threadId")),
                    conversation_id=_as_optional_str(item.get("conversationId")),
                    title=_as_optional_str(item.get("title") or item.get("subject")),
                )
            )
        return TeamsMessagePage(records=tuple(records), next_cursor=None, source="graph")

    def search_messages(
        self,
        *,
        channel: str,
        query: str,
        since: str | None = None,
        limit: int = 25,
        cursor: str | None = None,
        timeout_seconds: int | None = None,
        allow_cli_fallback: bool = True,
    ) -> TeamsMessagePage:
        payload = None
        ask_workiq = getattr(self._bridge, "ask_workiq", None)
        if callable(ask_workiq):
            def _ask() -> dict[str, Any] | None:
                question = build_teams_search_question(
                    channel=channel,
                    query=query,
                    since=since,
                    limit=limit,
                    cursor=cursor,
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
            payload = coerce_workiq_json_payload(payload, root_key="messages")
        return self._build_page(payload)

    def _retry(self, callable_fn: Callable[[], dict[str, Any] | None]) -> dict[str, Any] | None:
        kwargs: dict[str, Any] = {
            "max_attempts": self._max_attempts,
            "retry_on": (ConnectionError, QueryError),
        }
        if self._sleep_func is not None:
            kwargs["sleep_func"] = self._sleep_func
        return retry_with_backoff(callable_fn, **kwargs)

    def _build_page(self, payload: dict[str, Any] | None) -> TeamsMessagePage:
        if payload is None:
            return TeamsMessagePage(records=(), next_cursor=None, source="workiq")

        raw_records = payload.get("messages") or payload.get("items") or payload.get("results") or ()
        records: list[TeamsMessageRecord] = []
        for raw_record in raw_records:
            if not isinstance(raw_record, dict):
                continue
            records.append(
                TeamsMessageRecord(
                    source_id=_as_optional_str(raw_record.get("id") or raw_record.get("messageId")),
                    channel=_as_optional_str(raw_record.get("channel") or raw_record.get("teamChannel")),
                    sender=_as_optional_str(_sender_value(raw_record.get("from") or raw_record.get("sender"))),
                    sent_at=_as_optional_str(raw_record.get("createdDateTime") or raw_record.get("timestamp")),
                    web_url=_as_optional_str(raw_record.get("webUrl") or raw_record.get("url") or raw_record.get("link")),
                    preview=_as_optional_str(raw_record.get("bodyPreview") or raw_record.get("preview") or raw_record.get("snippet") or _body_content(raw_record.get("body"))),
                    thread_id=_as_optional_str(raw_record.get("threadId")),
                    conversation_id=_as_optional_str(raw_record.get("conversationId") or raw_record.get("meetingId")),
                    title=_as_optional_str(raw_record.get("title") or raw_record.get("subject")),
                )
            )
        if not records:
            records.extend(_records_from_prose(payload))

        next_cursor = _as_optional_str(payload.get("next_cursor") or payload.get("nextPageToken") or payload.get("cursor"))
        return TeamsMessagePage(records=tuple(records), next_cursor=next_cursor, source="workiq")


def _records_from_prose(payload: dict[str, Any]) -> list[TeamsMessageRecord]:
    """Synthesize Teams records from links embedded in a prose ask_work_iq answer.

    A Teams message/chat permalink carries a recoverable ``threadId``; it is stored on
    ``thread_id`` so the discovery matcher resolves a durable identifier, with the prose
    line retained as the channel/title context for scoring.
    """

    records: list[TeamsMessageRecord] = []
    for url, context in extract_source_url_contexts(prose_text_from_payload(payload)):
        records.append(
            TeamsMessageRecord(
                source_id=None,
                channel=_as_optional_str(context),
                sender=None,
                sent_at=None,
                web_url=url,
                preview=_as_optional_str(context),
                thread_id=url,
                title=_as_optional_str(context),
            )
        )
    return records


def _sender_value(value: Any) -> Any:
    if isinstance(value, dict):
        user = value.get("user")
        if isinstance(user, dict):
            return user.get("displayName") or user.get("id")
        return value.get("displayName") or value.get("name")
    return value


def _body_content(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("content") or value.get("plainTextContent")
    return value


def _as_optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)
