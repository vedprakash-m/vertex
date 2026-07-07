from __future__ import annotations

from pathlib import Path
from src.commands import watch as watch_command
from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.core.edition_resolver import resolve_edition
from src.core.exceptions import ConfigError


def run_watch_source_doctor(
    *,
    edition_name: str,
    editions_root: Path,
    programs_root: Path,
    selected_sources: tuple[watch_command.WatchSource, ...],
) -> DoctorReport:
    resolved = resolve_edition(edition_name, editions_root=editions_root, programs_root=programs_root)
    if resolved is None:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Watch Sources", "fail", f"Edition '{edition_name}' could not be resolved."),),
        )

    try:
        watch_command.validate_watch_program(resolved.program)
    except ConfigError as error:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Watch Sources", "fail", str(error)),),
        )

    issues = watch_command.get_watch_source_readiness_issues(
        program_id=resolved.program.id,
        program=resolved.program,
        selected_sources=selected_sources,
        programs_root=programs_root,
    )
    if issues:
        return DoctorReport(
            edition=edition_name,
            checks=(
                DoctorCheck(
                    "Watch Sources",
                    "fail",
                    f"Selected watch sources not ready for program '{resolved.program.id}': {'; '.join(issues)}",
                ),
            ),
        )

    selected_labels = ", ".join(source.value for source in selected_sources)
    return DoctorReport(
        edition=edition_name,
        checks=(
            DoctorCheck(
                "Watch Sources",
                "ok",
                f"Selected watch sources ready for program '{resolved.program.id}': {selected_labels}.",
            ),
        ),
    )
