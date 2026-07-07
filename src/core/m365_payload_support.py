from __future__ import annotations

from typing import Any

from src.core.m365_identifiers import normalize_thread_id


def optional_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def sender_alias(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if "@" in text:
        return text.split("@", 1)[0].strip().lower() or None
    return text.split()[0].strip().lower() or None


def workiq_payload_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for key in ("emails", "items", "results", "messages", "meetings", "threads"):
        value = payload.get(key)
        if isinstance(value, list):
            records.extend(item for item in value if isinstance(item, dict))
    return records


def workiq_subject(record: dict[str, Any]) -> str | None:
    return optional_string(record.get("subject") or record.get("title") or record.get("name"))


def workiq_preview(record: dict[str, Any]) -> str | None:
    return optional_string(
        record.get("preview")
        or record.get("snippet")
        or record.get("bodyPreview")
        or record.get("description")
        or record.get("summary")
    )


def workiq_sender(record: dict[str, Any]) -> str | None:
    value = record.get("sender") or record.get("from")
    if isinstance(value, dict):
        email_address = value.get("emailAddress")
        if isinstance(email_address, dict):
            return optional_string(email_address.get("address") or email_address.get("name"))
        return optional_string(value.get("address") or value.get("name") or value.get("displayName"))
    return optional_string(value)


def workiq_participant_aliases(record: dict[str, Any]) -> tuple[str, ...]:
    aliases: list[str] = []
    for raw_value in (
        workiq_sender(record),
        *_workiq_people_values(record.get("participants")),
        *_workiq_people_values(record.get("attendees")),
        *_workiq_people_values(record.get("members")),
        *_workiq_people_values(record.get("toRecipients")),
        *_workiq_people_values(record.get("ccRecipients")),
    ):
        alias = sender_alias(raw_value)
        if alias:
            aliases.append(alias)
    return tuple(dict.fromkeys(aliases))


def workiq_thread_id(record: dict[str, Any]) -> str | None:
    return normalize_thread_id(
        optional_string(
            record.get("threadId")
            or record.get("conversationId")
            or record.get("meetingId")
            or record.get("webUrl")
            or record.get("url")
            or record.get("link")
        )
    )


def _workiq_people_values(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        normalized = optional_string(value)
        return (normalized,) if normalized else ()
    if isinstance(value, dict):
        email_address = value.get("emailAddress")
        if isinstance(email_address, dict):
            normalized = optional_string(email_address.get("address") or email_address.get("name"))
            return (normalized,) if normalized else ()
        normalized = optional_string(value.get("address") or value.get("name") or value.get("displayName"))
        return (normalized,) if normalized else ()
    if isinstance(value, list):
        collected: list[str] = []
        for entry in value:
            collected.extend(_workiq_people_values(entry))
        return tuple(collected)
    return ()
