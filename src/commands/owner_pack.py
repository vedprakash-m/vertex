from __future__ import annotations

from collections.abc import Callable
import csv
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path

import typer

from src.commands import gather as gather_helpers
from src.commands import report as report_helpers
from src.commands.vitality import generate_vitality_report
from src.core.ado_client import ADOClient
from src.core.action_tracker import load_action_resolution_candidate_ids, load_actions
from src.core.assumption_tracker import load_assumptions
from src.core.claim_tracker import load_open_decision_asks
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.feedback.calibration_router import load_forecast_calibration_dri_profile
from src.core.milestone_engine import (
    assess_milestone_health,
    describe_milestone_schedule_variance,
    load_milestones,
    load_milestone_completion_date_history_map,
    load_milestone_target_date_history_map,
    summarize_milestone_completion_date_history,
    summarize_milestone_target_date_history,
)
from src.core.models import WorkItem
from src.core.models_v2 import ActionStatus, Program, VitalityAggregate, Workstream
from src.core.owner_pack import OwnerPack, OwnerPackCalibrationSummary, OwnerPackMilestoneContribution, OwnerPackVitalitySummary, build_owner_pack, write_owner_pack
from src.core.program_fact_store import load_program_facts, project_action_items, project_assumptions, project_milestones, project_risk_entries
from src.core.query_builder import ODataFilter
from src.core.store_factory import build_trajectory_store_for_program_id
from src.core.telemetry_summary import build_program_telemetry_summary


ProgramLoader = Callable[[str, Path], tuple[Program, tuple[Workstream, ...]]]
ItemLoader = Callable[[ADOClient | None, Program, datetime], tuple[tuple[WorkItem, ...], int]]
_DEFAULT_LOAD_ASSUMPTIONS = load_assumptions
_DEFAULT_LOAD_MILESTONES = load_milestones


def _load_current_assumptions(program_id: str, *, programs_root: Path):
    assumption_loader = load_assumptions
    if assumption_loader is not _DEFAULT_LOAD_ASSUMPTIONS:
        return tuple(assumption_loader(program_id, programs_root=programs_root))
    return project_assumptions(
        load_program_facts(
            program_id,
            db_root=programs_root.parent,
            programs_root=programs_root,
            fact_types=("assumption.entry",),
        )
    )


def _load_current_milestones(program_id: str, *, programs_root: Path):
    milestone_loader = load_milestones
    if milestone_loader is not _DEFAULT_LOAD_MILESTONES:
        return tuple(milestone_loader(program_id, programs_root=programs_root))
    return project_milestones(
        load_program_facts(
            program_id,
            db_root=programs_root.parent,
            programs_root=programs_root,
            fact_types=("milestone.entry",),
        )
    )


@dataclass(frozen=True, slots=True)
class OwnerPackArtifacts:
    pack: OwnerPack
    output_path: Path
    ado_calls: int


def owner_pack_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    owner: str = typer.Option(..., "--owner", help="Owner alias, for example priya."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    artifacts = generate_owner_pack(program, owner)
    if format == "human":
        typer.echo(f"Owner pack written: {artifacts.output_path}")
        typer.echo(
            f"Items: {len(artifacts.pack.items)} | Open risks: {len(artifacts.pack.open_risks)} | Risk register entries: {len(artifacts.pack.risk_register_entries)} | Milestone contributions: {len(artifacts.pack.milestone_contributions)} | Stale items: {len(artifacts.pack.stale_items)} | Open actions: {len(artifacts.pack.open_actions)} | Open assumptions: {len(artifacts.pack.open_assumptions)} | Open asks: {len(artifacts.pack.open_decision_asks)} | Proposed ADO updates: {len(artifacts.pack.proposal_entries)}"
        )
    else:
        typer.echo(
            render_owner_pack_output(
                _build_owner_pack_payload(artifacts),
                format=format,
            ),
            nl=False,
        )
    raise typer.Exit(code=0)


def _build_owner_pack_payload(artifacts: OwnerPackArtifacts) -> dict[str, object]:
    pack = artifacts.pack
    return {
        "ado_calls": artifacts.ado_calls,
        "counts": {
            "calibration": 0 if pack.calibration_summary is None else 1,
            "items": len(pack.items),
            "milestone_contributions": len(pack.milestone_contributions),
            "open_actions": len(pack.open_actions),
            "open_assumptions": len(pack.open_assumptions),
            "open_decision_asks": len(pack.open_decision_asks),
            "open_risks": len(pack.open_risks),
            "proposal_entries": len(pack.proposal_entries),
            "risk_register_entries": len(pack.risk_register_entries),
            "stale_items": len(pack.stale_items),
            "telemetry": 0 if pack.telemetry_summary is None else 1,
        },
        "calibration_summary": None if pack.calibration_summary is None else {
            "claim_accuracy": pack.calibration_summary.claim_accuracy,
            "contradicted": pack.calibration_summary.contradicted,
            "met": pack.calibration_summary.met,
            "owner_alias": pack.calibration_summary.owner_alias,
            "sample_size": pack.calibration_summary.sample_size,
            "slip_modifier": pack.calibration_summary.slip_modifier,
            "stale": pack.calibration_summary.stale,
        },
        "generated_at": pack.generated_at.isoformat(),
        "items": [
            {
                "id": item.id,
                "risk_level": item.risk_level.value,
                "state": item.state,
                "target_date": item.target_date.isoformat() if item.target_date is not None else None,
                "title": item.title,
            }
            for item in pack.items
        ],
        "milestone_contributions": [
            {
                "computed_status": contribution.computed_status,
                "completion_history_summary": contribution.completion_history_summary,
                "milestone_id": contribution.milestone_id,
                "name": contribution.name,
                "relation": contribution.relation,
                "schedule_summary": contribution.schedule_summary,
                "status": contribution.status,
                "target_date": contribution.target_date.isoformat(),
                "target_history_summary": contribution.target_history_summary,
            }
            for contribution in pack.milestone_contributions
        ],
        "open_actions": [
            {
                "due_date": action.due_date.isoformat() if action.due_date is not None else None,
                "id": action.id,
                "is_resolution_candidate": action.id in pack.resolution_candidate_action_ids,
                "status": action.status.value,
                "text": action.text,
            }
            for action in pack.open_actions
        ],
        "open_assumptions": [
            {
                "id": assumption.id,
                "is_overdue": assumption.id in pack.overdue_assumption_ids,
                "status": assumption.status.value,
                "text": assumption.text,
                "validation_due": assumption.validation_due.isoformat() if assumption.validation_due is not None else None,
            }
            for assumption in pack.open_assumptions
        ],
        "open_decision_asks": [
            {
                "ask_date": ask.ask_date.isoformat(),
                "id": ask.id,
                "issue_number": ask.issue_number,
                "text": ask.text,
            }
            for ask in pack.open_decision_asks
        ],
        "open_risks": [
            {
                "id": item.id,
                "risk_level": item.risk_level.value,
                "state": item.state,
                "title": item.title,
            }
            for item in pack.open_risks
        ],
        "output_path": str(artifacts.output_path),
        "owner_alias": pack.owner_alias,
        "program_id": pack.program_id,
        "proposal_entries": [
            {
                "action": entry.action,
                "entry_status": entry.entry_status,
                "field_or_tag": entry.field_or_tag,
                "proposal_id": entry.proposal_id,
                "proposal_status": entry.proposal_status,
                "work_item_id": entry.work_item_id,
            }
            for entry in pack.proposal_entries
        ],
        "risk_register_entries": [
            {
                "id": entry.id,
                "owner_alias": entry.owner_alias,
                "score": entry.probability.value + ":" + entry.impact.value,
                "status": entry.status.value,
                "title": entry.title,
            }
            for entry in pack.risk_register_entries
        ],
        "stale_items": [
            {
                "id": item.id,
                "risk_level": item.risk_level.value,
                "state": item.state,
                "title": item.title,
            }
            for item in pack.stale_items
        ],
        "telemetry_summary": pack.telemetry_summary,
        "vitality_summary": None if pack.vitality_summary is None else {
            "avg_richness": pack.vitality_summary.avg_richness,
            "composite_score": pack.vitality_summary.composite_score,
            "fresh_items": pack.vitality_summary.fresh_items,
            "total_items": pack.vitality_summary.total_items,
            "total_leakage": pack.vitality_summary.total_leakage,
            "workiq_signal_count": pack.vitality_summary.workiq_signal_count,
        },
    }


def render_owner_pack_output(payload: dict[str, object], *, format: str) -> str:
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        buffer = StringIO()
        writer = csv.writer(buffer)
        columns = (
            "entry_type",
            "program_id",
            "owner_alias",
            "output_path",
            "ado_calls",
            "ref_id",
            "item_id",
            "title_or_text",
            "status",
            "risk_level",
            "target_date",
            "due_date",
            "detail",
        )
        writer.writerow(columns)
        writer.writerow(
            [
                "summary",
                payload["program_id"],
                payload["owner_alias"],
                payload["output_path"],
                payload["ado_calls"],
                None,
                None,
                None,
                payload["counts"],
                None,
                None,
                None,
                None,
            ]
        )
        calibration_summary = payload.get("calibration_summary")
        if isinstance(calibration_summary, dict):
            writer.writerow([
                "calibration",
                payload["program_id"],
                payload["owner_alias"],
                payload["output_path"],
                payload["ado_calls"],
                calibration_summary.get("owner_alias"),
                None,
                None,
                None,
                None,
                None,
                None,
                f"accuracy={_format_percent(calibration_summary.get('claim_accuracy'))}, sample={calibration_summary.get('sample_size')}, slip_modifier=+{float(calibration_summary.get('slip_modifier') or 0.0):.2f}",
            ])
        for item in payload["items"]:  # type: ignore[attr-defined]
            writer.writerow([
                "item",
                payload["program_id"],
                payload["owner_alias"],
                payload["output_path"],
                payload["ado_calls"],
                item["id"],
                item["id"],
                item["title"],
                item["state"],
                item["risk_level"],
                item["target_date"],
                None,
                None,
            ])
        for entry in payload["risk_register_entries"]:  # type: ignore[attr-defined]
            writer.writerow([
                "risk_register",
                payload["program_id"],
                payload["owner_alias"],
                payload["output_path"],
                payload["ado_calls"],
                entry["id"],
                None,
                entry["title"],
                entry["status"],
                None,
                None,
                None,
                entry["score"],
            ])
        for entry in payload["milestone_contributions"]:  # type: ignore[attr-defined]
            writer.writerow([
                "milestone",
                payload["program_id"],
                payload["owner_alias"],
                payload["output_path"],
                payload["ado_calls"],
                entry["milestone_id"],
                None,
                entry["name"],
                entry["status"],
                None,
                entry["target_date"],
                None,
                entry["schedule_summary"] or entry["target_history_summary"] or entry["completion_history_summary"],
            ])
        if payload["telemetry_summary"]:
            writer.writerow([
                "telemetry",
                payload["program_id"],
                payload["owner_alias"],
                payload["output_path"],
                payload["ado_calls"],
                None,
                None,
                payload["telemetry_summary"],
                None,
                None,
                None,
                None,
                None,
            ])
        for entry in payload["open_actions"]:  # type: ignore[attr-defined]
            writer.writerow([
                "action",
                payload["program_id"],
                payload["owner_alias"],
                payload["output_path"],
                payload["ado_calls"],
                entry["id"],
                None,
                entry["text"],
                entry["status"],
                None,
                None,
                entry["due_date"],
                "resolution_candidate" if entry["is_resolution_candidate"] else None,
            ])
        for entry in payload["open_assumptions"]:  # type: ignore[attr-defined]
            writer.writerow([
                "assumption",
                payload["program_id"],
                payload["owner_alias"],
                payload["output_path"],
                payload["ado_calls"],
                entry["id"],
                None,
                entry["text"],
                entry["status"],
                None,
                None,
                entry["validation_due"],
                "overdue" if entry["is_overdue"] else None,
            ])
        for entry in payload["open_decision_asks"]:  # type: ignore[attr-defined]
            writer.writerow([
                "decision_ask",
                payload["program_id"],
                payload["owner_alias"],
                payload["output_path"],
                payload["ado_calls"],
                entry["id"],
                None,
                entry["text"],
                None,
                None,
                entry["ask_date"],
                None,
                entry["issue_number"],
            ])
        for entry in payload["proposal_entries"]:  # type: ignore[attr-defined]
            writer.writerow([
                "proposal",
                payload["program_id"],
                payload["owner_alias"],
                payload["output_path"],
                payload["ado_calls"],
                entry["proposal_id"],
                entry["work_item_id"],
                entry["action"],
                entry["entry_status"],
                None,
                None,
                None,
                entry["proposal_status"],
            ])
        return buffer.getvalue()
    if format != "human":
        raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")
    raise typer.BadParameter("Human owner-pack output is rendered directly by the command.")


def generate_owner_pack(
    program_id: str,
    owner_alias: str,
    *,
    as_of: datetime | None = None,
    programs_root: Path | None = None,
    output_root: Path | None = None,
    program_loader: ProgramLoader | None = None,
    item_loader: ItemLoader | None = None,
) -> OwnerPackArtifacts:
    resolved_programs_root = programs_root or PROGRAMS_ROOT
    current_time = as_of or datetime.now(timezone.utc)
    program, workstreams = (program_loader or gather_helpers._load_program_context)(program_id, resolved_programs_root)
    if program.ado is None:
        raise typer.BadParameter(f"Program '{program_id}' is missing ado configuration.")

    resolved_item_loader = item_loader or _load_owner_items
    client: ADOClient | None = None
    if item_loader is None and resolved_item_loader is _load_owner_items:
        client = ADOClient(
            organization=program.ado.organization,
            project=program.ado.project,
            timeout=program.ado.api_timeout_seconds,
        )
    items, ado_calls = resolved_item_loader(client, program, current_time)
    raci_workstream_ids = _load_raci_workstream_scope_ids(
        owner_alias=owner_alias,
        workstreams=workstreams,
    )
    scoped_item_ids = _resolve_scoped_item_ids(
        items=items,
        workstreams=workstreams,
        scoped_workstream_ids=raci_workstream_ids,
    )
    vitality_artifacts = generate_vitality_report(
        program_id,
        as_of=current_time,
        programs_root=resolved_programs_root,
        owner_alias=owner_alias,
    )
    program_facts = load_program_facts(program_id, db_root=resolved_programs_root.parent, programs_root=resolved_programs_root)
    open_actions = tuple(
        action
        for action in project_action_items(program_facts)
        if action.status in {ActionStatus.OPEN, ActionStatus.IN_PROGRESS}
    )
    milestones = _load_current_milestones(program_id, programs_root=resolved_programs_root)
    pack = build_owner_pack(
        program_id=program_id,
        owner_alias=owner_alias,
        items=items,
        risk_register_entries=project_risk_entries(program_facts),
        milestones=milestones,
        open_actions=open_actions,
        assumptions=_load_current_assumptions(program_id, programs_root=resolved_programs_root),
        resolution_candidate_action_ids=load_action_resolution_candidate_ids(
            program_id,
            open_actions,
            programs_root=resolved_programs_root,
        ),
        open_decision_asks=load_open_decision_asks(program_id, programs_root=resolved_programs_root),
        scoped_workstream_ids=raci_workstream_ids,
        scoped_item_ids=scoped_item_ids,
        generated_at=current_time,
        vitality_summary=_owner_vitality_summary(vitality_artifacts.owner_aggregates),
        telemetry_summary=build_program_telemetry_summary(
            program_id,
            programs_root=resolved_programs_root,
            as_of=current_time,
        ),
    )
    pack = replace(
        pack,
        calibration_summary=_load_owner_calibration_summary(
            program_id=program_id,
            owner_alias=owner_alias,
            programs_root=resolved_programs_root,
        ),
        milestone_contributions=_enrich_milestone_contributions(
            pack.milestone_contributions,
            program_id=program_id,
            milestones=milestones,
            items=items,
            as_of=current_time,
            programs_root=resolved_programs_root,
        ),
    )
    output_path = write_owner_pack(pack)
    return OwnerPackArtifacts(pack=pack, output_path=output_path, ado_calls=ado_calls + vitality_artifacts.ado_calls)


def _enrich_milestone_contributions(
    contributions: tuple[OwnerPackMilestoneContribution, ...],
    *,
    program_id: str,
    milestones: tuple,
    items: tuple[WorkItem, ...],
    as_of: datetime,
    programs_root: Path,
) -> tuple[OwnerPackMilestoneContribution, ...]:
    if not contributions or not milestones:
        return contributions

    milestone_by_id = {milestone.id: milestone for milestone in milestones}
    trajectory_store = build_trajectory_store_for_program_id(
        program_id,
        programs_root=programs_root,
    )
    trajectories = {
        item_id: trajectory_store.read(program_id, item_id)
        for milestone in milestones
        for item_id in milestone.linked_work_item_ids
    }
    assessments = tuple(
        assess_milestone_health(milestone, items, trajectories, as_of)
        for milestone in milestones
    )
    assessment_by_id = {assessment.milestone_id: assessment for assessment in assessments}
    target_date_history = load_milestone_target_date_history_map(
        program_id,
        milestones,
        programs_root=programs_root,
    )
    completion_date_history = load_milestone_completion_date_history_map(
        program_id,
        milestones,
        current_completion_dates={assessment.milestone_id: assessment.completion_date for assessment in assessments},
        programs_root=programs_root,
    )

    enriched: list[OwnerPackMilestoneContribution] = []
    for contribution in contributions:
        milestone = milestone_by_id.get(contribution.milestone_id)
        assessment = assessment_by_id.get(contribution.milestone_id)
        if milestone is None or assessment is None:
            enriched.append(contribution)
            continue
        enriched.append(
            replace(
                contribution,
                computed_status=assessment.computed_health.value,
                schedule_summary=describe_milestone_schedule_variance(milestone, items, trajectories, as_of),
                target_history_summary=summarize_milestone_target_date_history(
                    target_date_history.get(milestone.id, ()),
                    prefix="target history",
                ),
                completion_history_summary=summarize_milestone_completion_date_history(
                    completion_date_history.get(milestone.id, ()),
                    prefix="completion history",
                ),
            )
        )
    return tuple(enriched)


def _load_owner_items(
    client: ADOClient | None,
    program: Program,
    as_of: datetime,
) -> tuple[tuple[WorkItem, ...], int]:
    if client is None or program.ado is None:
        return (), 0

    rows = client.query_all(
        filter_expression=(
            ODataFilter()
            .in_area_paths(program.ado.area_paths)
            .in_work_item_types(program.ado.work_item_types)
            .not_in_states(program.ado.excluded_states)
            .build()
        ),
        select_fields=(
            "WorkItemId",
            "WorkItemType",
            "Title",
            "State",
            "ChangedDate",
            "AreaPath",
            "IterationPath",
            "TargetDate",
            "Tags",
            "AssignedTo",
            "AssignedToEmail",
        ),
        top=report_helpers.DEFAULT_ADO_TOP,
    )
    return tuple(report_helpers._work_item_from_raw(row, as_of) for row in rows), 1


def _owner_vitality_summary(aggregates: tuple[VitalityAggregate, ...]) -> OwnerPackVitalitySummary | None:
    if not aggregates:
        return None
    aggregate = aggregates[0]
    return OwnerPackVitalitySummary(
        composite_score=aggregate.composite_score,
        total_items=aggregate.total_items,
        fresh_items=aggregate.fresh_items,
        avg_richness=aggregate.avg_richness,
        total_leakage=aggregate.total_leakage,
        workiq_signal_count=aggregate.workiq_signal_count,
    )


def _load_raci_workstream_scope_ids(
    *,
    owner_alias: str,
    workstreams: tuple[Workstream, ...],
) -> tuple[str, ...]:
    normalized_owner = _normalize_alias(owner_alias)
    scoped_ids: list[str] = []
    for workstream in workstreams:
        if not workstream.id.strip():
            continue
        if workstream.accountable_owner and _normalize_alias(workstream.accountable_owner) == normalized_owner:
            scoped_ids.append(workstream.id.strip())
            continue

        if any(_normalize_alias(alias) == normalized_owner for alias in workstream.responsible_owners):
            scoped_ids.append(workstream.id.strip())

    return tuple(dict.fromkeys(scoped_ids))


def _resolve_scoped_item_ids(
    *,
    items: tuple[WorkItem, ...],
    workstreams: tuple[Workstream, ...],
    scoped_workstream_ids: tuple[str, ...],
) -> tuple[int, ...]:
    if not scoped_workstream_ids:
        return ()
    scoped_workstream_id_set = set(scoped_workstream_ids)
    return tuple(
        item.id
        for item in items
        if gather_helpers._resolve_workstream_id(item.area_path, workstreams) in scoped_workstream_id_set
    )


def _normalize_alias(value: str | None) -> str:
    if value is None:
        return "unassigned"
    normalized = value.strip().lower()
    if "@" in normalized:
        normalized = normalized.split("@", 1)[0]
    return normalized or "unassigned"


def _load_owner_calibration_summary(
    *,
    program_id: str,
    owner_alias: str,
    programs_root: Path,
) -> OwnerPackCalibrationSummary | None:
    profile = load_forecast_calibration_dri_profile(
        program_id,
        _normalize_alias(owner_alias),
        programs_root=programs_root,
    )
    if profile is None:
        return None
    return OwnerPackCalibrationSummary(
        owner_alias=profile.dri_alias,
        claim_accuracy=profile.claim_accuracy,
        sample_size=profile.sample_size,
        met=profile.met,
        contradicted=profile.contradicted,
        stale=profile.stale,
        slip_modifier=profile.slip_modifier,
    )


def _format_percent(value: object) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{round(float(value) * 100):d}%"  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "n/a"
from src.core.risk_register_engine import load_risk_register
