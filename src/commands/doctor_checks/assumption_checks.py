from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.core.assumption_tracker import check_validation_due, get_assumptions_path
from src.core.edition_resolver import resolve_edition
from src.core.exceptions import ConfigError
from src.core.milestone_engine import get_milestones_path
from src.core.program_fact_store import load_program_facts, project_assumptions, project_risk_entries
from src.core.risk_register_engine import get_risk_register_path


def run_assumption_doctor(
    *,
    edition_name: str,
    editions_root: Path,
    programs_root: Path,
    load_milestone_owner_aliases_fn: Callable[[str], tuple[str, ...]],
    load_current_milestones_fn: Callable[[str], tuple[Any, ...]],
) -> DoctorReport:
    resolved = resolve_edition(edition_name, editions_root=editions_root, programs_root=programs_root)
    if resolved is None:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Assumptions", "fail", f"Edition '{edition_name}' could not be resolved."),),
        )

    program_id = resolved.paths.program_id
    assumptions_path = get_assumptions_path(program_id, programs_root=programs_root)
    if not assumptions_path.exists():
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Assumptions", "warn", f"programs/{program_id}/assumptions.yaml is absent; assumption features are skipped."),),
        )

    try:
        assumptions = project_assumptions(
            load_program_facts(
                program_id,
                programs_root=programs_root,
                fact_types=("assumption.entry",),
            )
        )
        owner_aliases = set(load_milestone_owner_aliases_fn(program_id))
    except ConfigError as error:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Assumptions", "fail", str(error)),),
        )

    milestone_ids = None
    if get_milestones_path(program_id, programs_root=programs_root).exists():
        milestone_ids = {milestone.id for milestone in load_current_milestones_fn(program_id)}
    risk_ids = {risk.id for risk in project_risk_entries(load_program_facts(program_id, programs_root=programs_root, fact_types=("risk.entry",)))} if get_risk_register_path(program_id, programs_root=programs_root).exists() else set()

    problems: list[str] = []
    overdue_count = 0
    overdue_ids = {entry.id for entry in check_validation_due(assumptions, datetime.now(timezone.utc).date())}
    for entry in assumptions:
        if entry.owner_alias is not None and entry.owner_alias not in owner_aliases:
            problems.append(f"Unknown owner_alias '{entry.owner_alias}' referenced by assumption '{entry.id}'.")
        if entry.linked_milestone_id is not None:
            if milestone_ids is None:
                problems.append(
                    f"programs/{program_id}/milestones.yaml is missing but assumption '{entry.id}' references milestone '{entry.linked_milestone_id}'."
                )
            elif entry.linked_milestone_id not in milestone_ids:
                problems.append(f"Unknown linked_milestone_id '{entry.linked_milestone_id}' referenced by assumption '{entry.id}'.")
        if entry.linked_risk_id is not None and entry.linked_risk_id not in risk_ids:
            problems.append(f"Unknown linked_risk_id '{entry.linked_risk_id}' referenced by assumption '{entry.id}'.")
        if entry.id in overdue_ids:
            overdue_count += 1

    if problems:
        detail = "; ".join(problems[:2])
        if len(problems) > 2:
            detail = f"{detail}; +{len(problems) - 2} more"
        check = DoctorCheck("Assumptions", "fail", detail)
    elif overdue_count:
        label = "entry" if overdue_count == 1 else "entries"
        check = DoctorCheck(
            "Assumptions",
            "warn",
            f"programs/{program_id}/assumptions.yaml loaded ({len(assumptions)} assumptions); schema and references valid. {overdue_count} assumption {label} overdue for validation.",
        )
    else:
        check = DoctorCheck(
            "Assumptions",
            "ok",
            f"programs/{program_id}/assumptions.yaml loaded ({len(assumptions)} assumptions); schema, references, and validation dates valid.",
        )
    return DoctorReport(edition=edition_name, checks=(check,))
