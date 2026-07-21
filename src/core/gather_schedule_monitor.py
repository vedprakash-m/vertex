"""Manifest-aware, no-daemon monitoring for scheduled gather attempts.

The scheduled gather task cannot report an attempt that never starts.  A
separate, bounded Task Scheduler invocation therefore evaluates the persisted
gather-run lifecycle and records D-22's missed-attempt alert.  This module has
no Azure or scheduler dependency, so the same check is deterministic in a
runbook, test, or future scheduler implementation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.alerts import append_or_suppress_alert, entity_scoped_alert_id, resolve_alert
from src.core.gather_run_manifest import (
    COMMITTED_SUBDIR,
    FAILED_SUBDIR,
    QUARANTINE_SUBDIR,
    STAGING_SUBDIR,
    GatherRunManifest,
    get_gather_runs_dir,
    read_manifest,
)
from src.core.program_paths import PROGRAMS_ROOT


MISSED_ATTEMPT_CATEGORY = "gather_missed_attempt"
MISSED_ATTEMPT_ENTITY_TYPE = "gather_schedule"
DEFAULT_MAX_ATTEMPT_AGE = timedelta(hours=26)


@dataclass(frozen=True, slots=True)
class GatherScheduleStatus:
    """The current scheduler-attempt posture, independent of data freshness."""

    program_id: str
    checked_at: datetime
    last_attempt_at: datetime | None
    max_attempt_age: timedelta
    missed_attempt: bool
    alert_id: str


def _attempt_timestamp(manifest: GatherRunManifest) -> datetime:
    """Prefer explicit lifecycle evidence, retaining failed/crashed attempts."""
    return manifest.last_attempt_at or manifest.finished_at or manifest.started_at


def latest_gather_attempt_at(
    program_id: str, *, programs_root: Path = PROGRAMS_ROOT
) -> datetime | None:
    """Return the newest readable attempt across every lifecycle state.

    Failed and quarantined manifests count: they prove the scheduler attempted
    to run even though they must never become trusted scope.  A malformed
    manifest is ignored here; normal gather startup retains the stronger
    quarantine/diagnostic path for it, while this bounded monitor must still
    be able to identify a newer readable attempt.
    """
    runs_root = get_gather_runs_dir(program_id, programs_root=programs_root)
    timestamps: list[datetime] = []
    for state in (COMMITTED_SUBDIR, FAILED_SUBDIR, QUARANTINE_SUBDIR, STAGING_SUBDIR):
        state_dir = runs_root / state
        if not state_dir.exists():
            continue
        try:
            run_dirs = tuple(state_dir.iterdir())
        except OSError:
            continue
        for run_dir in run_dirs:
            if not run_dir.is_dir():
                continue
            try:
                manifest = read_manifest(run_dir)
            except (OSError, ValueError):
                continue
            attempted_at = _attempt_timestamp(manifest)
            # A legacy/corrupt naive timestamp cannot establish an auditable
            # schedule deadline; ignore it rather than letting a mixed-clock
            # comparison crash this independent monitor.
            if attempted_at.tzinfo is not None and attempted_at.utcoffset() is not None:
                timestamps.append(attempted_at)
    return max(timestamps) if timestamps else None


def evaluate_scheduled_gather_attempt(
    program_id: str,
    *,
    now: datetime | None = None,
    programs_root: Path = PROGRAMS_ROOT,
    max_attempt_age: timedelta = DEFAULT_MAX_ATTEMPT_AGE,
) -> GatherScheduleStatus:
    """Evaluate and durably alert on AG-7.8's independent 26-hour deadline.

    A healthy *attempt* resolves this category even if its source scope later
    proves partial; currentness/scope alerts remain separate conditions.  The
    append-only alert ledger preserves the recovery transition.  ``now`` is
    injectable to make threshold behavior deterministic.
    """
    if max_attempt_age <= timedelta(0):
        raise ValueError("max_attempt_age must be positive")
    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    last_attempt_at = latest_gather_attempt_at(program_id, programs_root=programs_root)
    missed_attempt = (
        last_attempt_at is None or checked_at - last_attempt_at >= max_attempt_age
    )
    alert_id = entity_scoped_alert_id(
        program_id=program_id,
        category=MISSED_ATTEMPT_CATEGORY,
        entity_type=MISSED_ATTEMPT_ENTITY_TYPE,
        entity_id=program_id,
    )

    if missed_attempt:
        observed = "no recorded gather attempt" if last_attempt_at is None else (
            f"last recorded gather attempt was {last_attempt_at.astimezone(timezone.utc).isoformat()}"
        )
        append_or_suppress_alert(
            program_id=program_id,
            category=MISSED_ATTEMPT_CATEGORY,
            entity_type=MISSED_ATTEMPT_ENTITY_TYPE,
            entity_id=program_id,
            severity="error",
            message=(
                f"Armada scheduled gather missed its {int(max_attempt_age.total_seconds() // 3600)}h "
                f"attempt deadline: {observed}."
            ),
            next_command=f"vertex gather --program {program_id}",
            programs_root=programs_root,
            context={
                "last_attempt_at": last_attempt_at.astimezone(timezone.utc).isoformat()
                if last_attempt_at is not None else None,
                "max_attempt_age_seconds": int(max_attempt_age.total_seconds()),
            },
            now=checked_at,
        )
    else:
        resolve_alert(alert_id, program_id=program_id, programs_root=programs_root, now=checked_at)

    return GatherScheduleStatus(
        program_id=program_id,
        checked_at=checked_at,
        last_attempt_at=last_attempt_at,
        max_attempt_age=max_attempt_age,
        missed_attempt=missed_attempt,
        alert_id=alert_id,
    )
