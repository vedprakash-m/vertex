from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from src.core.archive_store import find_latest_confirmed_entry, read_archive_index
from src.core.milestone_engine import (
    MilestoneStatus,
    assess_milestone_health,
    describe_milestone_schedule_variance,
    load_milestone_completion_date_history_map,
    load_milestone_target_date_history_map,
    summarize_milestone_completion_date_history,
    summarize_milestone_target_date_history,
)
from src.core.program_fact_store import load_program_facts, project_risk_entries
from src.core.risk_register_engine import get_risk_register_path
from src.core.snapshot_store import get_archive_root, read_snapshot
from src.core.models import WorkItem
from src.core.store_factory import build_trajectory_store_for_program_id


def build_milestone_health_warning(
    *,
    edition_name: str,
    program_id: str,
    milestones: tuple[Any, ...],
    programs_root: Path,
    archive_root: Path,
) -> str | None:
    snapshot_context = load_latest_confirmed_snapshot_items(
        edition_name,
        archive_root=archive_root,
    )
    if snapshot_context is None:
        return None

    snapshot_items, snapshot_as_of = snapshot_context
    trajectory_store = build_trajectory_store_for_program_id(
        program_id,
        programs_root=programs_root,
    )
    milestone_trajectories = {
        work_item_id: trajectory_store.read(program_id, work_item_id)
        for milestone in milestones
        for work_item_id in milestone.linked_work_item_ids
    }
    assessments = {
        milestone.id: assess_milestone_health(
            milestone,
            snapshot_items,
            milestone_trajectories,
            snapshot_as_of,
        )
        for milestone in milestones
    }
    affected = [
        (milestone, assessments[milestone.id])
        for milestone in milestones
        if assessments[milestone.id].computed_health in {MilestoneStatus.AT_RISK, MilestoneStatus.MISSED}
    ]
    if not affected:
        return None

    target_history_map = load_milestone_target_date_history_map(
        program_id,
        milestones,
        programs_root=programs_root,
    )
    completion_history_map = load_milestone_completion_date_history_map(
        program_id,
        milestones,
        current_completion_dates={
            milestone_id: assessment.completion_date
            for milestone_id, assessment in assessments.items()
            if assessment.completion_date is not None
        },
        programs_root=programs_root,
    )
    linked_risk_milestone_ids = set()
    risk_register_path = get_risk_register_path(program_id, programs_root=programs_root)
    if risk_register_path.exists():
        linked_risk_milestone_ids = {
            milestone_id
            for risk in project_risk_entries(load_program_facts(program_id, programs_root=programs_root, fact_types=("risk.entry",)))
            for milestone_id in risk.linked_milestone_ids
        }

    status_fragments: list[str] = []
    for status in (MilestoneStatus.AT_RISK, MilestoneStatus.MISSED):
        count = sum(1 for _, assessment in affected if assessment.computed_health == status)
        if count:
            status_label = status.value.replace("_", " ")
            milestone_label = "milestone" if count == 1 else "milestones"
            status_fragments.append(f"{count} {status_label} {milestone_label}")

    sample_milestone, sample_assessment = affected[0]
    sample_fragments: list[str] = []
    schedule_summary = describe_milestone_schedule_variance(
        sample_milestone,
        snapshot_items,
        milestone_trajectories,
        snapshot_as_of,
    )
    if schedule_summary is not None:
        sample_fragments.append(schedule_summary)
    target_history_summary = summarize_milestone_target_date_history(
        target_history_map.get(sample_milestone.id, ()),
    )
    if target_history_summary is not None:
        sample_fragments.append(target_history_summary)
    completion_history_summary = summarize_milestone_completion_date_history(
        completion_history_map.get(sample_milestone.id, ()),
    )
    if completion_history_summary is not None:
        sample_fragments.append(completion_history_summary)
    if not sample_fragments:
        sample_fragments.append(sample_assessment.reasoning)

    uncovered = [
        milestone.id
        for milestone, _ in affected
        if milestone.id not in linked_risk_milestone_ids
    ]

    detail_parts = [f"Latest confirmed snapshot shows {', '.join(status_fragments)}."]
    detail_parts.append(f"{sample_milestone.name}: {'; '.join(sample_fragments)}.")
    if uncovered:
        coverage_label = "milestone" if len(uncovered) == 1 else "milestones"
        detail_parts.append(
            f"{len(uncovered)} affected {coverage_label} missing linked risk coverage."
        )
    return " ".join(detail_parts)


def load_latest_confirmed_snapshot_items(
    edition_name: str,
    *,
    archive_root: Path,
) -> tuple[tuple[WorkItem, ...], datetime] | None:
    archive_index = read_archive_index(edition_name, archive_root=archive_root)
    latest_entry = find_latest_confirmed_entry(archive_index)
    if latest_entry is None:
        return None

    archive_root_path = get_archive_root(edition_name, archive_root)
    snapshot_path = (
        Path(latest_entry.snapshot_path)
        if latest_entry.snapshot_path is not None
        else archive_root_path / "snapshots" / f"issue_{latest_entry.issue_number:03d}.snapshot.json"
    )
    if not snapshot_path.exists():
        return None

    snapshot = read_snapshot(snapshot_path)
    return snapshot_to_work_items(snapshot), snapshot.ado_data_as_of


def snapshot_to_work_items(snapshot: Any) -> tuple[WorkItem, ...]:
    return tuple(
        WorkItem(
            id=item.id,
            type=item.type,
            title=item.title,
            state=item.state,
            assigned_to=item.assigned_to,
            assigned_to_email=None,
            area_path=item.area_path,
            iteration_path="",
            target_date=item.target_date,
            risk_level=item.risk_level,
            tags=list(item.tags),
            custom_fields={"changed_date": snapshot.ado_data_as_of.isoformat()},
            revisions=[],
            comments=[],
            fetched_at=snapshot.ado_data_as_of,
        )
        for item in snapshot.items
    )
