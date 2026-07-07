from __future__ import annotations

import csv
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from io import StringIO
import json
import os
from uuid import uuid4

import typer

from src.core.assumption_tracker import check_validation_due, save_assumptions
from src.core.journal import PROGRAMS_ROOT
from src.core.models_v2 import Assumption, AssumptionStatus, RiskCategory, RiskEntry, RiskImpact, RiskProbability, RiskStatus
from src.core.program_fact_store import load_program_facts, project_assumptions, project_risk_entries
from src.core.risk_register_engine import build_risk_id, compute_risk_score, save_risk_register


app = typer.Typer(help="Manage the program assumptions register.", invoke_without_command=True)


def _load_current_assumptions(program_id: str) -> tuple[Assumption, ...]:
    return project_assumptions(
        load_program_facts(
            program_id,
            programs_root=PROGRAMS_ROOT,
            fact_types=("assumption.entry",),
        )
    )


@app.callback(invoke_without_command=True)
def assumptions_command(
    ctx: typer.Context,
    program: str | None = typer.Option(None, "--program", help="Program id, e.g. myprogram."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if program is None or not program.strip():
        raise typer.BadParameter("--program is required.")
    _print_assumptions(program.strip(), status_filter=None, format=format)
    raise typer.Exit(code=0)


@app.command("list")
def list_assumptions_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    status: str | None = typer.Option(None, "--status", help="Optional status filter."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    status_filter = AssumptionStatus.from_string(status) if status is not None and status.strip() else None
    _print_assumptions(program.strip(), status_filter=status_filter, format=format)
    raise typer.Exit(code=0)


@app.command("add")
def add_assumption_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    text: str = typer.Option(..., "--text", help="Assumption text."),
    validation_method: str | None = typer.Option(None, "--validation-method", help="Optional validation method."),
    validation_due: str | None = typer.Option(None, "--validation-due", help="Optional YYYY-MM-DD validation due date."),
    milestone: str | None = typer.Option(None, "--milestone", help="Optional linked milestone id."),
    owner: str | None = typer.Option(None, "--owner", help="Owner alias. Defaults to current OS user when omitted."),
    identified_date: str | None = typer.Option(None, "--identified-date", help="Optional YYYY-MM-DD identified date."),
    entity_ref: list[str] | None = typer.Option(None, "--entity-ref", help="Repeat to add entity refs."),
) -> None:
    program_id = program.strip()
    text_value = text.strip()
    if not text_value:
        raise typer.BadParameter("--text is required.")

    assumption = Assumption(
        id=str(uuid4()),
        program_id=program_id,
        text=text_value,
        validation_method=(validation_method.strip() if validation_method is not None and validation_method.strip() else None),
        validation_due=_parse_optional_date(validation_due),
        status=AssumptionStatus.UNVALIDATED,
        linked_risk_id=None,
        linked_milestone_id=(milestone.strip() if milestone is not None and milestone.strip() else None),
        owner_alias=_default_actor(owner),
        identified_date=_parse_optional_date(identified_date) or datetime.now(timezone.utc).date(),
        entity_refs=tuple(item.strip() for item in entity_ref or [] if item.strip()),
    )

    entries = list(_load_current_assumptions(program_id))
    entries.append(assumption)
    save_assumptions(program_id, _sort_assumptions(tuple(entries)), programs_root=PROGRAMS_ROOT)
    typer.echo(f"Added assumption {assumption.id} to {program_id}.")
    raise typer.Exit(code=0)


@app.command("validate")
def validate_assumption_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    assumption_id: str = typer.Option(..., "--id", help="Assumption id."),
) -> None:
    program_id = program.strip()
    entries = list(_load_current_assumptions(program_id))
    match_index = next((index for index, entry in enumerate(entries) if entry.id == assumption_id.strip()), None)
    if match_index is None:
        raise typer.BadParameter(f"Assumption '{assumption_id}' was not found in {program_id}.")

    current = entries[match_index]
    updated = replace(
        current,
        status=AssumptionStatus.CONFIRMED,
        resolved_date=datetime.now(timezone.utc).date(),
    )
    if updated == current:
        typer.echo(f"Assumption {current.id} is unchanged.")
        raise typer.Exit(code=0)

    entries[match_index] = updated
    save_assumptions(program_id, _sort_assumptions(tuple(entries)), programs_root=PROGRAMS_ROOT)
    typer.echo(f"Validated assumption {updated.id} in {program_id}.")
    raise typer.Exit(code=0)


@app.command("invalidate")
def invalidate_assumption_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    assumption_id: str = typer.Option(..., "--id", help="Assumption id."),
    no_prompt: bool = typer.Option(False, "--no-prompt", help="Invalidate without prompting to create a linked risk."),
    force: bool = typer.Option(False, "--force", help="Alias for --no-prompt for headless execution."),
) -> None:
    program_id = program.strip()
    entries = list(_load_current_assumptions(program_id))
    match_index = next((index for index, entry in enumerate(entries) if entry.id == assumption_id.strip()), None)
    if match_index is None:
        raise typer.BadParameter(f"Assumption '{assumption_id}' was not found in {program_id}.")

    current = entries[match_index]
    updated = replace(
        current,
        status=AssumptionStatus.INVALIDATED,
        resolved_date=datetime.now(timezone.utc).date(),
    )
    warning: str | None = None
    if current.linked_risk_id is None:
        if force or no_prompt:
            warning = (
                f"Assumption {current.id} was invalidated without a linked risk. "
                "Re-run interactively or create a linked risk manually."
            )
        elif typer.confirm(f"Create linked risk for assumption {current.id}?", default=True):
            linked_risk_id = _create_or_link_risk_from_assumption(program_id, current)
            updated = replace(updated, linked_risk_id=linked_risk_id)
    if updated == current:
        typer.echo(f"Assumption {current.id} is unchanged.")
        raise typer.Exit(code=0)

    entries[match_index] = updated
    save_assumptions(program_id, _sort_assumptions(tuple(entries)), programs_root=PROGRAMS_ROOT)
    typer.echo(f"Invalidated assumption {updated.id} in {program_id}.")
    if updated.linked_risk_id is not None:
        typer.echo(f"Linked risk: {updated.linked_risk_id}")
    if warning is not None:
        typer.echo(f"Warning: {warning}")
    raise typer.Exit(code=0)


def _print_assumptions(program_id: str, *, status_filter: AssumptionStatus | None, format: str) -> None:
    entries = _load_current_assumptions(program_id)
    if status_filter is not None:
        entries = tuple(entry for entry in entries if entry.status == status_filter)
    today = datetime.now(timezone.utc).date()
    sorted_entries = _sort_assumptions(entries)
    if format == "json":
        typer.echo(
            json.dumps(
                {
                    "program_id": program_id,
                    "status_filter": status_filter.value if status_filter is not None else None,
                    "assumptions": [
                        asdict(entry)
                        | {
                            "staleness": ("overdue" if entry in check_validation_due((entry,), today) else "current"),
                        }
                        for entry in sorted_entries
                    ],
                },
                indent=2,
                sort_keys=True,
                default=_json_default,
            )
        )
        return
    if format == "csv":
        typer.echo(render_assumptions_csv(sorted_entries, as_of=today), nl=False)
        return
    if format != "human":
        raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")
    if not sorted_entries:
        typer.echo(f"No assumptions found for {program_id}.")
        return

    typer.echo(f"ASSUMPTIONS - {program_id} ({len(sorted_entries)})")
    for entry in sorted_entries:
        due_label = entry.validation_due.isoformat() if entry.validation_due is not None else "-"
        overdue_label = "overdue" if entry in check_validation_due((entry,), today) else "current"
        owner_label = entry.owner_alias or "-"
        typer.echo(
            f"- {entry.id} | {entry.status.value} | {overdue_label} | due {due_label} | owner {owner_label}"
        )
        typer.echo(f"  {entry.text}")


def _sort_assumptions(entries: tuple[Assumption, ...]) -> tuple[Assumption, ...]:
    today = datetime.now(timezone.utc).date()
    return tuple(
        sorted(
            entries,
            key=lambda entry: (
                0 if entry in check_validation_due((entry,), today) else 1,
                0 if entry.status is AssumptionStatus.UNVALIDATED else 1,
                entry.validation_due or date.max,
                entry.identified_date,
                entry.text.lower(),
            ),
        )
    )


def _create_or_link_risk_from_assumption(program_id: str, assumption: Assumption) -> str:
    risk_owner = assumption.owner_alias or _default_actor(None)
    title = f"Invalidated assumption: {assumption.text}"
    description = f"Assumption invalidated: {assumption.text}"
    probability = RiskProbability.from_string(typer.prompt("Risk probability", default="possible").strip())
    impact = RiskImpact.from_string(typer.prompt("Risk impact", default="medium").strip())
    category = RiskCategory.from_string(typer.prompt("Risk category", default="dependency").strip())
    risk_id = build_risk_id(program_id, title=title, description=description, owner_alias=risk_owner)

    entries = list(
        project_risk_entries(
            load_program_facts(
                program_id,
                programs_root=PROGRAMS_ROOT,
                fact_types=("risk.entry",),
            )
        )
    )
    if any(entry.id == risk_id for entry in entries):
        return risk_id

    risk_entry = RiskEntry(
        id=risk_id,
        program_id=program_id,
        title=title,
        description=description,
        probability=probability,
        impact=impact,
        category=category,
        owner_alias=risk_owner,
        mitigation_plan=None,
        mitigation_due_date=None,
        linked_workstream_ids=(),
        linked_work_item_ids=(),
        linked_milestone_ids=((assumption.linked_milestone_id,) if assumption.linked_milestone_id is not None else ()),
        linked_claim_ids=(),
        linked_action_ids=(),
        status=RiskStatus.OPEN,
        identified_date=datetime.now(timezone.utc).date(),
        identified_in_vertex_issue=None,
        last_reviewed_date=None,
        entity_refs=assumption.entity_refs,
    )
    entries.append(risk_entry)
    save_risk_register(program_id, _sort_risks(tuple(entries)), programs_root=PROGRAMS_ROOT)
    return risk_entry.id


def _sort_risks(entries: tuple[RiskEntry, ...]) -> tuple[RiskEntry, ...]:
    return tuple(
        sorted(
            entries,
            key=lambda entry: (
                0 if entry.status in {RiskStatus.OPEN, RiskStatus.ESCALATED} else 1,
                -compute_risk_score(entry),
                entry.title.lower(),
            ),
        )
    )


def _default_actor(value: str | None) -> str:
    if value is not None and value.strip():
        return value.strip()
    return (os.environ.get("USERNAME") or os.environ.get("USER") or "manual").strip() or "manual"


def _parse_optional_date(value: str | None) -> date | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return date.fromisoformat(stripped)


def render_assumptions_csv(entries: tuple[Assumption, ...], *, as_of: date) -> str:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        (
            "id",
            "program_id",
            "status",
            "staleness",
            "validation_due",
            "validation_method",
            "owner_alias",
            "linked_milestone_id",
            "linked_risk_id",
            "identified_date",
            "resolved_date",
            "entity_refs",
            "text",
        )
    )
    for entry in entries:
        writer.writerow(
            (
                entry.id,
                entry.program_id,
                entry.status.value,
                "overdue" if entry in check_validation_due((entry,), as_of) else "current",
                _csv_date(entry.validation_due),
                entry.validation_method or "",
                entry.owner_alias or "",
                entry.linked_milestone_id or "",
                entry.linked_risk_id or "",
                entry.identified_date.isoformat(),
                _csv_date(entry.resolved_date),
                "|".join(entry.entity_refs),
                entry.text,
            )
        )
    return buffer.getvalue()


def _csv_date(value: date | None) -> str:
    return value.isoformat() if value is not None else ""


def _json_default(value: object) -> object:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")