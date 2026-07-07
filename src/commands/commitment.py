"""WI-2.7: `vertex commitment` CLI — add, update, list commitments."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import typer

from src.core.commitment_store import (
    CommitmentDirection,
    CommitmentEntry,
    append_commitment_slip,
    build_commitment_id,
    load_commitment_entries,
    save_commitment,
)
from src.core.journal import PROGRAMS_ROOT


app = typer.Typer(help="Manage program commitments (inbound/outbound).", invoke_without_command=True)


@app.callback(invoke_without_command=True)
def commitment_command(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    typer.echo(ctx.get_help())
    raise typer.Exit(code=0)


@app.command("list")
def list_commitments(
    program: str = typer.Option(..., "--program", help="Program ID."),
    direction: str | None = typer.Option(None, "--direction", help="Filter by direction: inbound or outbound."),
    status: str | None = typer.Option(None, "--status", help="Filter by status, e.g. active."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """List commitments for a program."""
    entries = load_commitment_entries(
        program.strip(),
        direction=direction,
        status=status,
    )
    if format == "json":
        typer.echo(json.dumps([_entry_to_dict(e) for e in entries], indent=2))
    else:
        if not entries:
            typer.echo("No commitments found.")
        else:
            for e in entries:
                _print_commitment(e)
    raise typer.Exit(code=0)


@app.command("add")
def add_commitment(
    program: str = typer.Option(..., "--program", help="Program ID."),
    title: str = typer.Option(..., "--title", help="Commitment title."),
    dri: str = typer.Option(..., "--dri", help="DRI (alias or email)."),
    due_date: str = typer.Option(..., "--due-date", help="Due date (YYYY-MM-DD)."),
    direction: str = typer.Option("outbound", "--direction", help="Direction: inbound or outbound."),
    description: str = typer.Option("", "--description", help="Optional description."),
    entity_ref: str | None = typer.Option(None, "--entity-ref", help="Optional entity reference."),
    status: str = typer.Option("active", "--status", help="Status (default: active)."),
) -> None:
    """Add a new commitment."""
    if direction not in (CommitmentDirection.INBOUND, CommitmentDirection.OUTBOUND):
        typer.echo(f"Error: direction must be 'inbound' or 'outbound', got: {direction!r}", err=True)
        raise typer.Exit(code=1)

    commitment_id = build_commitment_id()
    entry = CommitmentEntry(
        commitment_id=commitment_id,
        title=title.strip(),
        dri=dri.strip(),
        due_date=due_date.strip(),
        direction=direction,
        status=status.strip(),
        description=description.strip(),
        entity_ref=entity_ref.strip() if entity_ref else None,
        slip_history=(),
        program_id=program.strip(),
    )
    save_commitment(program.strip(), entry)
    typer.echo(f"Commitment {commitment_id} added.")
    raise typer.Exit(code=0)


@app.command("update")
def update_commitment(
    program: str = typer.Option(..., "--program", help="Program ID."),
    commitment_id: str = typer.Option(..., "--id", help="Commitment ID."),
    slip_to: str | None = typer.Option(None, "--slip-to", help="New due date (YYYY-MM-DD) for slip."),
    reason: str = typer.Option("", "--reason", help="Reason for slip (ref to signal/fact ID)."),
    status: str | None = typer.Option(None, "--status", help="New status."),
) -> None:
    """Update a commitment (slip date or status change)."""
    entries = load_commitment_entries(program.strip())
    matching = [e for e in entries if e.commitment_id == commitment_id.strip()]
    if not matching:
        typer.echo(f"Error: commitment {commitment_id!r} not found.", err=True)
        raise typer.Exit(code=1)

    existing = matching[0]

    if slip_to:
        append_commitment_slip(
            program.strip(),
            commitment_id.strip(),
            new_due_date=slip_to.strip(),
            old_due_date=existing.due_date,
            reason=reason.strip(),
        )
        typer.echo(f"Commitment {commitment_id} slipped to {slip_to}.")

    if status:
        # Re-save with updated status
        from src.core.commitment_store import save_commitment
        updated = CommitmentEntry(
            commitment_id=existing.commitment_id,
            title=existing.title,
            dri=existing.dri,
            due_date=existing.due_date,
            direction=existing.direction,
            status=status.strip(),
            description=existing.description,
            entity_ref=existing.entity_ref,
            slip_history=existing.slip_history,
            program_id=existing.program_id,
        )
        save_commitment(program.strip(), updated)
        typer.echo(f"Commitment {commitment_id} status → {status}.")

    raise typer.Exit(code=0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _print_commitment(e: CommitmentEntry) -> None:
    direction_marker = "←" if e.direction == CommitmentDirection.INBOUND else "→"
    slip_note = f" [SLIPPED ×{e.slip_count}]" if e.is_slipped else ""
    typer.echo(f"  {direction_marker} [{e.status}] {e.commitment_id}: {e.title} (DRI: {e.dri}, due: {e.due_date}){slip_note}")


def _entry_to_dict(e: CommitmentEntry) -> dict:
    return {
        "commitment_id": e.commitment_id,
        "title": e.title,
        "dri": e.dri,
        "due_date": e.due_date,
        "direction": e.direction,
        "status": e.status,
        "description": e.description,
        "entity_ref": e.entity_ref,
        "slip_count": e.slip_count,
        "slip_history": [
            {"slipped_at": s.slipped_at.isoformat(), "old_due_date": s.old_due_date, "new_due_date": s.new_due_date, "reason": s.reason}
            for s in e.slip_history
        ],
        "program_id": e.program_id,
    }
