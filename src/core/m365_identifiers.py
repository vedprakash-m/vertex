from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse


def normalize_meeting_id(value: str | None) -> str | None:
    normalized = _normalize_input(value)
    if normalized is None:
        return None
    numeric_candidate = _compact_numeric_meeting_id(normalized)
    if numeric_candidate is not None:
        return numeric_candidate
    parsed = _try_parse_url(normalized)
    if parsed is None:
        return normalized

    query = parse_qs(parsed.query)
    for key in ("eventId", "meetingId"):
        candidate = _first_query_value(query, key)
        if candidate is not None:
            return _compact_numeric_meeting_id(candidate) or candidate
    path_candidate = _meeting_id_from_path(parsed)
    if path_candidate is not None:
        return path_candidate
    return normalized


def normalize_thread_id(value: str | None) -> str | None:
    normalized = _normalize_input(value)
    if normalized is None:
        return None
    parsed = _try_parse_url(normalized)
    if parsed is None:
        return normalized

    query = parse_qs(parsed.query)
    for key in ("threadId", "conversationId", "meetingId", "eventId"):
        candidate = _first_query_value(query, key)
        if candidate is not None:
            return candidate

    path_candidate = _thread_id_from_path(parsed)
    if path_candidate is not None:
        return path_candidate
    return normalized


def _normalize_input(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _try_parse_url(value: str):
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return parsed


def _first_query_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    return _normalize_input(unquote(values[0]))


def _compact_numeric_meeting_id(value: str) -> str | None:
    digits_only = value.replace(" ", "")
    if digits_only.isdigit():
        return digits_only
    return None


def _decoded_path_parts(parsed) -> list[str]:
    return [unquote(part) for part in parsed.path.split("/") if part]


def _meeting_id_from_path(parsed) -> str | None:
    path_parts = _decoded_path_parts(parsed)
    if len(path_parts) >= 2 and path_parts[0] == "meet":
        candidate = _normalize_input(path_parts[1])
        if candidate is not None:
            return _compact_numeric_meeting_id(candidate) or candidate
    if len(path_parts) >= 3 and path_parts[0] == "l" and path_parts[1] in {"meetup-join", "chat"}:
        candidate = _normalize_input(path_parts[2])
        if candidate is not None and candidate.startswith("19:meeting_"):
            return candidate
    return None


def normalize_site_library(site_library: str | None) -> str | None:
    """Normalize a SharePoint site+library path to a stable §13.5 registry key.

    Strips the scheme+host, URL-decodes path components, and lowercases the
    result so that variant spellings of the same site+library produce the same
    key (e.g. ``https://tenant.sharepoint.com/sites/CORP/Docs`` →
    ``/sites/corp/docs``). Returns ``None`` for empty / None input.
    """
    if not site_library:
        return None
    stripped = site_library.strip()
    if not stripped:
        return None
    parsed = _try_parse_url(stripped)
    if parsed is not None:
        path = parsed.path.rstrip("/")
    else:
        path = stripped.rstrip("/")
    path = unquote(path).lower()
    path = re.sub(r"/{2,}", "/", path)
    return path or None


def _thread_id_from_path(parsed) -> str | None:
    path_parts = _decoded_path_parts(parsed)
    if len(path_parts) >= 3 and path_parts[0] == "l" and path_parts[1] in {"message", "chat", "meetup-join"}:
        return _normalize_input(path_parts[2])
    return None