from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.exceptions import StateError
from src.core.notification_state_store import ConfirmedNotification, append_confirmed_notify_run, load_confirmed_notification_events, load_latest_notification_state


def test_load_confirmed_notification_events_round_trips_appended_run(tmp_path: Path) -> None:
    append_confirmed_notify_run(
        edition="acme_weekly",
        issue_number=7,
        confirmed_at=datetime(2026, 5, 7, 9, 30, tzinfo=timezone.utc),
        notifications=(
            ConfirmedNotification(
                dri_email="isaiah@example.com",
                to=("isaiah@example.com",),
                cc=("lead@example.com",),
                subject="Escalate missing rollout evidence",
                work_item_ids=(901010, 901011),
            ),
        ),
        programs_root=(tmp_path / "programs"),
    )

    events = load_confirmed_notification_events(edition="acme_weekly", programs_root=(tmp_path / "programs"))
    latest_state = load_latest_notification_state(edition="acme_weekly", programs_root=(tmp_path / "programs"))

    assert len(events) == 1
    assert events[0].issue_number == 7
    assert events[0].notifications[0].work_item_ids == (901010, 901011)
    assert latest_state is not None
    assert {item.work_item_id for item in latest_state.items} == {901010, 901011}


def test_load_confirmed_notification_events_rejects_numeric_string_issue_number(tmp_path: Path) -> None:
    notifications_root = (tmp_path / "programs") / "acme_weekly" / "publications" / "acme_weekly" / "notifications"
    notifications_root.mkdir(parents=True, exist_ok=True)
    (notifications_root / "2026-05-07.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "edition": "acme_weekly",
                "date": "2026-05-07",
                "events": [
                    {
                        "issue_number": "7",
                        "confirmed_at": "2026-05-07T09:30:00+00:00",
                        "mode": "preview_confirmed",
                        "notifications": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(StateError, match="issue_number must be an integer"):
        load_confirmed_notification_events(edition="acme_weekly", programs_root=(tmp_path / "programs"))


def test_load_latest_notification_state_rejects_non_object_event_row(tmp_path: Path) -> None:
    notifications_root = (tmp_path / "programs") / "acme_weekly" / "publications" / "acme_weekly" / "notifications"
    notifications_root.mkdir(parents=True, exist_ok=True)
    (notifications_root / "2026-05-07.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "edition": "acme_weekly",
                "date": "2026-05-07",
                "events": [["not", "an", "object"]],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(StateError, match="Invalid notification event"):
        load_latest_notification_state(edition="acme_weekly", programs_root=(tmp_path / "programs"))


def test_load_confirmed_notification_events_rejects_non_string_dri_email(tmp_path: Path) -> None:
    notifications_root = (tmp_path / "programs") / "acme_weekly" / "publications" / "acme_weekly" / "notifications"
    notifications_root.mkdir(parents=True, exist_ok=True)
    (notifications_root / "2026-05-07.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "edition": "acme_weekly",
                "date": "2026-05-07",
                "events": [
                    {
                        "issue_number": 7,
                        "confirmed_at": "2026-05-07T09:30:00+00:00",
                        "mode": "preview_confirmed",
                        "notifications": [
                            {
                                "dri_email": 1,
                                "to": ["isaiah@example.com"],
                                "cc": [],
                                "subject": "Escalate missing rollout evidence",
                                "work_item_ids": [901010],
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(StateError, match="dri_email must be a string"):
        load_confirmed_notification_events(edition="acme_weekly", programs_root=(tmp_path / "programs"))


def test_load_latest_notification_state_rejects_numeric_string_work_item_id(tmp_path: Path) -> None:
    notifications_root = (tmp_path / "programs") / "acme_weekly" / "publications" / "acme_weekly" / "notifications"
    notifications_root.mkdir(parents=True, exist_ok=True)
    (notifications_root / "2026-05-07.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "edition": "acme_weekly",
                "date": "2026-05-07",
                "events": [
                    {
                        "issue_number": 7,
                        "confirmed_at": "2026-05-07T09:30:00+00:00",
                        "mode": "preview_confirmed",
                        "notifications": [
                            {
                                "dri_email": "isaiah@example.com",
                                "to": ["isaiah@example.com"],
                                "cc": [],
                                "subject": "Escalate missing rollout evidence",
                                "work_item_ids": ["901010"],
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(StateError, match="work_item_ids must be an integer"):
        load_latest_notification_state(edition="acme_weekly", programs_root=(tmp_path / "programs"))


def test_load_confirmed_notification_events_rejects_non_string_payload_edition(tmp_path: Path) -> None:
    notifications_root = (tmp_path / "programs") / "acme_weekly" / "publications" / "acme_weekly" / "notifications"
    notifications_root.mkdir(parents=True, exist_ok=True)
    (notifications_root / "2026-05-07.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "edition": 1,
                "date": "2026-05-07",
                "events": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(StateError, match="invalid edition"):
        load_confirmed_notification_events(edition="acme_weekly", programs_root=(tmp_path / "programs"))


def test_load_confirmed_notification_events_rejects_non_string_payload_date(tmp_path: Path) -> None:
    notifications_root = (tmp_path / "programs") / "acme_weekly" / "publications" / "acme_weekly" / "notifications"
    notifications_root.mkdir(parents=True, exist_ok=True)
    (notifications_root / "2026-05-07.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "edition": "acme_weekly",
                "date": 20260507,
                "events": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(StateError, match="invalid date"):
        load_confirmed_notification_events(edition="acme_weekly", programs_root=(tmp_path / "programs"))


def test_load_confirmed_notification_events_rejects_non_string_confirmed_at(tmp_path: Path) -> None:
    notifications_root = (tmp_path / "programs") / "acme_weekly" / "publications" / "acme_weekly" / "notifications"
    notifications_root.mkdir(parents=True, exist_ok=True)
    (notifications_root / "2026-05-07.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "edition": "acme_weekly",
                "date": "2026-05-07",
                "events": [
                    {
                        "issue_number": 7,
                        "confirmed_at": 1,
                        "mode": "preview_confirmed",
                        "notifications": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(StateError, match="confirmed_at must be a string"):
        load_confirmed_notification_events(edition="acme_weekly", programs_root=(tmp_path / "programs"))


def test_load_confirmed_notification_events_rejects_non_string_mode(tmp_path: Path) -> None:
    notifications_root = (tmp_path / "programs") / "acme_weekly" / "publications" / "acme_weekly" / "notifications"
    notifications_root.mkdir(parents=True, exist_ok=True)
    (notifications_root / "2026-05-07.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "edition": "acme_weekly",
                "date": "2026-05-07",
                "events": [
                    {
                        "issue_number": 7,
                        "confirmed_at": "2026-05-07T09:30:00+00:00",
                        "mode": 1,
                        "notifications": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(StateError, match="mode must be a string"):
        load_confirmed_notification_events(edition="acme_weekly", programs_root=(tmp_path / "programs"))


def test_load_confirmed_notification_events_rejects_non_list_payload_events(tmp_path: Path) -> None:
    notifications_root = (tmp_path / "programs") / "acme_weekly" / "publications" / "acme_weekly" / "notifications"
    notifications_root.mkdir(parents=True, exist_ok=True)
    (notifications_root / "2026-05-07.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "edition": "acme_weekly",
                "date": "2026-05-07",
                "events": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(StateError, match=r"Invalid notification log format in .*"):
        load_confirmed_notification_events(edition="acme_weekly", programs_root=(tmp_path / "programs"))


def test_load_confirmed_notification_events_rejects_missing_payload_events(tmp_path: Path) -> None:
    notifications_root = (tmp_path / "programs") / "acme_weekly" / "publications" / "acme_weekly" / "notifications"
    notifications_root.mkdir(parents=True, exist_ok=True)
    (notifications_root / "2026-05-07.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "edition": "acme_weekly",
                "date": "2026-05-07",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(StateError, match=r"Notification log .* is missing events"):
        load_confirmed_notification_events(edition="acme_weekly", programs_root=(tmp_path / "programs"))

