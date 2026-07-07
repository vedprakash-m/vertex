from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
from typing import Callable
import typer

from src.commands import gather as gather_helpers
from src.core.exceptions import ConfigError
from src.core.journal import PROGRAMS_ROOT
from src.core.milestone_engine import (
    assess_milestone_health,
    build_critical_path,
    describe_milestone_schedule_variance,
    load_milestone_completion_date_history_map,
    load_milestone_target_date_history_map,
    save_milestones,
    summarize_milestone_completion_date_history,
    summarize_milestone_target_date_history,
)
from src.core.models import WorkItem
from src.core.models_v2 import Milestone, MilestoneAssessment, MilestoneStatus, Program, Workstream
from src.core.program_fact_store import load_program_facts, project_dependencies, project_milestones
from src.core.store_factory import build_trajectory_store_for_program_id


ProgramLoader = Callable[[str, Path], tuple[Program, tuple[Workstream, ...]]]
ItemLoader = Callable[[Program, tuple[Workstream, ...], datetime], tuple[tuple[WorkItem, ...], int]]


@dataclass(frozen=True, slots=True)
class MilestoneAssessmentRow:
    id: str
    name: str
    declared_status: str
    computed_health: str
    schedule_summary: str | None
    completion_date: str | None
    completion_date_history: tuple[str, ...]
    target_date_history: tuple[str, ...]
    target_date: str
    owner_alias: str
    critical_path: bool
    confidence: str
    slip_probability: float
    linked_work_item_count: int
    blocked_criteria: tuple[str, ...]
    reasoning: str


@dataclass(frozen=True, slots=True)
class MilestoneAssessmentReport:
    program_id: str
    generated_at: datetime
    ado_calls: int
    critical_path_ids: tuple[str, ...]
    rows: tuple[MilestoneAssessmentRow, ...]

    def to_payload(self) -> dict[str, object]:
        payload = asdict(self)
        payload["generated_at"] = self.generated_at.isoformat()
        return payload


app = typer.Typer(help="Manage milestone health and authored milestone data.", invoke_without_command=True)


@app.callback(invoke_without_command=True)
def milestones_command(
    ctx: typer.Context,
    program: str | None = typer.Option(None, "--program", help="Program id, e.g. myprogram."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if program is None or not program.strip():
        raise typer.BadParameter("--program is required.")
    _emit_milestone_list(program.strip(), format=format)
    raise typer.Exit(code=0)


@app.command("list")
def list_milestones_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    _emit_milestone_list(program.strip(), format=format)
    raise typer.Exit(code=0)


@app.command("assess")
def assess_milestones_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
    as_of: str | None = typer.Option(None, "--as-of", help="Optional YYYY-MM-DD override for assessment time."),
) -> None:
    try:
        report = build_milestone_assessment_report(
            program.strip(),
            as_of=_parse_as_of(as_of),
            programs_root=PROGRAMS_ROOT,
        )
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error

    if format == "json":
        typer.echo(json.dumps(report.to_payload(), indent=2, sort_keys=True))
        raise typer.Exit(code=0)
    if format == "csv":
        typer.echo(render_milestone_assessment_csv(report), nl=False)
        raise typer.Exit(code=0)
    if format != "human":
        raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")

    typer.echo(render_milestone_assessment_report(report))
    raise typer.Exit(code=0)


@app.command("update")
def update_milestone_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    milestone_id: str = typer.Option(..., "--id", help="Milestone id."),
    status: str | None = typer.Option(None, "--status", help="Optional new status."),
    target_date: str | None = typer.Option(None, "--target-date", help="Optional new YYYY-MM-DD target date."),
    owner: str | None = typer.Option(None, "--owner", help="Optional new owner alias."),
    name: str | None = typer.Option(None, "--name", help="Optional new milestone name."),
    notes: str | None = typer.Option(None, "--notes", help="Optional new notes text."),
    clear_notes: bool = typer.Option(False, "--clear-notes", help="Clear milestone notes."),
) -> None:
    if notes is not None and clear_notes:
        raise typer.BadParameter("Choose only one of --notes or --clear-notes.")

    program_id = program.strip()
    milestones = list(_load_current_milestones(program_id, programs_root=PROGRAMS_ROOT))
    match_index = next((index for index, entry in enumerate(milestones) if entry.id == milestone_id.strip()), None)
    if match_index is None:
        raise typer.BadParameter(f"Milestone '{milestone_id}' was not found in {program_id}.")

    current = milestones[match_index]
    next_status = MilestoneStatus.from_string(status) if status is not None and status.strip() else current.status
    next_target_date = _parse_optional_date(target_date, option_name="--target-date") if target_date is not None else current.target_date
    next_owner = owner.strip() if owner is not None and owner.strip() else current.owner_alias
    next_name = name.strip() if name is not None and name.strip() else current.name
    next_notes = _resolve_notes_value(notes, clear_notes=clear_notes, current=current.notes)

    updated = replace(
        current,
        name=next_name,
        target_date=next_target_date,
        owner_alias=next_owner,
        status=next_status,
        notes=next_notes,
    )
    if updated == current:
        typer.echo(f"Milestone {current.id} is unchanged.")
        raise typer.Exit(code=0)

    milestones[match_index] = updated
    save_milestones(program_id, _sort_milestones(tuple(milestones)), programs_root=PROGRAMS_ROOT)
    typer.echo(f"Updated milestone {updated.id} in {program_id}.")
    raise typer.Exit(code=0)


def build_milestone_assessment_report(
    program_id: str,
    *,
    as_of: datetime | None = None,
    programs_root: Path | None = None,
    program_loader: ProgramLoader | None = None,
    item_loader: ItemLoader | None = None,
) -> MilestoneAssessmentReport:
    resolved_programs_root = programs_root or PROGRAMS_ROOT
    current_time = as_of or datetime.now(timezone.utc)
    fact_snapshot = load_program_facts(
        program_id,
        programs_root=resolved_programs_root,
        fact_types=("milestone.entry", "dependency.link"),
    )
    milestones = _sort_milestones(project_milestones(fact_snapshot))
    if not milestones:
        return MilestoneAssessmentReport(
            program_id=program_id,
            generated_at=current_time,
            ado_calls=0,
            critical_path_ids=(),
            rows=(),
        )

    program, workstreams = (program_loader or _load_program_context)(program_id, resolved_programs_root)
    items, ado_calls = (item_loader or _load_live_items)(program, workstreams, current_time)
    dependencies = project_dependencies(fact_snapshot)
    critical_path_ids = tuple(
        milestone.id for milestone in build_critical_path(milestones, dependencies)
    )
    trajectory_map = _load_trajectory_map(
        program_id=program_id,
        milestones=milestones,
        programs_root=resolved_programs_root,
    )
    critical_path_id_set = set(critical_path_ids)
    assessments = tuple(
        replace(
            assess_milestone_health(milestone, items, trajectory_map, current_time),
            critical_path=milestone.id in critical_path_id_set,
        )
        for milestone in milestones
    )
    target_date_history = load_milestone_target_date_history_map(
        program_id,
        milestones,
        programs_root=resolved_programs_root,
    )
    completion_date_history = load_milestone_completion_date_history_map(
        program_id,
        milestones,
        current_completion_dates={assessment.milestone_id: assessment.completion_date for assessment in assessments},
        programs_root=resolved_programs_root,
    )
    rows = tuple(
        _build_assessment_row(
            milestone,
            assessment=assessment,
            schedule_summary=describe_milestone_schedule_variance(milestone, items, trajectory_map, current_time),
            completion_date_history=completion_date_history.get(milestone.id, ()),
            target_date_history=target_date_history.get(milestone.id, ()),
        )
        for milestone, assessment in zip(milestones, assessments, strict=False)
    )
    return MilestoneAssessmentReport(
        program_id=program_id,
        generated_at=current_time,
        ado_calls=ado_calls,
        critical_path_ids=critical_path_ids,
        rows=rows,
    )


def render_milestone_assessment_report(report: MilestoneAssessmentReport) -> str:
    if not report.rows:
        return f"No milestones found for {report.program_id}."

    critical_path_label = ", ".join(report.critical_path_ids) if report.critical_path_ids else "none"
    lines = [
        f"MILESTONE HEALTH — {report.program_id} ({len(report.rows)})",
        f"ADO calls: {report.ado_calls}",
        f"Critical path: {critical_path_label}",
    ]
    for row in report.rows:
        path_label = "critical path" if row.critical_path else "non-critical"
        lines.append(
            f"- {row.id} | declared {row.declared_status} | computed {row.computed_health} | target {row.target_date} | {path_label} | slip {row.slip_probability:.2f} | confidence {row.confidence}"
        )
        lines.append(f"  {row.name}")
        if row.schedule_summary:
            lines.append(f"  Schedule: {row.schedule_summary}")
        if row.completion_date:
            lines.append(f"  Completion date: {row.completion_date}")
        completion_history_summary = summarize_milestone_completion_date_history(row.completion_date_history)
        if completion_history_summary:
            lines.append(f"  {completion_history_summary}")
        history_summary = summarize_milestone_target_date_history(row.target_date_history)
        if history_summary:
            lines.append(f"  {history_summary}")
        lines.append(f"  {row.reasoning}")
        if row.blocked_criteria:
            blocked_detail = row.blocked_criteria[0]
            if len(row.blocked_criteria) > 1:
                blocked_detail = f"{blocked_detail}; +{len(row.blocked_criteria) - 1} more"
            lines.append(f"  Blockers: {blocked_detail}")
    return "\n".join(lines)


def render_milestone_assessment_csv(report: MilestoneAssessmentReport) -> str:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        (
            "program_id",
            "generated_at",
            "ado_calls",
            "id",
            "name",
            "declared_status",
            "computed_health",
            "target_date",
            "owner_alias",
            "critical_path",
            "confidence",
            "slip_probability",
            "linked_work_item_count",
            "schedule_summary",
            "completion_date",
            "completion_date_history",
            "target_date_history",
            "blocked_criteria",
            "reasoning",
        )
    )
    for row in report.rows:
        writer.writerow(
            (
                report.program_id,
                report.generated_at.isoformat(),
                report.ado_calls,
                row.id,
                row.name,
                row.declared_status,
                row.computed_health,
                row.target_date,
                row.owner_alias,
                "true" if row.critical_path else "false",
                row.confidence,
                row.slip_probability,
                row.linked_work_item_count,
                row.schedule_summary or "",
                row.completion_date or "",
                "|".join(row.completion_date_history),
                "|".join(row.target_date_history),
                "|".join(row.blocked_criteria),
                row.reasoning,
            )
        )
    return buffer.getvalue()


def _emit_milestone_list(program_id: str, *, format: str) -> None:
    milestones = _load_current_milestones(program_id, programs_root=PROGRAMS_ROOT)
    if format == "json":
        typer.echo(
            json.dumps(
                {
                    "program_id": program_id,
                    "milestones": [
                        {
                            "id": milestone.id,
                            "name": milestone.name,
                            "target_date": milestone.target_date.isoformat(),
                            "owner_alias": milestone.owner_alias,
                            "status": milestone.status.value,
                            "exit_criteria": list(milestone.exit_criteria),
                            "linked_workstream_ids": list(milestone.linked_workstream_ids),
                            "linked_work_item_ids": list(milestone.linked_work_item_ids),
                            "notes": milestone.notes,
                        }
                        for milestone in milestones
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    if format == "csv":
        typer.echo(render_milestone_list_csv(program_id, milestones), nl=False)
        return
    if format != "human":
        raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")
    if not milestones:
        typer.echo(f"No milestones found for {program_id}.")
        return

    typer.echo(f"MILESTONES — {program_id} ({len(milestones)})")
    for milestone in milestones:
        typer.echo(
            f"- {milestone.id} | {milestone.status.value} | target {milestone.target_date.isoformat()} | owner {milestone.owner_alias} | criteria {len(milestone.exit_criteria)} | linked items {len(milestone.linked_work_item_ids)}"
        )
        typer.echo(f"  {milestone.name}")


def render_milestone_list_csv(program_id: str, milestones: tuple[Milestone, ...]) -> str:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        (
            "program_id",
            "id",
            "name",
            "target_date",
            "owner_alias",
            "status",
            "exit_criteria",
            "linked_workstream_ids",
            "linked_work_item_ids",
            "notes",
        )
    )
    for milestone in milestones:
        writer.writerow(
            (
                program_id,
                milestone.id,
                milestone.name,
                milestone.target_date.isoformat(),
                milestone.owner_alias,
                milestone.status.value,
                "|".join(milestone.exit_criteria),
                "|".join(milestone.linked_workstream_ids),
                "|".join(str(item_id) for item_id in milestone.linked_work_item_ids),
                milestone.notes or "",
            )
        )
    return buffer.getvalue()


def _load_current_milestones(program_id: str, *, programs_root: Path) -> tuple[Milestone, ...]:
    return _sort_milestones(
        project_milestones(
            load_program_facts(
                program_id,
                programs_root=programs_root,
                fact_types=("milestone.entry",),
            )
        )
    )


def _build_assessment_row(
    milestone: Milestone,
    *,
    assessment: MilestoneAssessment,
    schedule_summary: str | None,
    completion_date_history: tuple[str, ...],
    target_date_history: tuple[str, ...],
) -> MilestoneAssessmentRow:
    return MilestoneAssessmentRow(
        id=milestone.id,
        name=milestone.name,
        declared_status=milestone.status.value,
        computed_health=assessment.computed_health.value,
        schedule_summary=schedule_summary,
        completion_date=(assessment.completion_date.isoformat() if assessment.completion_date is not None else None),
        completion_date_history=completion_date_history,
        target_date_history=target_date_history,
        target_date=milestone.target_date.isoformat(),
        owner_alias=milestone.owner_alias,
        critical_path=assessment.critical_path,
        confidence=assessment.confidence.value,
        slip_probability=assessment.slip_probability,
        linked_work_item_count=len(milestone.linked_work_item_ids),
        blocked_criteria=assessment.blocked_criteria,
        reasoning=assessment.reasoning,
    )


def _load_program_context(program_id: str, programs_root: Path) -> tuple[Program, tuple[Workstream, ...]]:
    return gather_helpers._load_program_context(program_id, programs_root)


def _load_live_items(
    program: Program,
    workstreams: tuple[Workstream, ...],
    as_of: datetime,
) -> tuple[tuple[WorkItem, ...], int]:
    items, _, ado_calls = gather_helpers._load_ado_items_via_uil(
        program, workstreams, as_of,
        since=as_of - timedelta(days=program.ado.date_window_days if program.ado else 90),
        programs_root=PROGRAMS_ROOT,
    )
    return items, ado_calls


def _load_trajectory_map(
    *,
    program_id: str,
    milestones: tuple[Milestone, ...],
    programs_root: Path,
) -> dict[int, tuple]:
    item_ids = {item_id for milestone in milestones for item_id in milestone.linked_work_item_ids}
    trajectory_store = build_trajectory_store_for_program_id(
        program_id,
        programs_root=programs_root,
    )
    return {
        item_id: trajectory_store.read(program_id, item_id)
        for item_id in item_ids
    }


def _sort_milestones(milestones: tuple[Milestone, ...]) -> tuple[Milestone, ...]:
    return tuple(sorted(milestones, key=lambda milestone: (milestone.target_date, milestone.name.lower(), milestone.id)))


def _parse_optional_date(value: str, *, option_name: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError as error:
        raise typer.BadParameter(f"{option_name} must be YYYY-MM-DD.") from error


def _parse_as_of(value: str | None) -> datetime | None:
    if value is None or not value.strip():
        return None
    parsed = _parse_optional_date(value, option_name="--as-of")
    return datetime(parsed.year, parsed.month, parsed.day, 12, 0, tzinfo=timezone.utc)


def _resolve_notes_value(notes: str | None, *, clear_notes: bool, current: str | None) -> str | None:
    if clear_notes:
        return None
    if notes is None:
        return current
    normalized = notes.strip()
    if not normalized:
        raise typer.BadParameter("Use --clear-notes to clear milestone notes.")
    return normalized