from __future__ import annotations

import json
from pathlib import Path

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.core.catchup_runner import get_catchup_usage_log_path
from src.core.edition_resolver import resolve_edition


def run_catchup_log_doctor(*, edition_name: str, editions_root: Path, programs_root: Path) -> DoctorReport:
    resolved = resolve_edition(edition_name, editions_root=editions_root, programs_root=programs_root)
    if resolved is None:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Catchup Log", "fail", f"Edition '{edition_name}' could not be resolved."),),
        )

    try:
        events = read_catchup_log_entries(resolved.program.id, programs_root=programs_root)
    except (OSError, ValueError) as error:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Catchup Log", "fail", str(error)),),
        )

    if not events:
        return DoctorReport(
            edition=edition_name,
            checks=(
                DoctorCheck(
                    "Catchup Log",
                    "ok",
                    f"No catchup failures or truncation events recorded for program '{resolved.program.id}'.",
                    metadata={"event_count": 0, "program_id": resolved.program.id},
                ),
            ),
        )

    recent_events = events[-3:]
    detail = "; ".join(format_catchup_log_event(entry) for entry in recent_events)
    return DoctorReport(
        edition=edition_name,
        checks=(
            DoctorCheck(
                "Catchup Log",
                "warn",
                f"Recent catchup events for program '{resolved.program.id}': {detail}",
                metadata={
                    "event_count": len(events),
                    "program_id": resolved.program.id,
                    "recent_events": recent_events,
                },
            ),
        ),
    )


def read_catchup_log_entries(program_id: str, *, programs_root: Path) -> tuple[dict[str, str], ...]:
    path = get_catchup_usage_log_path(program_id, programs_root=programs_root)
    if not path.exists():
        return ()

    entries: list[dict[str, str]] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError as error:
            raise ValueError(f"Malformed catchup log entry at {path}:{line_number}: {error.msg}.") from error
        if not isinstance(payload, dict):
            continue
        event_name = str(payload.get("event") or "").strip()
        if not event_name.startswith("catchup_"):
            continue
        entries.append(
            {
                "event": event_name,
                "recorded_at": str(payload.get("recorded_at") or ""),
                "reason": str(payload.get("reason") or "").strip(),
            }
        )
    return tuple(entries)


def format_catchup_log_event(entry: dict[str, str]) -> str:
    timestamp = entry.get("recorded_at") or "unknown time"
    event_name = entry.get("event") or "catchup_unknown"
    reason = entry.get("reason") or "no detail"
    return f"{timestamp} {event_name} ({reason})"
