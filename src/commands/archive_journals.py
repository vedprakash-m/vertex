from __future__ import annotations

import csv
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path

import typer

from src.core.exceptions import ConfigError
from src.core.journal import PROGRAMS_ROOT, archive_weekly_journal_files, archive_weekly_journal_files_by_retention
from src.core.journal_retention import load_signal_retention_policy


def archive_journals_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    before: str | None = typer.Option(None, "--before", help="Archive weekly journal files before YYYY-Www."),
    retention: bool = typer.Option(False, "--retention", help="Archive weekly journal files that are fully past the configured retention policy."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    resolved_program = program.strip()
    if not resolved_program:
        raise typer.BadParameter("--program is required.")
    if not (PROGRAMS_ROOT / resolved_program).exists():
        raise typer.BadParameter(f"Program '{resolved_program}' does not exist at {PROGRAMS_ROOT / resolved_program}.")
    if retention and before is not None:
        raise typer.BadParameter("Choose either --before or --retention, not both.")
    if not retention and before is None:
        raise typer.BadParameter("Provide either --before or --retention.")

    if retention:
        try:
            retention_policy = load_signal_retention_policy(resolved_program, programs_root=PROGRAMS_ROOT)
        except ConfigError as error:
            raise typer.BadParameter(str(error)) from error
        moved_paths = archive_weekly_journal_files_by_retention(
            resolved_program,
            as_of=datetime.now(timezone.utc),
            retention_days_by_source=retention_policy.retention_days_by_source if retention_policy is not None else {},
            default_retention_days=retention_policy.default_retention_days if retention_policy is not None else 365,
            programs_root=PROGRAMS_ROOT,
        )
    else:
        assert before is not None  # guaranteed by guard: not retention and before is None raises earlier
        try:
            moved_paths = archive_weekly_journal_files(
                resolved_program,
                before_week=before.strip(),
                programs_root=PROGRAMS_ROOT,
            )
        except ValueError as error:
            raise typer.BadParameter(str(error)) from error

    if format == "human":
        if not moved_paths:
            if retention:
                typer.echo(f"No weekly journal files eligible under the retention policy for {resolved_program}.")
            else:
                typer.echo(f"No weekly journal files before {before} for {resolved_program}.")
            raise typer.Exit(code=0)

        if retention:
            typer.echo(f"Archived {len(moved_paths)} weekly journal file(s) for {resolved_program} using retention policy.")
        else:
            typer.echo(f"Archived {len(moved_paths)} weekly journal file(s) for {resolved_program}.")
        for path in moved_paths:
            typer.echo(f"- {path}")
    else:
        typer.echo(
            render_archive_journals_output(
                program_id=resolved_program,
                moved_paths=moved_paths,
                before_week=before.strip() if before is not None else None,
                retention=retention,
                format=format,
            ),
            nl=False,
        )
    raise typer.Exit(code=0)


def render_archive_journals_output(
    *,
    program_id: str,
    moved_paths: tuple[Path, ...] | list[Path],
    before_week: str | None,
    retention: bool,
    format: str,
) -> str:
    payload = {
        "program_id": program_id,
        "mode": "retention" if retention else "before_week",
        "before_week": before_week,
        "archived_count": len(moved_paths),
        "moved_paths": [str(path) for path in moved_paths],
    }
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow(("row_type", "program_id", "mode", "before_week", "archived_count", "path"))
        writer.writerow(("summary", payload["program_id"], payload["mode"], payload["before_week"] or "", payload["archived_count"], ""))
        for path in moved_paths:
            writer.writerow(("path", payload["program_id"], payload["mode"], payload["before_week"] or "", "", str(path)))
        return buffer.getvalue()
    raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")