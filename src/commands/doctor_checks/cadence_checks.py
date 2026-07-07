from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.core.archive_store import find_latest_confirmed_entry, read_archive_index
from src.core.communication_plan import CommunicationPlanEntry, describe_communication_plan_entry, load_communication_plan_entries
from src.core.edition_resolver import resolve_edition
from src.core.policy_evaluator import check_cadence


def describe_cadence_status(cadence: str, last_confirmed_at: datetime, now: datetime) -> str:
    elapsed = max(now - last_confirmed_at, timedelta())
    if check_cadence(cadence, last_confirmed_at, as_of=now):
        days = elapsed.days
        if days <= 0:
            return "on track"
        return f"on track; last confirmed {days} day{'s' if days != 1 else ''} ago"

    if cadence == "daily":
        window_days = 1
    elif cadence == "weekly":
        window_days = 7
    elif cadence == "biweekly":
        window_days = 14
    elif cadence == "monthly":
        window_days = 30
    else:
        return "cadence window unknown"

    overdue_days = max(1, elapsed.days - window_days)
    return f"overdue by {overdue_days} day{'s' if overdue_days != 1 else ''}"


def run_cadence_doctor(
    *,
    edition_name: str,
    editions_root: Path,
    programs_root: Path,
    archive_root: Path,
    describe_cadence_status_fn: Callable[[str, datetime, datetime], str],
) -> DoctorReport:
    resolved = resolve_edition(edition_name, editions_root=editions_root, programs_root=programs_root)
    if resolved is None:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Cadence", "fail", f"Edition '{edition_name}' could not be resolved."),),
        )

    plan_entries = load_communication_plan_entries(resolved.raw_program)
    checks: list[DoctorCheck] = []
    if not plan_entries:
        checks.append(DoctorCheck("Cadence Plan", "ok", "communication_plan is absent; using edition cadence fallback."))
        plan_entries = (CommunicationPlanEntry(edition=edition_name),)

    for index, entry in enumerate(plan_entries, start=1):
        candidate_name = entry.edition
        candidate = resolve_edition(candidate_name, editions_root=editions_root, programs_root=programs_root)
        context = describe_communication_plan_entry(entry)
        if len(plan_entries) == 1:
            label = "Cadence"
        elif context:
            label = f"Cadence {candidate_name} [{context}]"
        else:
            label = f"Cadence {candidate_name} #{index}"
        if candidate is None:
            checks.append(DoctorCheck(label, "fail", f"communication_plan references unknown edition '{candidate_name}'."))
            continue

        planned_cadence = entry.cadence or candidate.edition.cadence
        if entry.cadence is not None and entry.cadence != candidate.edition.cadence:
            checks.append(
                DoctorCheck(
                    label,
                    "fail",
                    f"{candidate_name}: communication_plan cadence '{entry.cadence}' does not match edition cadence '{candidate.edition.cadence}'.",
                )
            )
            continue

        latest_confirmed = find_latest_confirmed_entry(read_archive_index(candidate_name, archive_root=archive_root))
        last_confirmed_at = latest_confirmed.generated_at if latest_confirmed is not None else None
        if last_confirmed_at is None:
            checks.append(
                DoctorCheck(
                    label,
                    "warn",
                    f"{candidate_name}: no confirmed issues yet ({planned_cadence} cadence).",
                )
            )
            continue

        status_detail = describe_cadence_status_fn(planned_cadence, last_confirmed_at, datetime.now(timezone.utc))
        checks.append(
            DoctorCheck(
                label,
                "ok" if check_cadence(planned_cadence, last_confirmed_at) else "warn",
                f"{candidate_name}: {status_detail} ({planned_cadence} cadence).",
            )
        )

    return DoctorReport(edition=edition_name, checks=tuple(checks))
