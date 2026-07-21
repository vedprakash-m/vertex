"""ADF-W5.10 (specs/arch-data-fix.md Section 10.6): scheduled-task health
sub-check.

Surfaces whether the out-of-band artifacts a Task Scheduler/cron runbook is
supposed to produce -- a fresh ``vertex prefetch`` snapshot and a fresh
``vertex cockpit build`` -- are actually present and within their staleness
budget, or have silently stopped being produced.

Status mapping (Section 10.3's "system-health status uses labels/icons, not
program-risk colors"):

  * ``ok``      -> the artifact is fresh (within budget)
  * ``warn``    -> the artifact exists but is stale (over budget)
  * ``fail``    -> reserved for an unrecoverable evaluation error; a *missing*
                  artifact is surfaced as ``info`` rather than ``fail`` because
                  a program that has never opted into a scheduled task is not
                  unhealthy -- it has simply not adopted that cadence yet.

Registered in ``src/commands/doctor.py`` via the ``--schedule-health`` flag.
Zone A (the primitive itself, ``src/core/schedule_health.py``) owns no AI or
M365 imports; this wrapper adds only the doctor presentation mapping.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.core.schedule_health import (
    ScheduleHealthFinding,
    evaluate_schedule_health,
)

#: Maps the primitive's three statuses to a doctor ``DoctorCheck.status``.
#: A missing artifact is deliberately downgraded to ``info`` (not ``fail``)
#: since a program that has never scheduled prefetch/cockpit builds is a
#: legitimate early-adoption state, not a health failure.
_STATUS_MAP: dict[str, str] = {
    "ok": "ok",
    "warn": "warn",
    "missing": "info",
    "inactive": "info",
}

_LABEL_BY_ARTIFACT: dict[str, str] = {
    "prefetch": "Scheduled Prefetch",
    "cockpit_html": "Scheduled Cockpit",
}


def run_schedule_health_doctor(
    *,
    program_id: str,
    programs_root: Path,
    now: datetime | None = None,
    prefetch_enabled: bool = True,
    evaluate_fn: Callable[..., Sequence[ScheduleHealthFinding]] | None = None,
) -> DoctorReport:
    """Return a DoctorReport auditing scheduled-task freshness for one program.

    Parameters
    ----------
    program_id
        The program whose schedule health is being audited.
    programs_root
        The ``programs/`` directory containing ``<program_id>/``.
    now
        Optional override for the staleness comparison instant (UTC).
    prefetch_enabled
        Whether the program has enabled the WorkIQ/M365 source consumed by
        ``vertex prefetch``. A disabled source is reported as inactive rather
        than incorrectly as a missing scheduled artifact.
    evaluate_fn
        Dependency seam for tests; defaults to the real
        :func:`src.core.schedule_health.evaluate_schedule_health`.
    """
    evaluator = evaluate_fn or evaluate_schedule_health
    resolved_now = now or datetime.now(timezone.utc)
    try:
        findings = evaluator(program_id, programs_root=programs_root, now=resolved_now)
    except (OSError, ValueError) as error:
        return DoctorReport(
            edition=program_id,
            checks=(
                DoctorCheck(
                    "Schedule Health",
                    "fail",
                    f"Could not evaluate schedule health for {program_id}: {error}",
                ),
            ),
        )

    if not prefetch_enabled:
        findings = tuple(
            ScheduleHealthFinding(
                artifact="prefetch",
                status="inactive",
                detail=(
                    f"WorkIQ prefetch is inactive for {program_id} because the program's "
                    "M365/WorkIQ channel is disabled."
                ),
                age_hours=None,
            )
            if finding.artifact == "prefetch"
            else finding
            for finding in findings
        )

    if not findings:
        return DoctorReport(
            edition=program_id,
            checks=(
                DoctorCheck(
                    "Schedule Health",
                    "info",
                    f"No schedule-health signals configured for {program_id}.",
                ),
            ),
        )

    checks: list[DoctorCheck] = []
    worst = "ok"
    for finding in findings:
        checks.append(_finding_to_check(finding))
        worst = _worst_status(worst, finding.status)

    # A summary row lets a human scan one line per program; the per-artifact
    # rows above carry the actionable detail and next command.
    detail_parts: list[str] = []
    for finding in findings:
        detail_parts.append(f"{finding.artifact}={finding.status}")
    summary = DoctorCheck(
        "Schedule Health",
        worst,
        f"{program_id}: " + ", ".join(detail_parts)
        + ". See per-artifact rows for the suggested next command.",
    )
    return DoctorReport(edition=program_id, checks=(summary, *tuple(checks)))


def _finding_to_check(finding: ScheduleHealthFinding) -> DoctorCheck:
    label = _LABEL_BY_ARTIFACT.get(finding.artifact, finding.artifact)
    status = _STATUS_MAP.get(finding.status, "info")
    metadata = {"age_hours": finding.age_hours} if finding.age_hours is not None else None
    if finding.status == "inactive":
        metadata = {"active": False}
    return DoctorCheck(
        label,
        status,
        finding.detail,
        metadata=metadata,
    )


def _worst_status(current: str, candidate: str) -> str:
    # ``missing`` (mapped to ``info`` at the row level) is the least severe
    # outcome here since it may simply mean "never opted in"; warn > fail > ok.
    resolved_candidate = _STATUS_MAP.get(candidate, "info")
    ordering = {"ok": 0, "info": 1, "warn": 2, "fail": 3}
    return resolved_candidate if ordering.get(resolved_candidate, 0) > ordering.get(current, 0) else current
