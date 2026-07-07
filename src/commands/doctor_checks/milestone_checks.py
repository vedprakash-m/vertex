from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.core.edition_resolver import resolve_edition
from src.core.exceptions import ConfigError
from src.core.milestone_engine import get_milestones_path
from src.core.models_v2 import Milestone
from src.core.program_fact_store import load_program_facts, project_milestones


def load_current_milestones(program_id: str, *, programs_root: Path) -> tuple[Milestone, ...]:
    return project_milestones(
        load_program_facts(
            program_id,
            programs_root=programs_root,
            fact_types=("milestone.entry",),
        )
    )


def run_milestone_doctor(
    *,
    edition_name: str,
    editions_root: Path,
    programs_root: Path,
    archive_root: Path,
    load_current_milestones_fn: Callable[[str], tuple[Any, ...]],
    load_milestone_owner_aliases_fn: Callable[[str], tuple[str, ...]],
    build_milestone_health_warning_fn: Callable[[str, str, tuple[Any, ...]], str | None],
) -> DoctorReport:
    resolved = resolve_edition(edition_name, editions_root=editions_root, programs_root=programs_root)
    if resolved is None:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Milestones", "fail", f"Edition '{edition_name}' could not be resolved."),),
        )

    milestones_path = get_milestones_path(resolved.paths.program_id, programs_root=programs_root)
    if not milestones_path.exists():
        return DoctorReport(
            edition=edition_name,
            checks=(
                DoctorCheck(
                    "Milestones",
                    "warn",
                    f"programs/{resolved.paths.program_id}/milestones.yaml is absent; milestone features are skipped.",
                ),
            ),
        )

    try:
        milestones = load_current_milestones_fn(resolved.paths.program_id)
        owner_aliases = set(load_milestone_owner_aliases_fn(resolved.paths.program_id))
    except ConfigError as error:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Milestones", "fail", str(error)),),
        )

    workstream_ids = {workstream.id for workstream in resolved.workstreams}
    problems: list[str] = []
    for milestone in milestones:
        if milestone.owner_alias not in owner_aliases:
            problems.append(
                f"Unknown owner_alias '{milestone.owner_alias}' referenced by milestone '{milestone.id}'."
            )
        for workstream_id in milestone.linked_workstream_ids:
            if workstream_id not in workstream_ids:
                problems.append(
                    f"Unknown linked_workstream_id '{workstream_id}' referenced by milestone '{milestone.id}'."
                )

    if problems:
        detail = "; ".join(problems[:2])
        if len(problems) > 2:
            detail = f"{detail}; +{len(problems) - 2} more"
        check = DoctorCheck("Milestones", "fail", detail)
    else:
        label = "milestone" if len(milestones) == 1 else "milestones"
        health_warning = build_milestone_health_warning_fn(
            edition_name,
            resolved.paths.program_id,
            milestones,
        )
        if health_warning is not None:
            check = DoctorCheck(
                "Milestones",
                "warn",
                f"programs/{resolved.paths.program_id}/milestones.yaml loaded ({len(milestones)} {label}); schema and references valid. {health_warning}",
            )
        else:
            check = DoctorCheck(
                "Milestones",
                "ok",
                f"programs/{resolved.paths.program_id}/milestones.yaml loaded ({len(milestones)} {label}); schema and references valid.",
            )

    return DoctorReport(edition=edition_name, checks=(check,))
