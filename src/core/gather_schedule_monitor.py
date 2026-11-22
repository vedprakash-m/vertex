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
import os
from pathlib import Path
import subprocess
from typing import Callable, Mapping

import yaml

from src.core.alerts import append_or_suppress_alert, entity_scoped_alert_id, resolve_alert
from src.core.exceptions import AuthError, ConfigError
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
DEFAULT_OVERLAP_LEASE_SECONDS = 300
CRASH_RESUME_MODE = "quarantine-abandoned-then-retry"
MISSED_JOB_RESUME_MODE = "retry-on-next-authorized-start"


@dataclass(frozen=True, slots=True)
class ScheduledAcquisitionPolicy:
    mode: str
    authorized_principal: str
    approval_ref: str
    alert_delivery_ref: str


@dataclass(frozen=True, slots=True)
class ScheduledGatherReadiness:
    principal_authorized: bool
    read_only: bool
    overlap_bounded_seconds: int
    crash_resume: str
    missed_job_resume: str
    alert_ready: bool
    ready: bool
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GatherScheduleStatus:
    """The current scheduler-attempt posture, independent of data freshness."""

    program_id: str
    checked_at: datetime
    last_attempt_at: datetime | None
    max_attempt_age: timedelta
    missed_attempt: bool | None
    alert_id: str
    readiness: ScheduledGatherReadiness


class ScheduledGatherAuthorizationError(AuthError):
    """Scheduled acquisition refused before opening a lease or provider."""


def load_scheduled_acquisition_policy(
    program_id: str, *, programs_root: Path = PROGRAMS_ROOT
) -> ScheduledAcquisitionPolicy | None:
    path = programs_root / program_id / "program.yaml"
    if not path.exists():
        return None
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        document = {} if loaded is None else loaded
    except (OSError, yaml.YAMLError) as error:
        raise ConfigError(f"Could not load scheduled-acquisition policy for '{program_id}'.") from error
    if not isinstance(document, Mapping):
        raise ConfigError(f"Program config for '{program_id}' must be a mapping.")
    raw = document.get("scheduled_acquisition")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ConfigError(f"scheduled_acquisition for '{program_id}' must be a mapping.")

    def string(field: str) -> str:
        value = raw.get(field)
        if not isinstance(value, str):
            return ""
        return value.strip()

    return ScheduledAcquisitionPolicy(
        mode=string("mode"),
        authorized_principal=string("authorized_principal"),
        approval_ref=string("approval_ref"),
        alert_delivery_ref=string("alert_delivery_ref"),
    )


def evaluate_scheduled_gather_readiness(
    program_id: str,
    *,
    principal_resolver: Callable[[], str] | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> ScheduledGatherReadiness:
    policy = load_scheduled_acquisition_policy(program_id, programs_root=programs_root)
    blockers: list[str] = []
    if policy is None:
        principal_authorized = False
        read_only = False
        alert_ready = False
        blockers.append("scheduled principal authorization missing")
        blockers.append("read-only acquisition policy missing")
        blockers.append("alert delivery authorization missing")
    else:
        try:
            principal = (principal_resolver or resolve_scheduled_process_principal)().strip()
        except (OSError, RuntimeError):
            principal = ""
        if not principal or not policy.authorized_principal:
            principal_authorized = False
            blockers.append("scheduled principal authorization missing")
        elif principal != policy.authorized_principal:
            principal_authorized = False
            blockers.append("scheduled principal mismatch")
        else:
            principal_authorized = True
        if not policy.approval_ref:
            principal_authorized = False
            blockers.append("owner approval reference missing")
        read_only = policy.mode == "read_only"
        if not read_only:
            blockers.append("scheduled acquisition is not explicitly read-only")
        alert_ready = bool(policy.alert_delivery_ref)
        if not alert_ready:
            blockers.append("alert delivery authorization missing")
    ready = principal_authorized and read_only and alert_ready
    return ScheduledGatherReadiness(
        principal_authorized=principal_authorized,
        read_only=read_only,
        overlap_bounded_seconds=DEFAULT_OVERLAP_LEASE_SECONDS,
        crash_resume=CRASH_RESUME_MODE,
        missed_job_resume=MISSED_JOB_RESUME_MODE,
        alert_ready=alert_ready,
        ready=ready,
        blockers=tuple(blockers),
    )


def require_scheduled_gather_authorized(
    program_id: str,
    *,
    principal_resolver: Callable[[], str] | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> ScheduledGatherReadiness:
    readiness = evaluate_scheduled_gather_readiness(
        program_id,
        principal_resolver=principal_resolver,
        programs_root=programs_root,
    )
    if not readiness.ready:
        blockers = "; ".join(readiness.blockers)
        raise ScheduledGatherAuthorizationError(
            f"Scheduled gather refused before acquisition for program '{program_id}': {blockers}."
        )
    return readiness


def resolve_scheduled_process_principal() -> str:
    """Return the OS-bound identity of this process, never a caller assertion."""
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot")
        if not system_root:
            raise RuntimeError("Windows process principal could not be resolved")
        whoami_path = Path(system_root) / "System32" / "whoami.exe"
        completed = subprocess.run(
            [str(whoami_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            raise RuntimeError("Windows process principal could not be resolved")
        return completed.stdout.strip()
    try:
        completed = subprocess.run(
            ["id", "-un"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            raise RuntimeError("POSIX process principal could not be resolved")
        return completed.stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("POSIX process principal could not be resolved") from error


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
    principal_resolver: Callable[[], str] | None = None,
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
    readiness = evaluate_scheduled_gather_readiness(
        program_id,
        principal_resolver=principal_resolver,
        programs_root=programs_root,
    )
    alert_id = entity_scoped_alert_id(
        program_id=program_id,
        category=MISSED_ATTEMPT_CATEGORY,
        entity_type=MISSED_ATTEMPT_ENTITY_TYPE,
        entity_id=program_id,
    )
    if not readiness.ready:
        return GatherScheduleStatus(
            program_id=program_id,
            checked_at=checked_at,
            last_attempt_at=None,
            max_attempt_age=max_attempt_age,
            missed_attempt=None,
            alert_id=alert_id,
            readiness=readiness,
        )
    last_attempt_at = latest_gather_attempt_at(program_id, programs_root=programs_root)
    missed_attempt = (
        last_attempt_at is None or checked_at - last_attempt_at >= max_attempt_age
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
                f"Scheduled gather missed its {int(max_attempt_age.total_seconds() // 3600)}h "
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
        readiness=readiness,
    )
