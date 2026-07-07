from __future__ import annotations

import csv
from dataclasses import asdict
from datetime import date, datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
from typing import cast

import typer

from src.commands.ado import apply_ado_proposal
from src.core.action_tracker import assess_action_staleness, load_action_resolution_candidate_ids, update_action_status
from src.core.journal import PROGRAMS_ROOT
from src.core.models_v2 import ActionItem, ActionStatus
from src.core.program_fact_store import load_program_facts, project_action_items


REPO_ROOT = Path(__file__).resolve().parents[2]


app = typer.Typer(help="List and review extracted actions.", invoke_without_command=True)


def _load_action_items(program_id: str) -> tuple[ActionItem, ...]:
    return project_action_items(
        load_program_facts(
            program_id,
            programs_root=PROGRAMS_ROOT,
            fact_types=("action.item",),
        )
    )


def _meeting_close_batch_summaries(entries: tuple[ActionItem, ...]) -> tuple[dict[str, object], ...]:
    grouped: dict[str, list[ActionItem]] = {}
    for entry in entries:
        meeting_id = _meeting_close_source_meeting_id(entry)
        if meeting_id is None:
            continue
        grouped.setdefault(meeting_id, []).append(entry)
    summaries: list[dict[str, object]] = []
    for meeting_id, actions in sorted(grouped.items()):
        due_dates = sorted(action.due_date for action in actions if action.due_date is not None)
        counts_by_status: dict[str, int] = {}
        for action in actions:
            counts_by_status[action.status.value] = counts_by_status.get(action.status.value, 0) + 1
        summaries.append(
            {
                "meeting_id": meeting_id,
                "action_count": len(actions),
                "status_counts": counts_by_status,
                "earliest_due_date": due_dates[0].isoformat() if due_dates else None,
            }
        )
    return tuple(summaries)


def _meeting_close_batch_patterns(entries: tuple[ActionItem, ...]) -> tuple[dict[str, object], ...]:
    grouped: dict[str, list[ActionItem]] = {}
    for entry in entries:
        meeting_id = _meeting_close_source_meeting_id(entry)
        if meeting_id is None:
            continue
        grouped.setdefault(meeting_id, []).append(entry)
    if len(grouped) < 2:
        return ()

    workstream_meetings: dict[str, set[str]] = {}
    owner_meetings: dict[str, set[str]] = {}
    owner_workstream_meetings: dict[tuple[str, str], set[str]] = {}
    owner_work_item_meetings: dict[tuple[str, int], set[str]] = {}
    owner_claim_meetings: dict[tuple[str, str], set[str]] = {}
    owner_risk_meetings: dict[tuple[str, str], set[str]] = {}
    workstream_claim_meetings: dict[tuple[str, str], set[str]] = {}
    workstream_risk_meetings: dict[tuple[str, str], set[str]] = {}
    work_item_claim_meetings: dict[tuple[int, str], set[str]] = {}
    work_item_risk_meetings: dict[tuple[int, str], set[str]] = {}
    owner_due_date_meetings: dict[tuple[str, str], set[str]] = {}
    workstream_due_date_meetings: dict[tuple[str, str], set[str]] = {}
    due_date_meetings: dict[str, set[str]] = {}
    action_text_meetings: dict[str, set[str]] = {}
    claim_meetings: dict[str, set[str]] = {}
    risk_meetings: dict[str, set[str]] = {}
    item_meetings: dict[int, set[str]] = {}
    for meeting_id, actions in grouped.items():
        for workstream_id in {action.workstream_id for action in actions if action.workstream_id}:
            workstream_meetings.setdefault(workstream_id, set()).add(meeting_id)
        for owner_alias in {action.owner_alias for action in actions if action.owner_alias}:
            owner_meetings.setdefault(owner_alias, set()).add(meeting_id)
        for owner_workstream in {
            (action.owner_alias, action.workstream_id)
            for action in actions
            if action.owner_alias and action.workstream_id
        }:
            owner_workstream_meetings.setdefault(owner_workstream, set()).add(meeting_id)
        for owner_work_item in {
            (action.owner_alias, work_item_id)
            for action in actions
            if action.owner_alias
            for work_item_id in action.linked_work_item_ids
        }:
            owner_work_item_meetings.setdefault(owner_work_item, set()).add(meeting_id)
        for owner_claim in {
            (action.owner_alias, action.linked_claim_id)
            for action in actions
            if action.owner_alias and action.linked_claim_id
        }:
            owner_claim_meetings.setdefault(owner_claim, set()).add(meeting_id)
        for owner_risk in {
            (action.owner_alias, action.linked_risk_id)
            for action in actions
            if action.owner_alias and action.linked_risk_id
        }:
            owner_risk_meetings.setdefault(owner_risk, set()).add(meeting_id)
        for workstream_claim in {
            (action.workstream_id, action.linked_claim_id)
            for action in actions
            if action.workstream_id and action.linked_claim_id
        }:
            workstream_claim_meetings.setdefault(workstream_claim, set()).add(meeting_id)
        for workstream_risk in {
            (action.workstream_id, action.linked_risk_id)
            for action in actions
            if action.workstream_id and action.linked_risk_id
        }:
            workstream_risk_meetings.setdefault(workstream_risk, set()).add(meeting_id)
        for work_item_claim in {
            (work_item_id, action.linked_claim_id)
            for action in actions
            if action.linked_claim_id
            for work_item_id in action.linked_work_item_ids
        }:
            work_item_claim_meetings.setdefault(work_item_claim, set()).add(meeting_id)
        for work_item_risk in {
            (work_item_id, action.linked_risk_id)
            for action in actions
            if action.linked_risk_id
            for work_item_id in action.linked_work_item_ids
        }:
            work_item_risk_meetings.setdefault(work_item_risk, set()).add(meeting_id)
        for owner_due_date in {
            (action.owner_alias, action.due_date.isoformat())
            for action in actions
            if action.owner_alias and action.due_date is not None
        }:
            owner_due_date_meetings.setdefault(owner_due_date, set()).add(meeting_id)
        for workstream_due_date in {
            (action.workstream_id, action.due_date.isoformat())
            for action in actions
            if action.workstream_id and action.due_date is not None
        }:
            workstream_due_date_meetings.setdefault(workstream_due_date, set()).add(meeting_id)
        for due_date in {action.due_date.isoformat() for action in actions if action.due_date is not None}:
            due_date_meetings.setdefault(due_date, set()).add(meeting_id)
        for normalized_text in {_normalize_action_pattern_text(action.text) for action in actions if action.text.strip()}:
            if normalized_text:
                action_text_meetings.setdefault(normalized_text, set()).add(meeting_id)
        for claim_id in {action.linked_claim_id for action in actions if action.linked_claim_id}:
            claim_meetings.setdefault(claim_id, set()).add(meeting_id)
        for risk_id in {action.linked_risk_id for action in actions if action.linked_risk_id}:
            risk_meetings.setdefault(risk_id, set()).add(meeting_id)
        for work_item_id in {work_item_id for action in actions for work_item_id in action.linked_work_item_ids}:
            item_meetings.setdefault(work_item_id, set()).add(meeting_id)

    patterns: list[dict[str, object]] = []
    for workstream_id, meeting_ids in sorted(workstream_meetings.items()):
        if len(meeting_ids) < 2:
            continue
        patterns.append(
            {
                "pattern_type": "workstream",
                "key": workstream_id,
                "meeting_count": len(meeting_ids),
                "meeting_ids": tuple(sorted(meeting_ids)),
            }
        )
    for owner_alias, meeting_ids in sorted(owner_meetings.items()):
        if len(meeting_ids) < 2:
            continue
        patterns.append(
            {
                "pattern_type": "owner",
                "key": owner_alias,
                "meeting_count": len(meeting_ids),
                "meeting_ids": tuple(sorted(meeting_ids)),
            }
        )
    for owner_workstream, meeting_ids in sorted(owner_workstream_meetings.items()):
        if len(meeting_ids) < 2:
            continue
        owner_alias, workstream_id = owner_workstream
        patterns.append(
            {
                "pattern_type": "owner_workstream",
                "key": f"{owner_alias}:{workstream_id}",
                "label": f"{owner_alias} / {workstream_id}",
                "meeting_count": len(meeting_ids),
                "meeting_ids": tuple(sorted(meeting_ids)),
            }
        )
    for owner_work_item, meeting_ids in sorted(owner_work_item_meetings.items()):
        if len(meeting_ids) < 2:
            continue
        owner_alias, work_item_id = owner_work_item
        patterns.append(
            {
                "pattern_type": "owner_work_item",
                "key": f"{owner_alias}:WI:{work_item_id}",
                "label": f"{owner_alias} / WI:{work_item_id}",
                "meeting_count": len(meeting_ids),
                "meeting_ids": tuple(sorted(meeting_ids)),
            }
        )
    for owner_claim, meeting_ids in sorted(owner_claim_meetings.items()):
        if len(meeting_ids) < 2:
            continue
        owner_alias, claim_id = owner_claim
        patterns.append(
            {
                "pattern_type": "owner_claim",
                "key": f"{owner_alias}:{claim_id}",
                "label": f"{owner_alias} / {claim_id}",
                "meeting_count": len(meeting_ids),
                "meeting_ids": tuple(sorted(meeting_ids)),
            }
        )
    for owner_risk, meeting_ids in sorted(owner_risk_meetings.items()):
        if len(meeting_ids) < 2:
            continue
        owner_alias, risk_id = owner_risk
        patterns.append(
            {
                "pattern_type": "owner_risk",
                "key": f"{owner_alias}:{risk_id}",
                "label": f"{owner_alias} / {risk_id}",
                "meeting_count": len(meeting_ids),
                "meeting_ids": tuple(sorted(meeting_ids)),
            }
        )
    for workstream_claim, meeting_ids in sorted(workstream_claim_meetings.items()):
        if len(meeting_ids) < 2:
            continue
        workstream_id, claim_id = workstream_claim
        patterns.append(
            {
                "pattern_type": "workstream_claim",
                "key": f"{workstream_id}:{claim_id}",
                "label": f"{workstream_id} / {claim_id}",
                "meeting_count": len(meeting_ids),
                "meeting_ids": tuple(sorted(meeting_ids)),
            }
        )
    for workstream_risk, meeting_ids in sorted(workstream_risk_meetings.items()):
        if len(meeting_ids) < 2:
            continue
        workstream_id, risk_id = workstream_risk
        patterns.append(
            {
                "pattern_type": "workstream_risk",
                "key": f"{workstream_id}:{risk_id}",
                "label": f"{workstream_id} / {risk_id}",
                "meeting_count": len(meeting_ids),
                "meeting_ids": tuple(sorted(meeting_ids)),
            }
        )
    for work_item_claim, meeting_ids in sorted(work_item_claim_meetings.items()):
        if len(meeting_ids) < 2:
            continue
        work_item_id, claim_id = work_item_claim
        patterns.append(
            {
                "pattern_type": "work_item_claim",
                "key": f"WI:{work_item_id}:{claim_id}",
                "label": f"WI:{work_item_id} / {claim_id}",
                "meeting_count": len(meeting_ids),
                "meeting_ids": tuple(sorted(meeting_ids)),
            }
        )
    for work_item_risk, meeting_ids in sorted(work_item_risk_meetings.items()):
        if len(meeting_ids) < 2:
            continue
        work_item_id, risk_id = work_item_risk
        patterns.append(
            {
                "pattern_type": "work_item_risk",
                "key": f"WI:{work_item_id}:{risk_id}",
                "label": f"WI:{work_item_id} / {risk_id}",
                "meeting_count": len(meeting_ids),
                "meeting_ids": tuple(sorted(meeting_ids)),
            }
        )
    for owner_due_date, meeting_ids in sorted(owner_due_date_meetings.items()):
        if len(meeting_ids) < 2:
            continue
        owner_alias, due_date = owner_due_date
        patterns.append(
            {
                "pattern_type": "owner_due_date",
                "key": f"{owner_alias}:{due_date}",
                "label": f"{owner_alias} / {due_date}",
                "meeting_count": len(meeting_ids),
                "meeting_ids": tuple(sorted(meeting_ids)),
            }
        )
    for workstream_due_date, meeting_ids in sorted(workstream_due_date_meetings.items()):
        if len(meeting_ids) < 2:
            continue
        workstream_id, due_date = workstream_due_date
        patterns.append(
            {
                "pattern_type": "workstream_due_date",
                "key": f"{workstream_id}:{due_date}",
                "label": f"{workstream_id} / {due_date}",
                "meeting_count": len(meeting_ids),
                "meeting_ids": tuple(sorted(meeting_ids)),
            }
        )
    for due_date, meeting_ids in sorted(due_date_meetings.items()):
        if len(meeting_ids) < 2:
            continue
        patterns.append(
            {
                "pattern_type": "due_date",
                "key": due_date,
                "meeting_count": len(meeting_ids),
                "meeting_ids": tuple(sorted(meeting_ids)),
            }
        )
    for normalized_text, meeting_ids in sorted(action_text_meetings.items()):
        if len(meeting_ids) < 2:
            continue
        patterns.append(
            {
                "pattern_type": "action_text",
                "key": _safe_identifier(normalized_text),
                "label": normalized_text,
                "meeting_count": len(meeting_ids),
                "meeting_ids": tuple(sorted(meeting_ids)),
            }
        )
    for claim_id, meeting_ids in sorted(claim_meetings.items()):
        if len(meeting_ids) < 2:
            continue
        patterns.append(
            {
                "pattern_type": "claim",
                "key": claim_id,
                "meeting_count": len(meeting_ids),
                "meeting_ids": tuple(sorted(meeting_ids)),
            }
        )
    for risk_id, meeting_ids in sorted(risk_meetings.items()):
        if len(meeting_ids) < 2:
            continue
        patterns.append(
            {
                "pattern_type": "risk",
                "key": risk_id,
                "meeting_count": len(meeting_ids),
                "meeting_ids": tuple(sorted(meeting_ids)),
            }
        )
    for work_item_id, meeting_ids in sorted(item_meetings.items()):
        if len(meeting_ids) < 2:
            continue
        patterns.append(
            {
                "pattern_type": "work_item",
                "key": f"WI:{work_item_id}",
                "meeting_count": len(meeting_ids),
                "meeting_ids": tuple(sorted(meeting_ids)),
            }
        )
    return tuple(patterns)


@app.callback(invoke_without_command=True)
def actions_command(
    ctx: typer.Context,
    program: str | None = typer.Option(None, "--program", help="Program id, e.g. myprogram."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if program is None or not program.strip():
        raise typer.BadParameter("--program is required.")
    _print_actions(program.strip(), status_filter=None, format=format)
    raise typer.Exit(code=0)


@app.command("list")
def list_actions_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    status: str | None = typer.Option(None, "--status", help="Optional status filter."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    status_filter = ActionStatus.from_string(status) if status is not None and status.strip() else None
    _print_actions(program.strip(), status_filter=status_filter, format=format)
    raise typer.Exit(code=0)


@app.command("review")
def review_actions_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    reviewer: str | None = typer.Option(None, "--reviewer", help="Reviewer alias. Defaults to current OS user."),
    apply_ado: bool = typer.Option(
        False,
        "--apply-ado",
        help="Apply fully approved meeting-close ADO proposal batches after review.",
    ),
) -> None:
    program_id = program.strip()
    queue = [entry for entry in _sort_actions(_load_action_items(program_id)) if entry.status is ActionStatus.PROPOSED]
    if not queue:
        typer.echo(f"No proposed actions for {program_id}.")
        raise typer.Exit(code=0)

    meeting_close_batches = _meeting_close_batch_summaries(tuple(queue))
    meeting_close_patterns = _meeting_close_batch_patterns(tuple(queue))
    if meeting_close_batches:
        typer.echo(f"MEETING-CLOSE BATCHES — {len(meeting_close_batches)}")
        for batch in meeting_close_batches:
            status_summary = ", ".join(
                f"{status}={count}" for status, count in sorted(cast(dict[str, int], batch["status_counts"]).items())
            )
            due_label = batch["earliest_due_date"] or "-"
            typer.echo(
                f"- {batch['meeting_id']} | {batch['action_count']} actions | earliest due {due_label} | {status_summary}"
            )
    if meeting_close_patterns:
        typer.echo(f"MEETING-CLOSE PATTERNS — {len(meeting_close_patterns)}")
        for pattern in meeting_close_patterns:
            typer.echo(_format_meeting_close_pattern(pattern))

    review_actor = _default_actor(reviewer)
    reviewed = 0
    reviewed_statuses: dict[str, ActionStatus] = {}
    for action in queue:
        typer.echo("")
        typer.echo(f"{action.id} | due {_due_label(action)} | owner {action.owner_alias} | {action.workstream_id or '-'}")
        typer.echo(action.text)
        choice = typer.prompt("Decision [a]pprove/[c]ancel/[s]kip/[q]uit", default="s").strip().lower()
        if choice in {"q", "quit"}:
            break
        if choice in {"", "s", "skip"}:
            continue
        if choice not in {"a", "approve", "c", "cancel"}:
            raise typer.BadParameter("Review decision must be approve, cancel, skip, or quit.")
        new_status = ActionStatus.OPEN if choice in {"a", "approve"} else ActionStatus.CANCELLED
        note_value = typer.prompt("Note (optional)", default="", show_default=False).strip()
        update_action_status(
            program_id,
            action.id,
            new_status,
            note_value or None,
            updated_by=review_actor,
            programs_root=PROGRAMS_ROOT,
        )
        reviewed_statuses[action.id] = new_status
        reviewed += 1

    if apply_ado:
        for message in _apply_reviewed_meeting_close_proposals(
            program_id,
            queue=tuple(queue),
            reviewed_statuses=reviewed_statuses,
        ):
            typer.echo(message)

    typer.echo(f"Reviewed {reviewed} action(s) for {program_id}.")
    raise typer.Exit(code=0)


@app.command("resolve")
def resolve_action_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    action_id: str = typer.Option(..., "--id", help="Action id."),
    status: str = typer.Option("done", "--status", help="Resolution status: done|cancelled."),
    note: str | None = typer.Option(None, "--note", help="Optional resolution note."),
    resolver: str | None = typer.Option(None, "--resolver", help="Resolver alias. Defaults to current OS user."),
) -> None:
    status_value = ActionStatus.from_string(status)
    if status_value not in {ActionStatus.DONE, ActionStatus.CANCELLED}:
        raise typer.BadParameter("--status must be done or cancelled.")
    update_action_status(
        program.strip(),
        action_id.strip(),
        status_value,
        note,
        updated_by=_default_actor(resolver),
        programs_root=PROGRAMS_ROOT,
    )
    typer.echo(f"Resolved action {action_id.strip()} in {program.strip()} as {status_value.value}.")
    raise typer.Exit(code=0)


def _print_actions(program_id: str, *, status_filter: ActionStatus | None, format: str) -> None:
    entries = _load_action_items(program_id)
    if status_filter is not None:
        entries = tuple(entry for entry in entries if entry.status is status_filter)
    overdue_ids = {entry.id for entry in assess_action_staleness(entries, datetime.now(timezone.utc).date())}
    resolution_candidate_ids = load_action_resolution_candidate_ids(program_id, entries, programs_root=PROGRAMS_ROOT)
    sorted_entries = _sort_actions(entries)
    meeting_close_batches = _meeting_close_batch_summaries(sorted_entries)
    meeting_close_patterns = _meeting_close_batch_patterns(sorted_entries)
    if format == "json":
        typer.echo(
            json.dumps(
                {
                    "program_id": program_id,
                    "status_filter": status_filter.value if status_filter is not None else None,
                    "meeting_close_batches": list(meeting_close_batches),
                    "meeting_close_patterns": list(meeting_close_patterns),
                    "actions": [
                        asdict(entry)
                        | {
                            "staleness": ("overdue" if entry.id in overdue_ids else "current"),
                            "resolution_candidate": entry.id in resolution_candidate_ids,
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
        typer.echo(
            render_actions_csv(
                sorted_entries,
                meeting_close_batches=meeting_close_batches,
                meeting_close_patterns=meeting_close_patterns,
                program_id=program_id,
                overdue_ids=overdue_ids,
                resolution_candidate_ids=resolution_candidate_ids,
            ),
            nl=False,
        )
        return
    if format != "human":
        raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")
    if not sorted_entries:
        typer.echo(f"No actions found for {program_id}.")
        return

    typer.echo(f"ACTION REGISTER — {program_id} ({len(sorted_entries)})")
    if meeting_close_batches:
        typer.echo(f"MEETING-CLOSE BATCHES — {len(meeting_close_batches)}")
        for batch in meeting_close_batches:
            status_summary = ", ".join(
                f"{status}={count}" for status, count in sorted(cast(dict[str, int], batch["status_counts"]).items())
            )
            due_label = batch["earliest_due_date"] or "-"
            typer.echo(
                f"- {batch['meeting_id']} | {batch['action_count']} actions | earliest due {due_label} | {status_summary}"
            )
    if meeting_close_patterns:
        typer.echo(f"MEETING-CLOSE PATTERNS — {len(meeting_close_patterns)}")
        for pattern in meeting_close_patterns:
            typer.echo(_format_meeting_close_pattern(pattern))
    for entry in sorted_entries:
        overdue_label = "overdue" if entry.id in overdue_ids else "current"
        candidate_label = " | candidate for resolution" if entry.id in resolution_candidate_ids else ""
        typer.echo(
            f"- {entry.id} | {entry.status.value} | {overdue_label} | due {_due_label(entry)} | owner {entry.owner_alias}{candidate_label}"
        )
        typer.echo(f"  {entry.text}")


def _sort_actions(entries: tuple[ActionItem, ...]) -> tuple[ActionItem, ...]:
    active_order = {
        ActionStatus.PROPOSED: 0,
        ActionStatus.OPEN: 1,
        ActionStatus.IN_PROGRESS: 2,
        ActionStatus.DONE: 3,
        ActionStatus.CANCELLED: 4,
    }
    return tuple(
        sorted(
            entries,
            key=lambda entry: (
                active_order[entry.status],
                entry.due_date or datetime.max.date(),
                entry.created_at,
                entry.text.lower(),
            ),
        )
    )


def _default_actor(value: str | None) -> str:
    if value is not None and value.strip():
        return value.strip()
    return (os.environ.get("USERNAME") or os.environ.get("USER") or "manual").strip() or "manual"


def _apply_reviewed_meeting_close_proposals(
    program_id: str,
    *,
    queue: tuple[ActionItem, ...],
    reviewed_statuses: dict[str, ActionStatus],
) -> tuple[str, ...]:
    meeting_batches: dict[str, list[ActionItem]] = {}
    for action in queue:
        meeting_id = _meeting_close_source_meeting_id(action)
        if meeting_id is None:
            continue
        meeting_batches.setdefault(meeting_id, []).append(action)

    if not meeting_batches:
        return ()

    messages: list[str] = []
    reviewed_pattern_entries: list[ActionItem] = []
    total_actions = sum(len(actions) for actions in meeting_batches.values())
    applied_batches = 0
    skipped_batches = 0
    total_applied = 0
    total_skipped = 0
    total_conflict = 0
    total_failed = 0
    for meeting_id, actions in sorted(meeting_batches.items()):
        if not all(action.id in reviewed_statuses for action in actions):
            skipped_batches += 1
            messages.append(
                f"Skipped meeting-close ADO apply for {meeting_id}: pending review decisions remain."
            )
            continue
        reviewed_pattern_entries.extend(actions)
        if any(reviewed_statuses[action.id] is not ActionStatus.OPEN for action in actions):
            skipped_batches += 1
            messages.append(
                f"Skipped meeting-close ADO apply for {meeting_id}: only fully approved batches can be applied."
            )
            continue
        proposal_reference = f"meeting-action-{_safe_identifier(meeting_id)}"
        artifacts = apply_ado_proposal(
            proposal_reference,
            programs_root=PROGRAMS_ROOT,
        )
        applied_batches += 1
        total_applied += artifacts.applied_count
        total_skipped += artifacts.skipped_count
        total_conflict += artifacts.conflict_count
        total_failed += artifacts.failed_count
        messages.append(
            f"Applied meeting-close proposal {proposal_reference}: "
            f"{artifacts.applied_count} applied | "
            f"{artifacts.skipped_count} skipped | "
            f"{artifacts.conflict_count} conflict | "
            f"{artifacts.failed_count} failed"
        )
    messages.append(
        f"Meeting-close batch summary: {len(meeting_batches)} meetings | {total_actions} actions | "
        f"{applied_batches} applied batch{'es' if applied_batches != 1 else ''} | "
        f"{skipped_batches} skipped batch{'es' if skipped_batches != 1 else ''} | "
        f"{total_applied} applied | {total_skipped} skipped | {total_conflict} conflict | {total_failed} failed"
    )
    patterns = _meeting_close_batch_patterns(tuple(reviewed_pattern_entries))
    if patterns:
        messages.append(f"Meeting-close pattern summary: {len(patterns)} recurring cross-meeting patterns")
        messages.extend(_format_meeting_close_pattern(pattern) for pattern in patterns)
    return tuple(messages)


def _meeting_close_source_meeting_id(action: ActionItem) -> str | None:
    source_signal_id = (action.source_signal_id or "").strip()
    if not source_signal_id.startswith("meeting-close:"):
        return None
    parts = source_signal_id.split(":", 2)
    if len(parts) != 3:
        return None
    meeting_id = parts[2].strip()
    return meeting_id or None


def _safe_identifier(value: str) -> str:
    normalized = value.strip().lower()
    safe = "".join(character if character.isalnum() else "-" for character in normalized)
    while "--" in safe:
        safe = safe.replace("--", "-")
    safe = safe.strip("-")
    return safe or "meeting"


def _due_label(action: ActionItem) -> str:
    return action.due_date.isoformat() if action.due_date is not None else "-"


def _normalize_action_pattern_text(value: str) -> str:
    collapsed = " ".join(value.strip().lower().split())
    normalized = "".join(character if character.isalnum() or character.isspace() else " " for character in collapsed)
    return " ".join(normalized.split())


def _format_meeting_close_pattern(pattern: dict[str, object]) -> str:
    pattern_label = str(pattern.get("label") or pattern["key"])
    return (
        f"- repeated {pattern['pattern_type']} {pattern_label} across {pattern['meeting_count']} meetings | "
        f"{', '.join(cast(tuple[str, ...], pattern['meeting_ids']))}"
    )


def render_actions_csv(
    entries: tuple[ActionItem, ...],
    *,
    meeting_close_batches: tuple[dict[str, object], ...],
    meeting_close_patterns: tuple[dict[str, object], ...],
    program_id: str,
    overdue_ids: set[str],
    resolution_candidate_ids: frozenset[str],
) -> str:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        (
            "id",
            "program_id",
            "status",
            "staleness",
            "resolution_candidate",
            "due_date",
            "owner_alias",
            "workstream_id",
            "created_at",
            "resolved_at",
            "source_type",
            "source_signal_id",
            "linked_work_item_ids",
            "linked_claim_id",
            "linked_risk_id",
            "text",
            "resolution_note",
        )
    )
    for entry in entries:
        writer.writerow(
            (
                entry.id,
                entry.program_id,
                entry.status.value,
                "overdue" if entry.id in overdue_ids else "current",
                "true" if entry.id in resolution_candidate_ids else "false",
                _csv_datetime(entry.due_date),
                entry.owner_alias,
                entry.workstream_id or "",
                entry.created_at.isoformat(),
                entry.resolved_at.isoformat() if entry.resolved_at is not None else "",
                entry.source_type.value,
                entry.source_signal_id or "",
                "|".join(str(item_id) for item_id in entry.linked_work_item_ids),
                entry.linked_claim_id or "",
                entry.linked_risk_id or "",
                entry.text,
                entry.resolution_note or "",
            )
        )
    for batch in meeting_close_batches:
        status_summary = ", ".join(
            f"{status}={count}" for status, count in sorted(cast(dict[str, int], batch["status_counts"]).items())
        )
        writer.writerow(
            (
                f"meeting-batch:{batch['meeting_id']}",
                program_id,
                "summary",
                "",
                "false",
                batch["earliest_due_date"] or "",
                "",
                "",
                "",
                "",
                "meeting_close_batch",
                f"meeting-close:{program_id}:{batch['meeting_id']}",
                "",
                "",
                "",
                f"{batch['action_count']} actions | {status_summary}",
                "",
            )
        )
    for pattern in meeting_close_patterns:
        pattern_label = str(pattern.get("label") or pattern["key"])
        writer.writerow(
            (
                f"meeting-pattern:{pattern['pattern_type']}:{pattern['key']}",
                program_id,
                "summary",
                "",
                "false",
                "",
                "",
                "",
                "",
                "",
                "meeting_close_pattern",
                "",
                "",
                "",
                "",
                f"repeated {pattern['pattern_type']} {pattern_label} across {pattern['meeting_count']} meetings | {', '.join(cast(tuple[str, ...], pattern['meeting_ids']))}",
                "",
            )
        )
    return buffer.getvalue()


def _csv_datetime(value: date | None) -> str:
    if value is None:
        return ""
    return value.isoformat()


def _json_default(value: object) -> object:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")