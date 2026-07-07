from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.core.claim_tracker import load_decision_asks
from src.core.decision_register import assess_decision_review_staleness, assess_proposed_decision_staleness, get_decisions_path
from src.core.edition_resolver import resolve_edition
from src.core.exceptions import ConfigError
from src.core.program_fact_store import load_program_facts, project_action_items, project_decision_entries, project_risk_entries
from src.core.risk_register_engine import get_risk_register_path
from src.core.action_tracker import get_actions_path


def run_decision_doctor(
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
            checks=(DoctorCheck("Decisions", "fail", f"Edition '{edition_name}' could not be resolved."),),
        )

    program_id = resolved.paths.program_id
    decisions_path = get_decisions_path(program_id, programs_root=programs_root)
    if not decisions_path.exists():
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Decisions", "warn", f"programs/{program_id}/decisions.yaml is absent; decision register features are skipped."),),
        )

    try:
        decisions = project_decision_entries(load_program_facts(program_id, programs_root=programs_root, fact_types=("decision.entry",)))
        owner_aliases = set(load_milestone_owner_aliases_fn(program_id))
    except ConfigError as error:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Decisions", "fail", str(error)),),
        )

    workstream_ids = {workstream.id for workstream in resolved.workstreams}
    decision_ids = {entry.id for entry in decisions}
    decision_ask_ids = {entry.id for entry in load_decision_asks(program_id, programs_root=programs_root)}
    risk_ids = {risk.id for risk in project_risk_entries(load_program_facts(program_id, programs_root=programs_root, fact_types=("risk.entry",)))} if get_risk_register_path(program_id, programs_root=programs_root).exists() else set()
    action_ids = {action.id for action in project_action_items(load_program_facts(program_id, programs_root=programs_root, fact_types=("action.item",)))} if get_actions_path(program_id, programs_root=programs_root).exists() else set()

    problems: list[str] = []
    stale_proposal_count = 0
    overdue_review_count = 0
    for entry in decisions:
        if entry.decided_by not in owner_aliases:
            problems.append(f"Unknown decided_by '{entry.decided_by}' referenced by decision '{entry.id}'.")
        if entry.workstream_id is not None and entry.workstream_id not in workstream_ids:
            problems.append(f"Unknown workstream_id '{entry.workstream_id}' referenced by decision '{entry.id}'.")
        if entry.linked_claim_id is not None and entry.linked_claim_id not in decision_ask_ids:
            problems.append(f"Unknown linked_claim_id '{entry.linked_claim_id}' referenced by decision '{entry.id}'.")
        if entry.linked_risk_id is not None and entry.linked_risk_id not in risk_ids:
            problems.append(f"Unknown linked_risk_id '{entry.linked_risk_id}' referenced by decision '{entry.id}'.")
        for action_id in entry.linked_action_ids:
            if action_id not in action_ids:
                problems.append(f"Unknown linked_action_id '{action_id}' referenced by decision '{entry.id}'.")
        if entry.superseded_by is not None and entry.superseded_by not in decision_ids:
            problems.append(f"Unknown superseded_by '{entry.superseded_by}' referenced by decision '{entry.id}'.")
        if assess_proposed_decision_staleness(entry, datetime.now(timezone.utc).date()):
            stale_proposal_count += 1
        if assess_decision_review_staleness(entry, datetime.now(timezone.utc).date()):
            overdue_review_count += 1

    if problems:
        detail = "; ".join(problems[:2])
        if len(problems) > 2:
            detail = f"{detail}; +{len(problems) - 2} more"
        check = DoctorCheck("Decisions", "fail", detail)
    elif stale_proposal_count or overdue_review_count:
        detail_parts: list[str] = []
        if stale_proposal_count:
            label = "entry" if stale_proposal_count == 1 else "entries"
            detail_parts.append(f"{stale_proposal_count} proposed decision {label} pending >14 days")
        if overdue_review_count:
            label = "entry" if overdue_review_count == 1 else "entries"
            detail_parts.append(f"{overdue_review_count} decided decision {label} overdue for review")
        check = DoctorCheck(
            "Decisions",
            "warn",
            f"programs/{program_id}/decisions.yaml loaded ({len(decisions)} decisions); schema and references valid. {'; '.join(detail_parts)}.",
        )
    else:
        check = DoctorCheck(
            "Decisions",
            "ok",
            f"programs/{program_id}/decisions.yaml loaded ({len(decisions)} decisions); schema, references, proposal ages, and review dates valid.",
        )
    return DoctorReport(edition=edition_name, checks=(check,))
