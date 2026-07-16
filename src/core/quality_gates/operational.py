"""Operational quality gates for overdue, coverage, milestone, and dependency checks."""
from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

from src.core.coverage_gap import build_coverage_gaps
from src.core.dependency_graph import detect_cross_program_cascades
from src.core.milestone_engine import assess_milestone_health
from src.core.models import RiskLevel, WorkItem
from src.core.models_v2 import DependencyStatus, MilestoneStatus, Signal
from src.core.quality_gates.current_state import load_current_dependencies, load_current_milestones, load_current_risks
from src.core.quality_gates.models import GateEvaluation
from src.core.store_factory import build_trajectory_store_for_program_id
from src.core.trajectory_analyzer import analyze_trajectories
from src.core.work_item_states import TERMINAL_WORK_ITEM_STATES


def evaluate_overdue_target_gate(items: tuple[WorkItem, ...], today: date) -> GateEvaluation:
    overdue_item_ids = [item.id for item in items if has_overdue_target(item, today)]
    if not overdue_item_ids:
        return GateEvaluation("QG-9", True, "No overdue target dates on non-terminal items.", 2, forceable=True)
    return GateEvaluation(
        "QG-9",
        False,
        f"Overdue target dates on non-terminal items: {preview_work_item_ids(overdue_item_ids)}",
        2,
        forceable=True,
    )


def evaluate_milestone_risk_linkage_gate(
    *,
    items: tuple[WorkItem, ...],
    as_of: datetime,
    program_id: str | None,
    programs_root: Path,
) -> GateEvaluation:
    if program_id is None:
        return GateEvaluation("QG-16", True, "Milestone risk-link gate passed.", 2, forceable=True)

    milestones = load_current_milestones(program_id, programs_root=programs_root)
    if not milestones:
        return GateEvaluation("QG-16", True, "Milestone risk-link gate passed.", 2, forceable=True)

    trajectory_store = build_trajectory_store_for_program_id(program_id, programs_root=programs_root)
    trajectories = {
        item_id: trajectory_store.read(program_id, item_id)
        for milestone in milestones
        for item_id in milestone.linked_work_item_ids
    }
    open_risks = tuple(
        risk
        for risk in load_current_risks(program_id, programs_root=programs_root)
        if risk.status.value != "closed"
    )
    failing_milestones: list[str] = []
    for milestone in milestones:
        assessment = assess_milestone_health(milestone, items, trajectories, as_of)
        if assessment.computed_health not in {MilestoneStatus.AT_RISK, MilestoneStatus.MISSED, MilestoneStatus.UNKNOWN}:
            continue
        if has_milestone_risk_linkage(milestone=milestone, open_risks=open_risks):
            continue
        failing_milestones.append(f"{milestone.id} ({assessment.computed_health.value})")

    if not failing_milestones:
        return GateEvaluation("QG-16", True, "Milestone risk-link gate passed.", 2, forceable=True)
    return GateEvaluation(
        "QG-16",
        False,
        "At-risk or missed milestones without linked risk register coverage: " + ", ".join(failing_milestones),
        2,
        forceable=True,
    )


def evaluate_cross_program_dependency_cascade_gate(
    *,
    items: tuple[WorkItem, ...],
    approved_signals: tuple[Signal, ...],
    as_of: datetime,
    program_id: str | None,
    programs_root: Path,
) -> GateEvaluation:
    if program_id is None or not items:
        return GateEvaluation("QG-19", True, "Cross-program dependency cascade gate passed.", 2, forceable=True)

    dependencies = tuple(
        dependency
        for dependency in load_current_dependencies(program_id, programs_root=programs_root)
        if dependency.from_program_id != dependency.to_program_id and dependency.status is not DependencyStatus.RESOLVED
    )
    if not dependencies:
        return GateEvaluation("QG-19", True, "Cross-program dependency cascade gate passed.", 2, forceable=True)

    trajectory_store = build_trajectory_store_for_program_id(program_id, programs_root=programs_root)
    populated_trajectories = {
        item.id: points
        for item in items
        if (points := trajectory_store.read(program_id, item.id))
    }
    drift_patterns = analyze_trajectories(populated_trajectories, as_of=as_of.date()) if populated_trajectories else ()
    cascades = detect_cross_program_cascades(
        signals=approved_signals,
        drift_patterns=drift_patterns,
        dependencies=dependencies,
    )
    if not cascades:
        return GateEvaluation("QG-19", True, "Cross-program dependency cascade gate passed.", 2, forceable=True)

    preview_lines = tuple(dict.fromkeys(format_cross_program_cascade_gate_line(cascade) for cascade in cascades))
    preview = "; ".join(preview_lines[:3])
    if len(preview_lines) > 3:
        preview = f"{preview}; and {len(preview_lines) - 3} more"
    return GateEvaluation(
        "QG-19",
        False,
        "Cross-program dependency cascade detected without explicit resolution plan: " + preview,
        2,
        forceable=True,
    )


def evaluate_high_risk_coverage_gate(
    *,
    items: tuple[WorkItem, ...],
    approved_signals: tuple[Signal, ...],
    narratives: Mapping[str, str] | Iterable[str],
    as_of: datetime,
    covered_item_ids: Collection[int] = (),
) -> GateEvaluation:
    high_risk_items = tuple(
        item
        for item in items
        if item.risk_level == RiskLevel.HIGH and not is_terminal(item)
    )
    coverage_gaps = build_coverage_gaps(
        high_risk_items,
        approved_signals=approved_signals,
        narratives=narratives,
        as_of=as_of,
        min_age_days=0,
        covered_item_ids=covered_item_ids,
    )
    if not coverage_gaps:
        return GateEvaluation("QG-13", True, "All active High-risk items have signal or narrative coverage.", 3)
    preview = ", ".join(f"WI:{gap.work_item_id}" for gap in coverage_gaps[:5])
    if len(coverage_gaps) > 5:
        preview = f"{preview}, and {len(coverage_gaps) - 5} more"
    return GateEvaluation(
        "QG-13",
        False,
        f"High-risk coverage gap on active items: {preview}",
        3,
    )


def has_overdue_target(item: WorkItem, today: date) -> bool:
    return item.target_date is not None and item.target_date < today and not is_terminal(item)


def is_terminal(item: WorkItem) -> bool:
    return item.state.strip().lower() in TERMINAL_WORK_ITEM_STATES


def preview_work_item_ids(item_ids: list[int]) -> str:
    preview = ", ".join(f"WI:{work_item_id}" for work_item_id in item_ids[:5])
    if len(item_ids) > 5:
        preview = f"{preview}, and {len(item_ids) - 5} more"
    return preview


def has_milestone_risk_linkage(*, milestone: Any, open_risks: tuple[Any, ...]) -> bool:
    linked_workstream_ids = set(milestone.linked_workstream_ids)
    linked_work_item_ids = set(milestone.linked_work_item_ids)
    for risk in open_risks:
        if milestone.id in risk.linked_milestone_ids:
            return True
        if linked_workstream_ids.intersection(risk.linked_workstream_ids):
            return True
        if linked_work_item_ids.intersection(risk.linked_work_item_ids):
            return True
    return False


def format_cross_program_cascade_gate_line(cascade: Any) -> str:
    trigger = cascade.trigger_kind
    if cascade.work_item_id is not None:
        trigger = f"{trigger} WI:{cascade.work_item_id}"
    return f"{cascade.source_item} -> {cascade.target_item} ({trigger})"
