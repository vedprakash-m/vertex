"""
Context-diff command: query Plane 1 configuration changes.

Implements §22 E4 of the program-context-maturity spec.

Usage:
    vertex context-diff --edition <edition> --since-issue N
    vertex context-diff --edition <edition> --since YYYY-MM-DD
    vertex context-diff --edition <edition> --between-start N --between M

Reads:
    - programs/<prog>/changelog/plane1_changes.jsonl
    - programs/<prog>/archive/<edition>/context_snapshots/issue_NNN.context.json

Zone A only. No AI. No M365 calls.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import typer

from src.core.edition_resolver import PROGRAMS_ROOT, resolve_edition
from src.core.plane1_changelog import load_plane1_changes


app = typer.Typer(help="Query Plane 1 configuration changes between issues or dates.")


@app.command("context-diff")
def context_diff(
    edition: str = typer.Option(..., "--edition", help="Edition id (e.g. acme_weekly). Required."),
    since_issue: int | None = typer.Option(None, "--since-issue", help="Show changes since confirmed issue N was published."),
    since_date: str | None = typer.Option(None, "--since", help="Show changes since ISO date YYYY-MM-DD."),
    between_start: int | None = typer.Option(None, "--between-start", help="Start issue for --between range."),
    between_end: int | None = typer.Option(None, "--between", help="End issue for --between range (inclusive). Use with --between-start."),
    format: str = typer.Option("text", "--format", help="Output format: text (default) or json."),
    programs_root: Path | None = typer.Option(None, "--programs-root"),
) -> None:
    """Query Plane 1 configuration changes for a program."""

    resolved_programs_root = programs_root or PROGRAMS_ROOT

    resolved = resolve_edition(edition, programs_root=resolved_programs_root)
    if resolved is None:
        raise typer.BadParameter(f"Edition '{edition}' could not be resolved.")
    program_id = resolved.paths.program_id

    if between_start is not None and between_end is not None:
        from src.core.context_snapshot_store import load_context_snapshot
        start_snap = load_context_snapshot(program_id, edition, between_start, archive_root=resolved_programs_root)
        end_snap = load_context_snapshot(program_id, edition, between_end, archive_root=resolved_programs_root)
        if start_snap is None:
            raise typer.BadParameter(f"No context snapshot found for issue {between_start}.")
        if end_snap is None:
            raise typer.BadParameter(f"No context snapshot found for issue {between_end}.")
        since_dt: datetime | None = start_snap.confirmed_at
        until_dt: datetime | None = end_snap.confirmed_at
        header = (
            f"Context changes for {program_id} | "
            f"Between issue-{between_start} and issue-{between_end} "
            f"({_ds(start_snap.confirmed_at)} → {_ds(end_snap.confirmed_at)})"
        )
    elif since_issue is not None:
        from src.core.context_snapshot_store import load_context_snapshot
        from src.core.archive_store import find_latest_confirmed_entry, read_archive_index
        snap = load_context_snapshot(program_id, edition, since_issue, archive_root=resolved_programs_root)
        if snap is not None:
            since_dt = snap.confirmed_at
        else:
            entry = find_latest_confirmed_entry(
                read_archive_index(edition, resolved.paths.archive_dir),
                before_issue_number=since_issue,
            )
            if entry is None:
                raise typer.BadParameter(f"No context snapshot or archive entry found for issue {since_issue}.")
            since_dt = entry.generated_at
        header = f"Context changes for {program_id} | Since issue-{since_issue} ({_ds(since_dt)})"
    elif since_date is not None:
        try:
            since_dt = _ensure_tz(datetime.fromisoformat(since_date))
        except ValueError:
            raise typer.BadParameter(f"Invalid date format: {since_date}. Use YYYY-MM-DD.")
        until_dt = None
        header = f"Context changes for {program_id} | Since {since_date}"
    else:
        raise typer.BadParameter("Specify --since-issue, --since, or --between-start/--between.")

    changes = load_plane1_changes(program_id, programs_root=resolved_programs_root, since=since_dt)
    if until_dt is not None:
        changes = [c for c in changes if c.ts <= until_dt]

    if format == "json":
        import json
        typer.echo(json.dumps([c.to_json() for c in changes], indent=2, default=str))
        return

    if not changes:
        typer.echo(f"{header}\n\nNo Plane 1 configuration changes detected.")
        return

    typer.echo(header)
    typer.echo()

    by_type: dict[str, list] = {}
    for c in changes:
        by_type.setdefault(c.entity_type, []).append(c)

    for entity_type in ("milestone", "risk", "workstream", "decision", "assumption"):
        if entity_type not in by_type:
            continue
        typer.echo(entity_type.upper())
        typer.echo("-" * len(entity_type))
        for c in by_type[entity_type]:
            prior_s = c.prior or "(none)"
            current_s = c.current or "(none)"
            ts_s = c.ts.strftime("%Y-%m-%d")
            typer.echo(f"  {c.entity_id}  {c.entity_name!r}  {c.field}  {prior_s} → {current_s}  {ts_s}")
        typer.echo()

    typer.echo(f"Summary: {len(changes)} change{'s' if len(changes) != 1 else ''}.")
    detail = ", ".join(f"{len(v)} {k}" for k, v in sorted(by_type.items()))
    typer.echo(f"  By type: {detail}")


def _ds(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def _ensure_tz(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
