"""Presentation/formatting leaf helpers for the integration command (D-13).

Pure rendering utilities extracted from the ``integration.py`` god module
(§28.4 strangler fig): title redaction, table printing, delta-item echoing, and
optional-datetime formatting. No registry/state logic. ``integration.py``
re-imports these so the ``integration.<name>`` attribute surface and all call
sites are unchanged.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

import typer


def _display_title(title: str | None, *, reveal: bool) -> str:
    if not title:
        return ""
    if reveal:
        return title
    digest = hashlib.sha256(title.encode("utf-8")).hexdigest()[:8]
    return f"[hidden:{digest}]"


def _emit_delta_items(label: str, registrations: tuple[Any, ...]) -> None:
    if not registrations:
        return
    typer.echo(f"  {label}:")
    for registration in registrations:
        workstreams = ",".join(getattr(registration, "workstream_ids", ()) or ()) or "unassigned"
        title = getattr(registration, "ref_title", None) or ""
        typer.echo(
            f"    {registration.ref_kind}:{registration.ref_id} workstreams={workstreams} status={registration.status.value} {title}".rstrip()
        )


def _format_optional_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _print_table(headers: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    typer.echo("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    typer.echo("  ".join("-" * width for width in widths))
    for row in rows:
        typer.echo("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)))
