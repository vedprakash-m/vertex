"""Current-state readers and open-action completeness gate for quality gates."""
from __future__ import annotations

from pathlib import Path

from src.core.models_v2 import ActionStatus
from src.core.program_fact_store import (
    load_program_facts,
    project_action_items,
    project_dependencies,
    project_milestones,
    project_risk_entries,
)
from src.core.quality_gates.models import GateEvaluation


def evaluate_open_action_completeness_gate(*, program_id: str | None, programs_root: Path) -> GateEvaluation:
    if program_id is None:
        return GateEvaluation("QG-15", True, "Open actions have owner and due-date coverage.", 2, forceable=True)

    incomplete_actions = [
        action.id
        for action in load_current_actions(program_id, programs_root=programs_root)
        if action.status in {ActionStatus.OPEN, ActionStatus.IN_PROGRESS}
        and (not action.owner_alias.strip() or action.owner_alias.strip().lower() == "unknown" or action.due_date is None)
    ]
    if not incomplete_actions:
        return GateEvaluation("QG-15", True, "Open actions have owner and due-date coverage.", 2, forceable=True)
    return GateEvaluation(
        "QG-15",
        False,
        f"Open action items missing owner or due date: {', '.join(incomplete_actions[:5])}",
        2,
        forceable=True,
    )


def load_current_actions(program_id: str, *, programs_root: Path):
    return project_action_items(
        load_program_facts(
            program_id,
            programs_root=programs_root,
            fact_types=("action.item",),
        )
    )


def load_current_milestones(program_id: str, *, programs_root: Path):
    return project_milestones(
        load_program_facts(
            program_id,
            programs_root=programs_root,
            fact_types=("milestone.entry",),
        )
    )


def load_current_risks(program_id: str, *, programs_root: Path):
    return project_risk_entries(
        load_program_facts(
            program_id,
            programs_root=programs_root,
            fact_types=("risk.entry",),
        )
    )


def load_current_dependencies(program_id: str, *, programs_root: Path):
    return project_dependencies(
        load_program_facts(
            program_id,
            programs_root=programs_root,
            fact_types=("dependency.link",),
        )
    )
