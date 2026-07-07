from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from src.core.exceptions import QueryError
from src.core.retry import retry_with_backoff
from src.m365.agency_bridge import AgencyBridge
from src.m365.workiq_ask_support import (
    build_transcript_by_name_question,
    build_transcript_question,
    coerce_workiq_json_payload,
)


@dataclass(frozen=True, slots=True)
class TranscriptRecord:
    meeting_id: str | None
    title: str | None
    captured_at: str | None
    web_url: str | None
    content: str


class TranscriptReader:
    """Thin meeting-transcript reader over Agency Bridge M365 access."""

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

    def get_transcript(self, *, meeting_id: str) -> TranscriptRecord | None:
        payload = None
        ask_workiq = getattr(self._bridge, "ask_workiq", None)
        if callable(ask_workiq):
            payload = self._retry(lambda: ask_workiq(build_transcript_question(meeting_id=meeting_id)))
            payload = coerce_workiq_json_payload(payload)
        if payload is None:
            payload = self._retry(
                lambda: self._bridge.invoke_mcp_tool("workiq", "get_transcript", {"meeting_id": meeting_id})
            )
        if payload is None:
            return None
        return _transcript_record_from_payload(payload)

    def get_transcript_by_name(
        self,
        *,
        calendar_name: str,
        since_days: int = 7,
    ) -> tuple[TranscriptRecord, ...]:
        """P4-20 (§9.6): name-based NL fallback for transcript fetch when ``series_id`` is null.

        Asks WorkIQ for the *verbatim* transcript text from every occurrence of the
        named meeting in the lookback window (the only empirically working transcript
        path in this environment). ``get_transcript(series_id)`` remains the primary
        path; this activates only when no durable ``series_id`` is configured.

        Degrades gracefully: returns ``()`` (never raises) on a missing/empty/malformed
        response, a missing ``ask_workiq`` callable, or a transport error.
        """
        name = (calendar_name or "").strip()
        if not name:
            return ()
        question = build_transcript_by_name_question(calendar_name=name, since_days=since_days)
        ask_workiq = getattr(self._bridge, "ask_workiq", None)
        if not callable(ask_workiq):
            return ()
        try:
            payload = self._retry(lambda: ask_workiq(question))
        except (ConnectionError, QueryError):
            return ()
        payload = coerce_workiq_json_payload(payload, root_key="transcripts")
        return _transcript_records_from_name_payload(payload)

    def _retry(self, callable_fn: Callable[[], dict[str, Any] | None]) -> dict[str, Any] | None:
        kwargs: dict[str, Any] = {
            "max_attempts": self._max_attempts,
            "retry_on": (ConnectionError, QueryError),
        }
        if self._sleep_func is not None:
            kwargs["sleep_func"] = self._sleep_func
        return retry_with_backoff(callable_fn, **kwargs)


def _transcript_record_from_payload(payload: dict[str, Any]) -> TranscriptRecord:
    """Build one TranscriptRecord from a WorkIQ transcript payload dict."""
    return TranscriptRecord(
        meeting_id=_as_optional_str(payload.get("meeting_id") or payload.get("meetingId") or payload.get("id")),
        title=_as_optional_str(payload.get("title") or payload.get("subject")),
        captured_at=_as_optional_str(payload.get("captured_at") or payload.get("createdDateTime") or payload.get("timestamp")),
        web_url=_as_optional_str(payload.get("webUrl") or payload.get("url") or payload.get("link")),
        content=_transcript_content(payload),
    )


def _transcript_records_from_name_payload(payload: dict[str, Any] | list[Any] | None) -> tuple[TranscriptRecord, ...]:
    """Decode the name-based query response into zero-or-more TranscriptRecords.

    Accepts a top-level list, a dict with a ``transcripts``/``items`` list, or a
    single transcript dict. Records with empty content are dropped (no transcript
    was available for that occurrence).
    """
    if payload is None:
        return ()
    if isinstance(payload, list):
        items: Any = payload
    elif isinstance(payload, dict):
        items = payload.get("transcripts") or payload.get("items") or payload.get("segments")
        if items is None:
            # A single transcript dict (one occurrence).
            record = _transcript_record_from_payload(payload)
            return (record,) if record.content else ()
    else:
        return ()
    if not isinstance(items, list):
        return ()
    records: list[TranscriptRecord] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        record = _transcript_record_from_payload(item)
        if record.content:
            records.append(record)
    return tuple(records)


def _transcript_content(payload: dict[str, Any]) -> str:
    text = payload.get("content") or payload.get("text") or payload.get("transcript")
    if isinstance(text, str):
        return text
    segments = payload.get("segments") or payload.get("items") or ()
    if not isinstance(segments, list):
        return ""
    parts: list[str] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        value = segment.get("text") or segment.get("content")
        if value not in (None, ""):
            parts.append(str(value))
    return "\n".join(parts)


def _as_optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)
