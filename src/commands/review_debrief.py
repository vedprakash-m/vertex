from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

import typer

from src.core.action_tracker import PROGRAMS_ROOT, append_action, build_action_id
from src.core.decision_register import save_decisions
from src.core.edition_resolver import resolve_edition_paths
from src.core.models_v2 import ActionItem, ActionSourceType, ActionStatus, DecisionEntry, DecisionStatus
from src.core.program_fact_store import load_program_facts, project_decision_entries


@dataclass(frozen=True, slots=True)
class DebriefActionDraft:
    owner_alias: str
    due_date: date | None
    text: str


def _load_decision_entries(program_id: str) -> tuple[DecisionEntry, ...]:
    return project_decision_entries(
        load_program_facts(
            program_id,
            programs_root=PROGRAMS_ROOT,
            fact_types=("decision.entry",),
        )
    )


def review_debrief_command(
    program: str | None = typer.Option(None, "--program", help="Program id, e.g. myprogram."),
    edition: str | None = typer.Option(None, "--edition", help="Edition id used to resolve the program when --program is omitted."),
    issue: int | None = typer.Option(None, "--issue", help="Optional issue number associated with the debrief."),
    title: str = typer.Option(..., "--title", help="Decision title."),
    context: str = typer.Option(..., "--context", help="Decision context or LT feedback framing."),
    decision: str = typer.Option(..., "--decision", help="Decision outcome recorded from the debrief."),
    reviewer: str | None = typer.Option(None, "--reviewer", help="Reviewer or decider alias. Defaults to the current OS user."),
    review_by: str | None = typer.Option(None, "--review-by", help="Optional YYYY-MM-DD follow-up review date for the decision."),
    workstream: str | None = typer.Option(None, "--workstream", help="Optional workstream id for the recorded decision and follow-up actions."),
    entity_ref: list[str] | None = typer.Option(None, "--entity-ref", help="Repeat to attach entity refs such as WI:7818186."),
    alternative: list[str] | None = typer.Option(None, "--alternative", help="Repeat to record alternatives considered."),
    action: list[str] | None = typer.Option(
        None,
        "--action",
        help="Repeat to add follow-up actions using 'owner_alias|YYYY-MM-DD|text' or 'owner_alias||text'.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview the debrief write without mutating decisions.yaml or actions.jsonl."),
) -> None:
    program_id = _resolve_program_id(program=program, edition=edition)
    decision_date = datetime.now(timezone.utc).date()
    reviewer_alias = _default_actor(reviewer)
    review_by_date = _parse_optional_date(review_by)
    entity_refs = tuple(item.strip() for item in entity_ref or [] if item.strip())
    alternatives = tuple(item.strip() for item in alternative or [] if item.strip())
    action_drafts = tuple(_parse_action_draft(value) for value in action or [])
    linked_work_item_ids = _linked_work_item_ids_from_refs(entity_refs)
    current_time = datetime.now(timezone.utc)

    decision_entry = DecisionEntry(
        id=str(uuid4()),
        program_id=program_id,
        title=title.strip(),
        context=_render_debrief_context(context=context, edition=edition, issue=issue),
        decision=decision.strip(),
        rationale=None,
        alternatives_considered=alternatives,
        decided_by=reviewer_alias,
        decision_date=decision_date,
        status=DecisionStatus.DECIDED,
        superseded_by=None,
        linked_claim_id=None,
        linked_risk_id=None,
        linked_action_ids=(),
        workstream_id=workstream.strip() if workstream is not None and workstream.strip() else None,
        entity_refs=entity_refs,
        review_by=review_by_date,
    )

    action_entries = tuple(
        ActionItem(
            id=build_action_id(
                program_id,
                text=draft.text,
                owner_alias=draft.owner_alias,
                due_date=draft.due_date,
                source_signal_id=None,
                workstream_id=decision_entry.workstream_id,
                linked_work_item_ids=linked_work_item_ids,
            ),
            program_id=program_id,
            text=draft.text,
            owner_alias=draft.owner_alias,
            due_date=draft.due_date,
            status=ActionStatus.OPEN,
            source_signal_id=None,
            source_type=ActionSourceType.REVIEW_FEEDBACK,
            linked_work_item_ids=linked_work_item_ids,
            linked_claim_id=None,
            linked_risk_id=None,
            workstream_id=decision_entry.workstream_id,
            created_at=current_time,
            resolved_at=None,
            resolution_note=None,
        )
        for draft in action_drafts
    )

    preview_lines = [
        f"Program: {program_id}",
        f"Decision: {decision_entry.title}",
        f"Context: {decision_entry.context}",
        f"Outcome: {decision_entry.decision}",
        f"Decided by: {decision_entry.decided_by}",
    ]
    if decision_entry.review_by is not None:
        preview_lines.append(f"Review by: {decision_entry.review_by.isoformat()}")
    if decision_entry.entity_refs:
        preview_lines.append(f"Entity refs: {', '.join(decision_entry.entity_refs)}")
    for entry in action_entries:
        due_text = entry.due_date.isoformat() if entry.due_date is not None else "-"
        preview_lines.append(f"Action: {entry.owner_alias} | due {due_text} | {entry.text}")

    if dry_run:
        typer.echo("Review debrief preview")
        typer.echo("----------------------")
        for line in preview_lines:
            typer.echo(line)
        raise typer.Exit(code=0)

    decisions = list(_load_decision_entries(program_id))
    decisions.append(decision_entry)
    save_decisions(program_id, tuple(decisions), programs_root=PROGRAMS_ROOT)
    for entry in action_entries:
        append_action(program_id, entry, programs_root=PROGRAMS_ROOT)

    typer.echo(f"Added debrief decision {decision_entry.id} to {program_id}.")
    if action_entries:
        typer.echo(f"Added {len(action_entries)} follow-up action(s) to {program_id}.")
    raise typer.Exit(code=0)


def _resolve_program_id(*, program: str | None, edition: str | None) -> str:
    if program is not None and program.strip():
        return program.strip()
    if edition is not None and edition.strip():
        resolved = resolve_edition_paths(edition.strip(), programs_root=PROGRAMS_ROOT)
        if resolved is None:
            raise typer.BadParameter(f"Unknown edition '{edition}'.")
        return resolved.program_id
    raise typer.BadParameter("Use --program or --edition to identify the target program.")


def _render_debrief_context(*, context: str, edition: str | None, issue: int | None) -> str:
    normalized = context.strip()
    suffix_parts: list[str] = []
    if edition is not None and edition.strip():
        suffix_parts.append(f"edition {edition.strip()}")
    if issue is not None:
        suffix_parts.append(f"issue {issue:03d}")
    if not suffix_parts:
        return normalized
    return f"{normalized} (Debrief from {' / '.join(suffix_parts)})"


def _default_actor(value: str | None) -> str:
    if value is not None and value.strip():
        return value.strip()
    return (Path.home().name or "system").strip() or "system"


def _parse_optional_date(value: str | None) -> date | None:
    if value is None or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError as error:
        raise typer.BadParameter(f"Invalid date '{value}'. Use YYYY-MM-DD.") from error


def _parse_action_draft(value: str) -> DebriefActionDraft:
    parts = value.split("|", 2)
    if len(parts) != 3:
        raise typer.BadParameter("--action entries must use 'owner_alias|YYYY-MM-DD|text' or 'owner_alias||text'.")
    owner_alias = parts[0].strip()
    due_date_text = parts[1].strip()
    text = parts[2].strip()
    if not owner_alias:
        raise typer.BadParameter("--action owner alias is required before the first '|'.")
    if not text:
        raise typer.BadParameter("--action text is required after the second '|'.")
    return DebriefActionDraft(
        owner_alias=owner_alias,
        due_date=_parse_optional_date(due_date_text) if due_date_text else None,
        text=text,
    )


def _linked_work_item_ids_from_refs(entity_refs: tuple[str, ...]) -> tuple[int, ...]:
    work_item_ids: list[int] = []
    for entity_ref in entity_refs:
        if not entity_ref.upper().startswith("WI:"):
            continue
        value = entity_ref.split(":", 1)[1].strip()
        if value.isdigit():
            work_item_ids.append(int(value))
    return tuple(dict.fromkeys(work_item_ids))