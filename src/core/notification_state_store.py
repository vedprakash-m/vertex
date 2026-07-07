from __future__ import annotations
from src.core.edition_resolver import get_program_output_dir

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from src.core.exceptions import StateError
from src.core.models import NotifiedWorkItemState, PriorNotificationState


@dataclass(frozen=True, slots=True)
class ConfirmedNotification:
    dri_email: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    work_item_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ConfirmedNotificationEvent:
    edition: str
    issue_number: int
    confirmed_at: datetime
    mode: str
    notifications: tuple[ConfirmedNotification, ...]


def append_confirmed_notify_run(
    *,
    edition: str,
    issue_number: int,
    confirmed_at: datetime,
    notifications: Iterable[ConfirmedNotification],
    programs_root: Path,
) -> Path:
    normalized_confirmed_at = _normalize_datetime(confirmed_at)
    path = _notifications_root(programs_root, edition) / f"{normalized_confirmed_at.date().isoformat()}.json"
    payload = _load_payload(path, edition=edition, date_value=normalized_confirmed_at.date().isoformat())
    events = payload.setdefault("events", [])
    if not isinstance(events, list):
        raise StateError(f"Invalid notification log format in {path}")

    events.append(
        {
            "issue_number": issue_number,
            "confirmed_at": normalized_confirmed_at.isoformat(),
            "mode": "preview_confirmed",
            "notifications": [
                {
                    "dri_email": notification.dri_email,
                    "to": list(notification.to),
                    "cc": list(notification.cc),
                    "subject": notification.subject,
                    "work_item_ids": list(notification.work_item_ids),
                }
                for notification in notifications
            ],
        }
    )
    events.sort(key=lambda event: str(event.get("confirmed_at", "")))
    _write_atomic_json(path, payload)
    return path


def load_latest_notification_state(
    *,
    edition: str,
    programs_root: Path,
) -> PriorNotificationState | None:
    notifications_root = _notifications_root(programs_root, edition)
    if not notifications_root.exists():
        return None

    latest_event: dict[str, Any] | None = None
    latest_confirmed_at: datetime | None = None
    for path in sorted(notifications_root.glob("*.json"), reverse=True):
        payload = _load_payload(path, edition=edition, date_value=None)
        events = payload.get("events", [])
        if not isinstance(events, list):
            raise StateError(f"Invalid notification log format in {path}")
        for event in events:
            if not isinstance(event, dict):
                raise StateError(f"Invalid notification event in {path}")
            confirmed_at = _parse_datetime(event.get("confirmed_at"))
            if confirmed_at is None:
                continue
            if latest_confirmed_at is None or confirmed_at > latest_confirmed_at:
                latest_confirmed_at = confirmed_at
                latest_event = event

    if latest_event is None or latest_confirmed_at is None:
        return None

    notifications = latest_event.get("notifications", [])
    if not isinstance(notifications, list):
        raise StateError("Notification event is missing a notifications list.")

    items: list[NotifiedWorkItemState] = []
    seen_keys: set[tuple[int, str]] = set()
    for notification in notifications:
        if not isinstance(notification, dict):
            raise StateError("Notification entry must be a mapping.")
        dri_email = _required_string(notification.get("dri_email"), field_name="dri_email").strip()
        if not dri_email:
            continue
        work_item_ids = _parse_int_tuple(notification.get("work_item_ids"), field_name="work_item_ids")
        for work_item_id in work_item_ids:
            key = (work_item_id, dri_email.lower())
            if key in seen_keys:
                continue
            seen_keys.add(key)
            items.append(
                NotifiedWorkItemState(
                    work_item_id=work_item_id,
                    dri_email=dri_email,
                    notified_at=latest_confirmed_at,
                )
            )

    if not items:
        return None
    return PriorNotificationState(notified_at=latest_confirmed_at, items=tuple(items))


def load_confirmed_notification_events(
    *,
    edition: str,
    programs_root: Path,
    since: datetime | None = None,
) -> tuple[ConfirmedNotificationEvent, ...]:
    notifications_root = _notifications_root(programs_root, edition)
    if not notifications_root.exists():
        return ()

    normalized_since = _normalize_datetime(since) if since is not None else None
    events: list[ConfirmedNotificationEvent] = []
    for path in sorted(notifications_root.glob("*.json")):
        payload = _load_payload(path, edition=edition, date_value=None)
        raw_events = payload.get("events", [])
        if not isinstance(raw_events, list):
            raise StateError(f"Invalid notification log format in {path}")
        for raw_event in raw_events:
            parsed_event = _parse_notification_event(raw_event, edition=edition)
            if parsed_event is None:
                continue
            if normalized_since is not None and parsed_event.confirmed_at < normalized_since:
                continue
            events.append(parsed_event)
    return tuple(sorted(events, key=lambda event: (event.confirmed_at, event.edition, event.issue_number)))


def _notifications_root(programs_root: Path, edition: str) -> Path:
    return get_program_output_dir(edition, programs_root=programs_root) / "notifications"


def _parse_notification_event(raw_event: Any, *, edition: str) -> ConfirmedNotificationEvent | None:
    if not isinstance(raw_event, dict):
        raise StateError("Notification event must be a mapping.")
    confirmed_at = _required_datetime(raw_event.get("confirmed_at"), field_name="confirmed_at")
    issue_number = _required_int(raw_event.get("issue_number"), field_name="issue_number")
    raw_notifications = raw_event.get("notifications", [])
    if not isinstance(raw_notifications, list):
        raise StateError("Notification event is missing a notifications list.")
    notifications: list[ConfirmedNotification] = []
    for raw_notification in raw_notifications:
        if not isinstance(raw_notification, dict):
            raise StateError("Notification entry must be a mapping.")
        dri_email = _required_string(raw_notification.get("dri_email"), field_name="dri_email").strip()
        subject = _required_string(raw_notification.get("subject"), field_name="subject").strip()
        if not dri_email or not subject:
            continue
        to = _parse_email_tuple(raw_notification.get("to"), field_name="to")
        cc = _parse_email_tuple(raw_notification.get("cc"), field_name="cc")
        work_item_ids = _parse_int_tuple(raw_notification.get("work_item_ids"), field_name="work_item_ids")
        notifications.append(
            ConfirmedNotification(
                dri_email=dri_email,
                to=to,
                cc=cc,
                subject=subject,
                work_item_ids=work_item_ids,
            )
        )
    return ConfirmedNotificationEvent(
        edition=edition,
        issue_number=issue_number,
        confirmed_at=confirmed_at,
        mode=_required_string(raw_event.get("mode"), field_name="mode").strip() or "preview_confirmed",
        notifications=tuple(notifications),
    )


def _load_payload(path: Path, *, edition: str, date_value: str | None) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": "1.0",
            "edition": edition,
            "date": date_value,
            "events": [],
        }

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise StateError(f"Invalid notification log JSON in {path}") from error

    if not isinstance(payload, dict):
        raise StateError(f"Invalid notification log payload in {path}")
    schema_version = payload.get("schema_version")
    if schema_version is not None and not isinstance(schema_version, str):
        raise StateError(f"Notification log {path} has invalid schema_version")
    payload_edition = payload.get("edition")
    if payload_edition is not None and not isinstance(payload_edition, str):
        raise StateError(f"Notification log {path} has invalid edition")
    if payload_edition not in (None, edition):
        raise StateError(f"Notification log {path} belongs to {payload_edition}, not {edition}")
    payload_date = payload.get("date")
    if payload_date is not None and not isinstance(payload_date, str):
        raise StateError(f"Notification log {path} has invalid date")
    if date_value is not None and payload_date not in (None, date_value):
        raise StateError(f"Notification log {path} has unexpected date {payload_date}")
    if "events" not in payload:
        raise StateError(f"Notification log {path} is missing events")
    payload_events = payload.get("events")
    if not isinstance(payload_events, list):
        raise StateError(f"Invalid notification log format in {path}")

    payload.setdefault("schema_version", "1.0")
    payload.setdefault("edition", edition)
    payload.setdefault("date", date_value)
    return payload


def _write_atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _normalize_datetime(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _required_datetime(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise StateError(f"{field_name} must be a string")
    try:
        return _normalize_datetime(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as error:
        raise StateError(f"invalid {field_name}") from error


def _parse_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _required_int(value: Any, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise StateError(f"{field_name} must be an integer")
    return value


def _required_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise StateError(f"{field_name} must be a string")
    return value


def _parse_email_tuple(value: Any, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise StateError(f"{field_name} must be a list of strings")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise StateError(f"{field_name} must contain strings only")
        if item.strip():
            items.append(item.strip())
    return tuple(items)


def _parse_int_tuple(value: Any, *, field_name: str) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise StateError(f"{field_name} must be a list of integers")
    parsed: list[int] = []
    for item in value:
        parsed.append(_required_int(item, field_name=field_name))
    return tuple(parsed)