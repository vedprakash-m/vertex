from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.core.action_tracker import assess_action_staleness, get_actions_path
from src.core.edition_resolver import resolve_edition
from src.core.exceptions import ConfigError
from src.core.program_fact_store import load_program_facts, project_action_items, project_risk_entries
from src.core.risk_register_engine import get_risk_register_path


def run_action_doctor(
    *,
    edition_name: str,
    editions_root: Path,
    programs_root: Path,
    load_milestone_owner_aliases_fn: Callable[[str], tuple[str, ...]],
) -> DoctorReport:
    resolved = resolve_edition(edition_name, editions_root=editions_root, programs_root=programs_root)
    if resolved is None:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Actions", "fail", f"Edition '{edition_name}' could not be resolved."),),
        )

    program_id = resolved.paths.program_id
    actions_path = get_actions_path(program_id, programs_root=programs_root)
    if not actions_path.exists():
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Actions", "warn", f"programs/{program_id}/journal/actions.jsonl is absent; action features are skipped."),),
        )

    try:
        actions = project_action_items(load_program_facts(program_id, programs_root=programs_root, fact_types=("action.item",)))
        owner_aliases = set(load_milestone_owner_aliases_fn(program_id))
    except (ConfigError, KeyError, TypeError, ValueError) as error:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Actions", "fail", str(error)),),
        )

    workstream_ids = {workstream.id for workstream in resolved.workstreams}
    risk_ids = {risk.id for risk in project_risk_entries(load_program_facts(program_id, programs_root=programs_root, fact_types=("risk.entry",)))} if get_risk_register_path(program_id, programs_root=programs_root).exists() else set()
    problems: list[str] = []
    for action in actions:
        if action.owner_alias not in owner_aliases:
            problems.append(f"Unknown owner_alias '{action.owner_alias}' referenced by action '{action.id}'.")
        if action.workstream_id is not None and action.workstream_id not in workstream_ids:
            problems.append(f"Unknown workstream_id '{action.workstream_id}' referenced by action '{action.id}'.")
        if action.linked_risk_id is not None and action.linked_risk_id not in risk_ids:
            problems.append(f"Unknown linked_risk_id '{action.linked_risk_id}' referenced by action '{action.id}'.")

    overdue_actions = assess_action_staleness(actions, datetime.now(timezone.utc).date())
    if problems:
        detail = "; ".join(problems[:2])
        if len(problems) > 2:
            detail = f"{detail}; +{len(problems) - 2} more"
        check = DoctorCheck("Actions", "fail", detail)
    elif overdue_actions:
        label = "entry" if len(overdue_actions) == 1 else "entries"
        check = DoctorCheck(
            "Actions",
            "warn",
            f"programs/{program_id}/journal/actions.jsonl loaded ({len(actions)} actions); schema and references valid. {len(overdue_actions)} open action {label} overdue.",
        )
    else:
        check = DoctorCheck(
            "Actions",
            "ok",
            f"programs/{program_id}/journal/actions.jsonl loaded ({len(actions)} actions); schema, references, and due dates valid.",
        )
    return DoctorReport(edition=edition_name, checks=(check,))
