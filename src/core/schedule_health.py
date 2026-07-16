"""ADF-W5.10 (specs/arch-data-fix.md Section 10.6): schedule health --
whether the out-of-band ``vertex prefetch``/``vertex cockpit build``
artifacts a Task Scheduler/cron job is supposed to produce are actually
fresh, or have silently stopped being produced.

This is a pure, standalone primitive (not yet wired into
``src/commands/doctor.py``'s main check composition -- that file is at its
architecture-fitness line budget with no headroom; see
`governance/runbooks/scheduled-tasks-runbook.md`'s explicit scope note).
Any caller (a future doctor check, `cockpit show`, an ad hoc script) can
use `evaluate_schedule_health` directly.

Zone A -- no AI or M365 imports.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.prefetch_store import read_latest_committed_snapshot

#: Section 10.6's own suggested cadence ("every 1-4 hours during business
#: hours") -- a prefetch snapshot older than this is "stale," not
#: necessarily wrong (the underlying data may still be usable, just not
#: freshly re-acquired on the expected schedule).
DEFAULT_PREFETCH_STALE_AFTER_HOURS = 6
DEFAULT_COCKPIT_STALE_AFTER_HOURS = 30  # a bit over one day


@dataclass(frozen=True, slots=True)
class ScheduleHealthFinding:
    artifact: str  # "prefetch" | "cockpit_html"
    status: str  # "ok" | "warn" | "missing"
    detail: str
    age_hours: float | None


def _cockpit_html_path(program_id: str, *, programs_root: Path) -> Path:
    return programs_root / program_id / "runtime" / "cockpit" / "cockpit.html"


def evaluate_prefetch_schedule_health(
    program_id: str,
    *,
    channel: str = "workiq",
    programs_root: Path = PROGRAMS_ROOT,
    stale_after_hours: float = DEFAULT_PREFETCH_STALE_AFTER_HOURS,
    now: datetime | None = None,
) -> ScheduleHealthFinding:
    resolved_now = now or datetime.now(timezone.utc)
    manifest = read_latest_committed_snapshot(program_id, channel, programs_root=programs_root)
    if manifest is None:
        return ScheduleHealthFinding(
            artifact="prefetch",
            status="missing",
            detail=f"No committed prefetch snapshot for {program_id}/{channel}. "
            "Either the scheduled task has never run, or it has always failed before committing. "
            "gather will fall back to a live WorkIQ call.",
            age_hours=None,
        )
    age_hours = (resolved_now - manifest.created_at).total_seconds() / 3600.0
    if age_hours > stale_after_hours:
        return ScheduleHealthFinding(
            artifact="prefetch",
            status="warn",
            detail=f"Latest prefetch snapshot for {program_id}/{channel} is {age_hours:.1f}h old "
            f"(expected refresh within {stale_after_hours:.0f}h) -- the scheduled task may have "
            "stopped running. gather will fall back to a live WorkIQ call once this snapshot expires.",
            age_hours=age_hours,
        )
    return ScheduleHealthFinding(
        artifact="prefetch",
        status="ok",
        detail=f"Latest prefetch snapshot for {program_id}/{channel} is {age_hours:.1f}h old "
        f"(completeness={manifest.completeness}).",
        age_hours=age_hours,
    )


def evaluate_cockpit_schedule_health(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    stale_after_hours: float = DEFAULT_COCKPIT_STALE_AFTER_HOURS,
    now: datetime | None = None,
) -> ScheduleHealthFinding:
    resolved_now = now or datetime.now(timezone.utc)
    html_path = _cockpit_html_path(program_id, programs_root=programs_root)
    if not html_path.exists():
        return ScheduleHealthFinding(
            artifact="cockpit_html",
            status="missing",
            detail=f"No cockpit.html found for {program_id}. Run `vertex cockpit build --program {program_id}` "
            "at least once, or schedule it per the runbook.",
            age_hours=None,
        )
    mtime = datetime.fromtimestamp(html_path.stat().st_mtime, tz=timezone.utc)
    age_hours = (resolved_now - mtime).total_seconds() / 3600.0
    if age_hours > stale_after_hours:
        return ScheduleHealthFinding(
            artifact="cockpit_html",
            status="warn",
            detail=f"cockpit.html for {program_id} is {age_hours:.1f}h old "
            f"(expected refresh within {stale_after_hours:.0f}h).",
            age_hours=age_hours,
        )
    return ScheduleHealthFinding(
        artifact="cockpit_html",
        status="ok",
        detail=f"cockpit.html for {program_id} is {age_hours:.1f}h old.",
        age_hours=age_hours,
    )


def evaluate_schedule_health(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    now: datetime | None = None,
) -> tuple[ScheduleHealthFinding, ...]:
    """The one entry point a caller (a future doctor check, a CLI command)
    should use -- returns both findings together."""
    return (
        evaluate_prefetch_schedule_health(program_id, programs_root=programs_root, now=now),
        evaluate_cockpit_schedule_health(program_id, programs_root=programs_root, now=now),
    )


__all__ = [
    "DEFAULT_COCKPIT_STALE_AFTER_HOURS",
    "DEFAULT_PREFETCH_STALE_AFTER_HOURS",
    "ScheduleHealthFinding",
    "evaluate_cockpit_schedule_health",
    "evaluate_prefetch_schedule_health",
    "evaluate_schedule_health",
]
