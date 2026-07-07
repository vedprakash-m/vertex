from __future__ import annotations

import json
import re
import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any
from urllib.parse import urlparse

from src.core.m365_identifiers import normalize_thread_id


_JSON_CODE_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
_TERMINAL_ESCAPE_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_TRANSIENT_WORKIQ_ID_RE = re.compile(r"^turn\d+(?:search|result)\d+$", re.IGNORECASE)
_DEFAULT_MAIL_WEB_HOSTS = frozenset({"outlook.office.com", "outlook.office365.com", "outlook.live.com"})


@dataclass(frozen=True, slots=True)
class DiscoveryRequest:
    """Typed, deterministic input for FQ-01 mailbox enumeration."""

    lane_name: str
    terms: tuple[str, ...]
    window_start: date
    window_end: date
    limit: int = 8


def build_structured_discovery_question(request: DiscoveryRequest) -> str:
    """Render the GA-S1 Probe-1 shape without allowing prompt control bytes."""

    if request.window_start > request.window_end:
        raise ValueError("WorkIQ discovery window_start must not be after window_end")
    if not 1 <= request.limit <= 50:
        raise ValueError("WorkIQ discovery limit must be from 1 through 50")
    lane_name = _clean_prompt_value(request.lane_name, field_name="lane_name")
    terms = tuple(_clean_prompt_value(term, field_name="terms") for term in request.terms if term.strip())
    if not terms:
        raise ValueError("WorkIQ discovery terms must not be empty")
    lane_terms = "; ".join(terms)
    return (
        "Use my Microsoft 365 mailbox to answer. "
        f"Which of my emails received between {request.window_start.isoformat()} and {request.window_end.isoformat()} "
        f"are about, or closely related to, '{lane_terms}' for workstream '{lane_name}'? "
        "Return JSON only, no markdown, using this schema: "
        '{"emails":[{"id":"","conversationId":"","threadId":"","subject":"","from":"",'
        '"receivedDateTime":"","webUrl":"","bodyPreview":""}]}. '
        f"Return up to {request.limit} results. "
        'If there are no related emails, return {"emails":[]}.'
    )


def validate_structured_discovery_payload(
    payload: dict[str, Any] | None,
    *,
    window_start: date,
    window_end: date,
    limit: int,
    allowed_web_hosts: frozenset[str] = _DEFAULT_MAIL_WEB_HOSTS,
) -> dict[str, Any]:
    """Fail closed per record and return only bounded, safe FQ-01 emails.

    WorkIQ is an NL surface, so malformed model output is expected. Invalid records
    are discarded rather than repaired into synthetic identities or timestamps.

    Placement note (vertex-tech-spec §13.1.1): this validator lives in Zone C
    alongside the JSON toolkit it shares helpers with (coerce_workiq_json_payload,
    normalize_thread_id from src.core.m365_identifiers). Zone C -> Zone A is
    permitted, so this placement is contract-safe (locked by
    tests/contracts/test_import_boundaries.py::test_workiq_structured_discovery_lives_in_zone_c).

    If source-keyed rich-evidence aggregation is added later, consider moving
    this per-record validation to src/core/workiq_discovery_validation.py so pure
    validation and aggregation sit in one Zone-A module. The
    boundary test above should move with it.
    """

    if not 1 <= limit <= 50:
        raise ValueError("WorkIQ discovery limit must be from 1 through 50")
    parsed = coerce_workiq_json_payload(payload, root_key="emails")
    raw_records = parsed.get("emails") if isinstance(parsed, dict) else None
    if not isinstance(raw_records, list):
        return {"emails": []}
    start_at = datetime.combine(window_start, time.min, tzinfo=timezone.utc)
    end_at = datetime.combine(window_end, time.max, tzinfo=timezone.utc)
    valid: list[dict[str, Any]] = []
    seen_identities: set[str] = set()
    for raw in raw_records:
        if not isinstance(raw, dict) or _contains_terminal_escape(raw):
            continue
        received_at = _parse_iso_datetime(raw.get("receivedDateTime"))
        if received_at is None or not start_at <= received_at <= end_at:
            continue
        durable_identity = normalize_thread_id(_nonempty_string(raw.get("conversationId") or raw.get("threadId") or raw.get("id")))
        if durable_identity is not None and _TRANSIENT_WORKIQ_ID_RE.fullmatch(durable_identity):
            durable_identity = None
        message_identity = normalize_thread_id(_nonempty_string(raw.get("id")))
        if message_identity is not None and _TRANSIENT_WORKIQ_ID_RE.fullmatch(message_identity):
            message_identity = None
        identity = durable_identity or _semantic_email_identity(raw, received_at=received_at)
        if identity is None or identity in seen_identities:
            continue
        web_url = _nonempty_string(raw.get("webUrl"))
        if web_url is not None and not _is_allowed_web_url(web_url, allowed_hosts=allowed_web_hosts):
            continue
        record = dict(raw)
        record["id"] = message_identity or identity
        if durable_identity is not None:
            record["conversationId"] = durable_identity
        elif not _nonempty_string(record.get("conversationId")):
            record.pop("conversationId", None)
            record.pop("threadId", None)
        record["receivedDateTime"] = received_at.isoformat().replace("+00:00", "Z")
        if web_url is not None:
            record["webUrl"] = web_url
        valid.append(record)
        seen_identities.add(identity)
        if len(valid) >= limit:
            break
    return {"emails": valid}


def _clean_prompt_value(value: str, *, field_name: str) -> str:
    normalized = " ".join(value.split())
    if not normalized or _TERMINAL_ESCAPE_RE.search(value):
        raise ValueError(f"WorkIQ discovery {field_name} contains an empty or unsafe value")
    # Keep quoted values from terminating the prompt's single-quoted boundary.
    return normalized.replace("'", "’")


def _nonempty_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _contains_terminal_escape(value: Any) -> bool:
    if isinstance(value, str):
        return _TERMINAL_ESCAPE_RE.search(value) is not None
    if isinstance(value, dict):
        return any(_contains_terminal_escape(key) or _contains_terminal_escape(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_terminal_escape(item) for item in value)
    return False


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _semantic_email_identity(record: dict[str, Any], *, received_at: datetime) -> str | None:
    subject = _nonempty_string(record.get("subject"))
    sender = record.get("from") or record.get("sender")
    sender_text: str | None
    if isinstance(sender, dict):
        sender_text = json.dumps(sender, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    else:
        sender_text = _nonempty_string(sender)
    if subject is None or not sender_text:
        return None
    material = "|".join((subject.casefold(), sender_text.casefold(), received_at.isoformat()))
    return f"semantic:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _is_allowed_web_url(value: str, *, allowed_hosts: frozenset[str]) -> bool:
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return False
    return parsed.scheme == "https" and port in (None, 443) and (parsed.hostname or "").lower() in allowed_hosts


# `ask_work_iq` is an M365 Copilot retrieval/summarization agent, not a raw Graph
# metadata enumerator: a literal "search calendar matching X, return JSON" prompt
# reliably comes back empty even when the source exists, whereas a content-relational
# question ("which of my meetings relate to X?") is answered well — the same shape the
# newsletter signal path uses. The builders below therefore lead with a content
# question, explicitly ask for any durable link/identifier, and keep the JSON schema as
# the preferred return format while tolerating a prose answer (harvested downstream via
# `extract_source_url_contexts`). See ops-ready.md S1 root-cause notes.
def build_mail_search_question(*, query: str, limit: int, cursor: str | None = None) -> str:
    cursor_hint = f' Continue from cursor "{cursor}" if supported.' if cursor else ""
    return (
        "Use my Microsoft 365 mailbox to answer. "
        f"Which of my email messages or conversation threads are about, or closely related to, {query!r}? "
        "For every relevant message or thread, give its subject, the sender, and — most important — any durable link or "
        "identifier you can surface: the message web link, the conversation/thread id, or the Outlook permalink. "
        "Prefer returning JSON only, with no markdown or commentary, using this schema: "
        '{"emails":[{"id":"","messageId":"","emailId":"","subject":"","title":"","from":{"emailAddress":{"address":"","name":""}},"sender":"","toRecipients":[{"emailAddress":{"address":"","name":""}}],"receivedDateTime":"","sentDateTime":"","webUrl":"","url":"","link":"","bodyPreview":"","preview":"","snippet":"","threadId":"","conversationId":""}],"next_cursor":null}. '
        f"Return up to {limit} results.{cursor_hint} "
        "If you cannot produce JSON, answer in plain text and list each subject together with its web link on the same line. "
        'If there are no related messages, return {"emails":[],"next_cursor":null}.'
    )


def build_calendar_search_question(*, query: str, limit: int, cursor: str | None = None) -> str:
    cursor_hint = f' Continue from cursor "{cursor}" if supported.' if cursor else ""
    return (
        "Use my Microsoft 365 calendar and meetings to answer. "
        f"Which meetings or recurring meeting series are about, or closely related to, {query!r}? "
        "Include recurring series and recent or upcoming occurrences. "
        "For every meeting you find, give its exact title, the organizer, and — most important — any durable link or "
        "identifier you can surface: the Teams join link, the calendar event web link, the online meeting id, or the "
        "recurring series id. "
        "Prefer returning JSON only, with no markdown or commentary, using this schema: "
        '{"events":[{"id":"","eventId":"","subject":"","title":"","organizer":{"emailAddress":{"address":"","name":""}},"start":{"dateTime":""},"end":{"dateTime":""},"location":{"displayName":""},"webUrl":"","url":"","link":"","joinWebUrl":"","meetingId":"","seriesMasterId":"","isRecurring":false,"recurrence":null,"type":"","attendees":[{"emailAddress":{"address":"","name":""}}],"onlineMeeting":{"id":"","joinUrl":""}}],"next_cursor":null}. '
        f"Return up to {limit} results.{cursor_hint} "
        "If you cannot produce JSON, answer in plain text and list each meeting title together with its Teams/calendar link on the same line. "
        'If there are no related meetings, return {"events":[],"next_cursor":null}.'
    )


def build_teams_search_question(
    *,
    channel: str,
    query: str,
    since: str | None,
    limit: int,
    cursor: str | None = None,
) -> str:
    since_hint = f" Only include messages on or after {since}." if since else ""
    channel_hint = "any channel or chat" if channel.strip().lower() == "all" else f"the channel {channel!r}"
    cursor_hint = f' Continue from cursor "{cursor}" if supported.' if cursor else ""
    return (
        f"Use my Microsoft Teams messages in {channel_hint} to answer. "
        f"Which conversations, channels, or chats are about, or closely related to, {query!r}? "
        "For every relevant message or thread, give the channel or chat name, the sender, and — most important — any durable "
        "link or identifier you can surface: the Teams message web link, the thread/conversation id, or the meeting id. "
        "Prefer returning JSON only, with no markdown or commentary, using this schema: "
        '{"messages":[{"id":"","messageId":"","channel":"","teamChannel":"","from":{"user":{"displayName":"","id":""}},"sender":"","createdDateTime":"","timestamp":"","webUrl":"","url":"","link":"","body":{"content":"","plainTextContent":""},"bodyPreview":"","preview":"","snippet":"","threadId":"","conversationId":"","meetingId":"","title":"","subject":""}],"next_cursor":null}. '
        f"Return up to {limit} results.{since_hint}{cursor_hint} "
        "If you cannot produce JSON, answer in plain text and list each channel or chat name together with its Teams link on the same line. "
        'If there are no related conversations, return {"messages":[],"next_cursor":null}.'
    )


def build_transcript_question(*, meeting_id: str) -> str:
    return (
        "Retrieve the Microsoft Teams meeting transcript for the specified meeting id. "
        "Return JSON only, with no markdown or commentary, using this schema: "
        '{"meetingId":"","meeting_id":"","id":"","title":"","subject":"","createdDateTime":"","timestamp":"","captured_at":"","webUrl":"","url":"","link":"","content":"","text":"","transcript":"","segments":[{"text":"","content":""}]}. '
        f"Meeting id: {meeting_id!r}. "
        "If no transcript is available, return {}."
    )


def build_transcript_by_name_question(*, calendar_name: str, since_days: int) -> str:
    """P4-20 (§9.6): name-based NL transcript query — the only empirically working
    transcript path when ``series_id`` is null. Demands *verbatim* transcript text
    (not summarization) from every occurrence of the named meeting in the lookback
    window, preserving extraction fidelity for ``ContentExtractionAgent``.
    """
    return (
        "Use my Microsoft 365 calendar, meetings, and Teams meeting transcripts to answer. "
        f"Find every occurrence in the past {since_days} days of the Teams meeting titled {calendar_name!r} "
        "and extract the raw verbatim transcript text for each occurrence — quote directly, do not summarize "
        "or paraphrase. "
        "Return JSON only, with no markdown or commentary, using this schema: "
        '{"transcripts":[{"meetingId":"","meeting_id":"","id":"","title":"","subject":"","createdDateTime":"","timestamp":"","captured_at":"","webUrl":"","url":"","link":"","content":"","text":"","transcript":"","segments":[{"text":"","content":""}]}]}. '
        "Return one transcript entry per occurrence. "
        'If no transcript is available for any occurrence, return {"transcripts":[]}.'
    )


def coerce_workiq_json_payload(payload: dict[str, Any] | None, *, root_key: str | None = None) -> dict[str, Any] | None:
    if payload is None:
        return None
    if root_key is not None and root_key in payload:
        return payload
    if root_key is None and not any(key in payload for key in ("response", "summary", "content", "text")):
        return payload
    parsed = extract_json_value_from_payload(payload)
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list) and root_key:
        return {root_key: parsed}
    return payload


def extract_json_value_from_payload(payload: dict[str, Any] | None) -> Any | None:
    if payload is None:
        return None
    for key in ("response", "summary", "content", "text"):
        value = payload.get(key)
        if not isinstance(value, str):
            continue
        parsed = extract_json_value_from_text(value)
        if parsed is not None:
            return parsed
    return None


def extract_json_value_from_text(text: str) -> Any | None:
    stripped = text.strip()
    if not stripped:
        return None
    for candidate in _json_text_candidates(stripped):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    decoder = json.JSONDecoder()
    for index, char in enumerate(stripped):
        if char not in "[{":
            continue
        try:
            value, _end = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        return value
    # WorkIQ CLI hard-wraps redirected stdout, including inside JSON string
    # values. Removing presentation newlines is safe only after every ordinary
    # JSON parse has failed; terminal controls are rejected by the caller.
    collapsed = stripped.replace("\r", "").replace("\n", "")
    if collapsed != stripped:
        for candidate in _json_text_candidates(collapsed):
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    return None


def _json_text_candidates(text: str) -> tuple[str, ...]:
    candidates: list[str] = [text]
    for match in _JSON_CODE_BLOCK_RE.finditer(text):
        block = match.group(1).strip()
        if block and block not in candidates:
            candidates.append(block)
    return tuple(candidates)


# Durable M365 identifiers (`seriesMasterId`, Teams `threadId`, meeting join links) most
# often arrive embedded in a prose `ask_work_iq` answer rather than as a structured JSON
# field. These helpers recover them so discovery can harvest IDs from a content answer.
_URL_RE = re.compile(r"https?://[^\s\)\]\}>\"'`]+", re.IGNORECASE)
# Trailing punctuation that markdown/prose commonly appends to an inline URL.
_URL_TRAILING_PUNCT = ".,;:!?"


def prose_text_from_payload(payload: dict[str, Any] | None) -> str | None:
    """Return the human-readable text body of a WorkIQ payload, if any."""

    if payload is None:
        return None
    for key in ("response", "summary", "content", "text", "answer"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _trim_url(raw: str) -> str:
    url = raw.strip()
    while url and url[-1] in _URL_TRAILING_PUNCT:
        url = url[:-1]
    # Markdown links render as `[label](url)`; the closing paren is stripped by the
    # character class above only when unbalanced, so guard the common balanced case.
    if url.count("(") < url.count(")") and url.endswith(")"):
        url = url[:-1]
    return url


def extract_source_urls_from_text(text: str | None) -> tuple[str, ...]:
    """Extract de-duplicated http(s) URLs embedded in a prose answer."""

    if not text:
        return ()
    seen: list[str] = []
    for match in _URL_RE.finditer(text):
        url = _trim_url(match.group(0))
        if url and url not in seen:
            seen.append(url)
    return tuple(seen)


def extract_source_url_contexts(text: str | None) -> tuple[tuple[str, str], ...]:
    """Pair each embedded URL with the line/sentence of prose that surrounds it.

    The surrounding text is used downstream as a match label (title/topic scoring) so a
    prose-harvested candidate can be ranked against the source's display name and topics.
    """

    if not text:
        return ()
    contexts: dict[str, str] = {}
    for line in re.split(r"[\r\n]+", text):
        line_urls = extract_source_urls_from_text(line)
        if not line_urls:
            continue
        context = _URL_RE.sub(" ", line).strip(" \t-*•|:")
        for url in line_urls:
            # Keep the first (richest) context seen for a given URL.
            contexts.setdefault(url, context)
    # URLs that only appeared without a clean line context still surface (empty context).
    for url in extract_source_urls_from_text(text):
        contexts.setdefault(url, "")
    return tuple(contexts.items())
