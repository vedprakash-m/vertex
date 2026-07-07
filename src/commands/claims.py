from __future__ import annotations

import csv
from typing import Any, Literal, cast
from dataclasses import asdict
from datetime import datetime, timezone
from io import StringIO
import json
import os

import typer

from src.core.claim_tracker import (
    PROGRAMS_ROOT,
    append_claim_status_update,
    assess_claim_entries,
    load_latest_claim_statuses,
    load_open_claims,
    load_open_decision_asks,
    locate_tracked_entry,
    resolve_entry_status,
)
from src.core.models_v2 import ClaimEntry, ClaimStatusUpdate, DecisionAsk


app = typer.Typer(help="List and resolve tracked claims and decision asks.", invoke_without_command=True)


@app.callback(invoke_without_command=True)
def claims_command(
    ctx: typer.Context,
    program: str | None = typer.Option(None, "--program", help="Program id, e.g. myprogram."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if program is None or not program.strip():
        raise typer.BadParameter("--program is required.")
    _print_open_claims(program.strip(), format=format)
    raise typer.Exit(code=0)


@app.command("resolve")
def resolve_claim_command(
    claim_id: str = typer.Option(..., "--id", help="Claim or decision-ask id."),
    status: str = typer.Option(..., "--status", help="Status to record."),
    program: str | None = typer.Option(None, "--program", help="Optional program id override."),
    note: str | None = typer.Option(None, "--note", help="Optional resolution note."),
    reviewer: str | None = typer.Option(None, "--reviewer", help="Reviewer alias. Defaults to the current OS user."),
) -> None:
    located = locate_tracked_entry(claim_id.strip(), program_id=(program.strip() if program else None), programs_root=PROGRAMS_ROOT)
    if located is None:
        raise typer.BadParameter(f"Could not uniquely resolve tracked entry '{claim_id}'.")

    next_status = _normalize_status(located.entry, status)
    if next_status == located.effective_status:
        typer.echo(f"Tracked entry {claim_id} is already {next_status}.")
        raise typer.Exit(code=0)

    resolved_reviewer = _default_reviewer_identity(reviewer)
    append_claim_status_update(
        located.program_id,
        ClaimStatusUpdate(
            claim_id=located.entry.id,
            new_status=next_status,
            updated_at=datetime.now(timezone.utc),
            updated_by=resolved_reviewer,
            note=(note.strip() if note is not None and note.strip() else None),
        ),
        programs_root=PROGRAMS_ROOT,
    )
    typer.echo(f"Updated {claim_id} in {located.program_id} to {next_status}.")
    raise typer.Exit(code=0)


def _print_open_claims(program_id: str, *, format: str) -> None:
    claims = load_open_claims(program_id, programs_root=PROGRAMS_ROOT)
    claim_assessments = assess_claim_entries(claims, items=(), as_of=datetime.now(timezone.utc))
    decision_asks = load_open_decision_asks(program_id, programs_root=PROGRAMS_ROOT)
    latest_statuses = load_latest_claim_statuses(program_id, programs_root=PROGRAMS_ROOT)
    open_claims_payload = [
        asdict(assessment.claim)
        | {
            "effective_status": assessment.effective_status,
            "reason": assessment.reason,
        }
        for assessment in claim_assessments
    ]
    open_asks_payload = [
        asdict(decision_ask)
        | {
            "effective_status": resolve_entry_status(decision_ask, latest_statuses),
        }
        for decision_ask in decision_asks
    ]

    if format == "json":
        typer.echo(
            json.dumps(
                {
                    "program_id": program_id,
                    "open_claims": open_claims_payload,
                    "open_decision_asks": open_asks_payload,
                },
                indent=2,
                sort_keys=True,
                default=_json_default,
            )
        )
        return
    if format == "csv":
        typer.echo(render_claims_csv(open_claims_payload, open_asks_payload), nl=False)
        return
    if format != "human":
        raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")

    if not claim_assessments and not decision_asks:
        typer.echo(f"No open claims or decision asks for {program_id}.")
        return

    typer.echo(f"OPEN CLAIMS — {program_id} ({len(claim_assessments)})")
    if not claim_assessments:
        typer.echo("- None")
    for assessment in claim_assessments:
        claim = assessment.claim
        due_label = claim.due_date.isoformat() if claim.due_date is not None else "-"
        typer.echo(
            f"- {claim.id} | issue #{claim.issue_number} | {assessment.effective_status} | due {due_label} | {claim.workstream_id or '-'}"
        )
        typer.echo(f"  {claim.text}")
        if assessment.reason is not None:
            typer.echo(f"  {assessment.reason}")

    typer.echo("")
    typer.echo(f"OPEN ASKS — {program_id} ({len(decision_asks)})")
    if not decision_asks:
        typer.echo("- None")
        return
    for decision_ask in decision_asks:
        typer.echo(
            f"- {decision_ask.id} | issue #{decision_ask.issue_number} | {resolve_entry_status(decision_ask, latest_statuses)}"
        )
        typer.echo(f"  {decision_ask.text}")


def render_claims_csv(open_claims: list[dict[str, object]], open_decision_asks: list[dict[str, object]]) -> str:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        (
            "entry_type",
            "id",
            "program_id",
            "edition_id",
            "issue_number",
            "workstream_id",
            "status",
            "claim_date",
            "due_date",
            "ask_date",
            "owner_alias",
            "entity_refs",
            "text",
            "reason",
        )
    )
    for claim in open_claims:
        writer.writerow(
            (
                "claim",
                claim["id"],
                claim["program_id"],
                claim["edition_id"],
                claim["issue_number"],
                claim["workstream_id"] or "",
                claim["effective_status"],
                _csv_date(claim.get("claim_date")),
                _csv_date(claim.get("due_date")),
                "",
                claim["owner_alias"] or "",
                "|".join(str(r) for r in cast(list[Any], claim.get("entity_refs") or [])),
                claim["text"],
                claim.get("reason") or "",
            )
        )
    for decision_ask in open_decision_asks:
        writer.writerow(
            (
                "decision_ask",
                decision_ask["id"],
                decision_ask["program_id"],
                decision_ask["edition_id"],
                decision_ask["issue_number"],
                "",
                decision_ask["effective_status"],
                "",
                "",
                _csv_date(decision_ask.get("ask_date")),
                decision_ask["owner_alias"] or "",
                "|".join(str(r) for r in cast(list[Any], decision_ask.get("entity_refs") or [])),
                decision_ask["text"],
                "",
            )
        )
    return buffer.getvalue()


def _normalize_status(entry: ClaimEntry | DecisionAsk, value: str) -> Literal["open", "met", "contradicted", "stale", "deferred", "resolved"]:
    normalized = value.strip().lower()
    if isinstance(entry, DecisionAsk):
        if normalized == "met":
            normalized = "resolved"
        if normalized not in {"open", "resolved", "deferred"}:
            raise typer.BadParameter("Decision asks support --status open|resolved|deferred.")
        return cast(Literal["open", "met", "contradicted", "stale", "deferred", "resolved"], normalized)
    if normalized == "resolved":
        normalized = "met"
    if normalized not in {"open", "met", "contradicted", "deferred"}:
        raise typer.BadParameter("Claims support --status open|met|contradicted|deferred.")
    return cast(Literal["open", "met", "contradicted", "stale", "deferred", "resolved"], normalized)


def _default_reviewer_identity(reviewer: str | None) -> str:
    if reviewer is not None and reviewer.strip():
        return reviewer.strip()
    return (os.environ.get("USERNAME") or os.environ.get("USER") or "manual").strip() or "manual"


def _csv_date(value: object) -> str:
    if value is None:
        return ""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _json_default(value: object) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value)!r} is not JSON serializable")