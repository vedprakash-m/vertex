from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import typer

from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.exceptions import StateError
from src.core.gather_state_store import load_gather_query_states, resolve_gather_state_path_for_read
from src.core.kusto_query_loader import load_kpi_queries


app = typer.Typer(help="Inspect runtime state for deterministic command surfaces.")


@app.command("kusto")
def inspect_kusto_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    query: str | None = typer.Option(None, "--query", help="Optional Kusto query id to inspect."),
    format: str = typer.Option("table", "--format", help="Output format: table or json."),
    since: str | None = typer.Option(None, "--since", help="Optional success recency window, for example 7d."),
) -> None:
    normalized_format = format.strip().lower()
    if normalized_format not in {"table", "json"}:
        raise typer.BadParameter("--format must be 'table' or 'json'.")

    try:
        rows = inspect_kusto_state(program, query_id=query, since=since)
    except FileNotFoundError as error:
        typer.echo(str(error))
        raise typer.Exit(code=3) from error
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error

    if not rows:
        typer.echo(f"No matching wired Kusto queries found for program '{program}'.")
        raise typer.Exit(code=2)

    if normalized_format == "json":
        typer.echo(json.dumps({"program_id": program, "queries": rows}, indent=2, sort_keys=True))
        raise typer.Exit(code=0)

    typer.echo(_render_table(rows))
    raise typer.Exit(code=0)


def inspect_kusto_state(
    program_id: str,
    *,
    query_id: str | None = None,
    since: str | None = None,
    programs_root: Path | None = None,
) -> list[dict[str, Any]]:
    resolved_programs_root = programs_root or PROGRAMS_ROOT
    state_path = resolve_gather_state_path_for_read(program_id, programs_root=resolved_programs_root)
    if not state_path.exists():
        raise FileNotFoundError(f"Gather state file is missing for program '{program_id}'.")

    try:
        query_states = load_gather_query_states(program_id, programs_root=resolved_programs_root)
    except StateError as error:
        raise FileNotFoundError(f"Gather state file is unreadable for program '{program_id}'.") from error
    cutoff = _parse_since_cutoff(since)

    wired_queries = tuple(
        query for query in load_kpi_queries(program_id, programs_root=resolved_programs_root) if query.refresh_on_gather
    )
    if query_id is not None:
        wired_queries = tuple(query for query in wired_queries if query.id == query_id)

    rows: list[dict[str, Any]] = []
    for wired_query in wired_queries:
        state = query_states.get(wired_query.id)
        last_succeeded_at = _optional_text(state, "last_succeeded_at")
        if cutoff is not None:
            parsed_last_succeeded = _parse_datetime(last_succeeded_at)
            if parsed_last_succeeded is None or parsed_last_succeeded < cutoff:
                continue
        rows.append(
            {
                "query_id": wired_query.id,
                "validated": wired_query.validated,
                "last_cycle_succeeded": state.get("last_cycle_succeeded") if isinstance(state, dict) else None,
                "last_succeeded_at": last_succeeded_at,
                "row_count": state.get("row_count") if isinstance(state, dict) else None,
                "duration_ms": state.get("duration_ms") if isinstance(state, dict) else None,
                "last_error": _optional_text(state, "last_error"),
            }
        )
    return rows


def _parse_since_cutoff(value: str | None) -> datetime | None:
    if value is None:
        return None
    text = value.strip().lower()
    if not text:
        return None
    if not text.endswith("d") or not text[:-1].isdigit():
        raise ValueError("--since must use day syntax like '7d'.")
    return datetime.now(timezone.utc) - timedelta(days=int(text[:-1]))


def _optional_text(state: Any, key: str) -> str | None:
    if not isinstance(state, dict):
        return None
    value = state.get(key)
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _render_table(rows: list[dict[str, Any]]) -> str:
    headers = ("query_id", "validated", "last_cycle_succeeded", "last_succeeded_at", "row_count", "duration_ms", "last_error")
    string_rows = [
        {header: _stringify_cell(row.get(header)) for header in headers}
        for row in rows
    ]
    widths = {
        header: max(len(header), *(len(row[header]) for row in string_rows))
        for header in headers
    }
    lines = [
        "  ".join(header.ljust(widths[header]) for header in headers),
        "  ".join("-" * widths[header] for header in headers),
    ]
    for row in string_rows:
        lines.append("  ".join(row[header].ljust(widths[header]) for header in headers))
    return "\n".join(lines)


def _stringify_cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value)