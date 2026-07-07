from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.core.edition_resolver import resolve_edition
from src.core.exceptions import ConfigError
from src.core.milestone_engine import get_milestones_path
from src.core.program_fact_store import load_program_facts, project_risk_entries
from src.core.risk_register_engine import assess_risk_staleness, get_risk_register_path


def run_risk_doctor(
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
            checks=(DoctorCheck("Risks", "fail", f"Edition '{edition_name}' could not be resolved."),),
        )

    program_id = resolved.paths.program_id
    risk_path = get_risk_register_path(program_id, programs_root=programs_root)
    if not risk_path.exists():
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Risks", "warn", f"programs/{program_id}/risk_register.yaml is absent; risk register features are skipped."),),
        )

    try:
        risks = project_risk_entries(load_program_facts(program_id, programs_root=programs_root, fact_types=("risk.entry",)))
        owner_aliases = set(load_milestone_owner_aliases_fn(program_id))
    except ConfigError as error:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Risks", "fail", str(error)),),
        )

    workstream_ids = {workstream.id for workstream in resolved.workstreams}
    milestone_ids = None
    problems: list[str] = []
    stale_count = 0
    for risk in risks:
        if risk.owner_alias not in owner_aliases:
            problems.append(f"Unknown owner_alias '{risk.owner_alias}' referenced by risk '{risk.id}'.")
        for workstream_id in risk.linked_workstream_ids:
            if workstream_id not in workstream_ids:
                problems.append(f"Unknown linked_workstream_id '{workstream_id}' referenced by risk '{risk.id}'.")
        if risk.linked_milestone_ids:
            if milestone_ids is None:
                milestone_path = get_milestones_path(program_id, programs_root=programs_root)
                milestone_ids = None if not milestone_path.exists() else {milestone.id for milestone in load_current_milestones_fn(program_id)}
            if milestone_ids is None:
                problems.append(f"programs/{program_id}/milestones.yaml is missing but risk '{risk.id}' references milestones.")
            else:
                for milestone_id in risk.linked_milestone_ids:
                    if milestone_id not in milestone_ids:
                        problems.append(f"Unknown linked_milestone_id '{milestone_id}' referenced by risk '{risk.id}'.")
        if assess_risk_staleness(risk, datetime.now(timezone.utc).date()):
            stale_count += 1

    if problems:
        detail = "; ".join(problems[:2])
        if len(problems) > 2:
            detail = f"{detail}; +{len(problems) - 2} more"
        check = DoctorCheck("Risks", "fail", detail)
    elif stale_count:
        label = "entry" if stale_count == 1 else "entries"
        check = DoctorCheck(
            "Risks",
            "warn",
            f"programs/{program_id}/risk_register.yaml loaded ({len(risks)} risks); schema and references valid. {stale_count} open risk {label} need review.",
        )
    else:
        check = DoctorCheck(
            "Risks",
            "ok",
            f"programs/{program_id}/risk_register.yaml loaded ({len(risks)} risks); schema, references, and review dates valid.",
        )
    return DoctorReport(edition=edition_name, checks=(check,))
