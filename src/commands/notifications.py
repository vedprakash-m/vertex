from __future__ import annotations

import json
from csv import DictWriter
from datetime import datetime, time, timezone
from io import StringIO
from pathlib import Path

import typer

from src.core.edition_resolver import PROGRAMS_ROOT, list_editions_for_program
from src.core.notification_state_store import ConfirmedNotificationEvent, load_confirmed_notification_events


REPO_ROOT = Path(__file__).resolve().parents[2]


def notifications_command(
    program: str = typer.Option(..., "--program", help="Program identifier."),
    since: str = typer.Option(..., "--since", help="Include entries at or after this ISO date."),
    format: str = typer.Option("text", "--format", help="Output format: text, json, or csv."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    normalized_program = _require_text(program, "--program")
    since_datetime = _parse_since(since)
    output_format = _normalize_format(format)
    edition_ids = list_editions_for_program(normalized_program, programs_root=programs_root)
    events = tuple(
        event
        for edition_id in edition_ids
        for event in load_confirmed_notification_events(
            edition=edition_id,
            programs_root=programs_root,
            since=since_datetime,
        )
    )
    sorted_events = tuple(sorted(events, key=lambda event: (event.confirmed_at, event.edition, event.issue_number)))

    if output_format == "json":
        payload = {
            "program_id": normalized_program,
            "since": since_datetime.isoformat(),
            "events": [_serialize_event(event) for event in sorted_events],
        }
        typer.echo(json.dumps(payload, indent=2))
        raise typer.Exit(code=0)

    if output_format == "csv":
        typer.echo(_render_csv(normalized_program, since_datetime, sorted_events), nl=False)
        raise typer.Exit(code=0)

    typer.echo(_render_text(normalized_program, since_datetime, sorted_events))
    raise typer.Exit(code=0)


def _require_text(value: str, option_name: str) -> str:
    if not value.strip():
        raise typer.BadParameter(f"{option_name} is required.")
    return value.strip()


def _parse_since(value: str) -> datetime:
    text = value.strip()
    if not text:
        raise typer.BadParameter("--since is required.")
    try:
        parsed_date = datetime.fromisoformat(text).date() if "T" in text else datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise typer.BadParameter("--since must be an ISO date or datetime.") from exc
    return datetime.combine(parsed_date, time(0, 0, 0), tzinfo=timezone.utc)


def _normalize_format(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in {"text", "json", "csv"}:
        raise typer.BadParameter("--format must be one of: text, json, csv.")
    return normalized


def _serialize_event(event: ConfirmedNotificationEvent) -> dict[str, object]:
    return {
        "edition": event.edition,
        "issue_number": event.issue_number,
        "confirmed_at": event.confirmed_at.isoformat(),
        "mode": event.mode,
        "notification_count": len(event.notifications),
        "notifications": [
            {
                "dri_email": notification.dri_email,
                "to": list(notification.to),
                "cc": list(notification.cc),
                "subject": notification.subject,
                "work_item_ids": list(notification.work_item_ids),
            }
            for notification in event.notifications
        ],
    }


def _render_text(program_id: str, since: datetime, events: tuple[ConfirmedNotificationEvent, ...]) -> str:
    header = f"Notification Log - {program_id}"
    lines = [header, "-" * len(header), f"Since: {since.date().isoformat()}"]
    if not events:
        lines.append("No notification events found.")
        return "\n".join(lines)
    for event in events:
        lines.append(
            f"{event.confirmed_at.isoformat()} | edition={event.edition} | issue={event.issue_number:03d} | mode={event.mode} | notifications={len(event.notifications)}"
        )
        for notification in event.notifications:
            work_item_ids = ",".join(str(work_item_id) for work_item_id in notification.work_item_ids) or "-"
            recipients = ", ".join(notification.to) if notification.to else notification.dri_email
            lines.append(
                f"  {notification.dri_email} | to={recipients} | wi={work_item_ids} | subject={notification.subject}"
            )
    return "\n".join(lines)


def _render_csv(program_id: str, since: datetime, events: tuple[ConfirmedNotificationEvent, ...]) -> str:
    buffer = StringIO()
    writer = DictWriter(
        buffer,
        fieldnames=(
            "program_id",
            "since",
            "edition",
            "issue_number",
            "confirmed_at",
            "mode",
            "dri_email",
            "to",
            "cc",
            "subject",
            "work_item_ids",
        ),
    )
    writer.writeheader()
    for event in events:
        if not event.notifications:
            writer.writerow(
                {
                    "program_id": program_id,
                    "since": since.isoformat(),
                    "edition": event.edition,
                    "issue_number": event.issue_number,
                    "confirmed_at": event.confirmed_at.isoformat(),
                    "mode": event.mode,
                    "dri_email": "",
                    "to": "",
                    "cc": "",
                    "subject": "",
                    "work_item_ids": "",
                }
            )
            continue
        for notification in event.notifications:
            writer.writerow(
                {
                    "program_id": program_id,
                    "since": since.isoformat(),
                    "edition": event.edition,
                    "issue_number": event.issue_number,
                    "confirmed_at": event.confirmed_at.isoformat(),
                    "mode": event.mode,
                    "dri_email": notification.dri_email,
                    "to": ";".join(notification.to),
                    "cc": ";".join(notification.cc),
                    "subject": notification.subject,
                    "work_item_ids": ";".join(str(work_item_id) for work_item_id in notification.work_item_ids),
                }
            )
    return buffer.getvalue()
