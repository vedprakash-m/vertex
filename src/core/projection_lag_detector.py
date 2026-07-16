"""ADF-W5.8 (specs/arch-data-fix.md Section 8.2.5): ``projection_lag`` alert.

The cockpit snapshot is a *projection* of the program's durable data. Section
8.2.5 lists ``projection_lag`` as one of the alert categories whose *detection
logic did not exist anywhere* before this pass (confirmed by investigation --
the only ``stale``/``staleness`` hits in the codebase were human-facing cockpit
prose, never a comparison). This module is that detection logic.

``detect_projection_lag`` is a pure read-and-compare: the projection time is
the snapshot's own ``generated_at`` (never inferred), and the underlying-data
signal is the most-recent mtime across the durable artifacts the snapshot is
projected from (the canonical fact-store database and the latest run-telemetry
record). If the projection is *older* than the freshest underlying artifact by
more than ``max_lag_minutes``, the projection is stale: the snapshot reflects a
state the underlying data has already moved past, and a re-build is warranted.

A *stale* projection lags its source; ``detect_projection_lag`` returns a
``ProjectionLagFinding`` describing which artifact overtook it and by how much.
The caller (cockpit build) emits an alert best-effort via
``append_or_suppress_alert`` -- never breaking the build that just succeeded.

Zone A -- reads filesystem mtimes only; no AI/M365 imports.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.reality_store import get_program_reality_db_path
from src.core.run_telemetry import run_telemetry_path

#: Default lag budget. A cockpit snapshot older than the freshest underlying
#: artifact by more than this is "lagging" -- Section 8.2.5's projection_lag.
#: Deliberately generous: the cockpit is an asynchronous projection, not a
#: live mirror, so a few minutes of staleness while a cadence runs is normal.
#: This catches the real failure mode (a scheduled build stopped running while
#: the underlying data kept changing), not normal projection latency.
DEFAULT_MAX_LAG_MINUTES = 60.0

#: Artifacts whose mtime represents "the underlying data moved." A change to
#: any of these means a snapshot older than that change no longer reflects
#: current state. The fact store is authoritative for facts/risks/decisions;
#: run_telemetry records every gather/report run.
_UNDERLYING_ARTIFACT_LABELS = ("fact_store", "run_telemetry")


@dataclass(frozen=True, slots=True)
class UnderlyingArtifactSignal:
    label: str
    path: Path
    mtime: datetime | None  # None when the artifact does not exist (yet)


@dataclass(frozen=True, slots=True)
class ProjectionLagFinding:
    """The result of comparing one projection against its underlying sources.

    ``is_lagging`` is True iff the projection time is older than the freshest
    underlying artifact by more than ``max_lag_minutes``. When False, the
    projection is current (or no underlying artifact exists to compare).
    """

    is_lagging: bool
    projection_at: datetime
    freshest_artifact: str | None  # label of the artifact that overtook it
    lag_minutes: float | None  # how far behind (only set when lagging)
    max_lag_minutes: float
    detail: str


def _resolve_underlying_signals(
    program_id: str, *, programs_root: Path
) -> tuple[UnderlyingArtifactSignal, ...]:
    """Read the mtime of each underlying artifact the cockpit projects from."""
    fact_store_path = get_program_reality_db_path(program_id, programs_root=programs_root)
    run_telemetry = run_telemetry_path(program_id, programs_root)

    signals: list[UnderlyingArtifactSignal] = []
    for label, path in (("fact_store", fact_store_path), ("run_telemetry", run_telemetry)):
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) if path.exists() else None
        except OSError:
            mtime = None
        signals.append(UnderlyingArtifactSignal(label, path, mtime))
    return tuple(signals)


def detect_projection_lag(
    program_id: str,
    *,
    projection_at: datetime,
    programs_root: Path = PROGRAMS_ROOT,
    max_lag_minutes: float = DEFAULT_MAX_LAG_MINUTES,
) -> ProjectionLagFinding:
    """Compare the projection time against the freshest underlying artifact.

    Returns a non-lagging finding when no underlying artifact exists (a brand-
    new program with no gather/report history has nothing to lag behind) or
    when the projection is within budget of the freshest one.
    """
    if projection_at.tzinfo is None:
        # Defensive: treat a naive datetime as UTC rather than raising -- a
        # cockpit snapshot's generated_at is always timezone-aware in practice.
        projection_at = projection_at.replace(tzinfo=timezone.utc)

    signals = _resolve_underlying_signals(program_id, programs_root=programs_root)
    freshest: UnderlyingArtifactSignal | None = None
    for signal in signals:
        signal_mtime = signal.mtime
        if signal_mtime is None:
            continue
        if freshest is None or freshest.mtime is None or signal_mtime > freshest.mtime:
            freshest = signal

    if freshest is None:
        return ProjectionLagFinding(
            is_lagging=False,
            projection_at=projection_at,
            freshest_artifact=None,
            lag_minutes=None,
            max_lag_minutes=max_lag_minutes,
            detail=f"No underlying data artifacts found for {program_id}; "
            "projection cannot lag what does not exist yet.",
        )

    # A projection newer than the data is the normal case (we just rebuilt it).
    # A projection older than the data by more than the budget is the alert.
    freshest_mtime = freshest.mtime
    assert freshest_mtime is not None
    delta = (freshest_mtime - projection_at).total_seconds() / 60.0
    if delta <= max_lag_minutes:
        return ProjectionLagFinding(
            is_lagging=False,
            projection_at=projection_at,
            freshest_artifact=freshest.label,
            lag_minutes=max(0.0, delta),
            max_lag_minutes=max_lag_minutes,
            detail=f"Projection is {max(0.0, delta):.1f}min from freshest artifact "
            f"({freshest.label}); within {max_lag_minutes:.0f}min budget.",
        )

    return ProjectionLagFinding(
        is_lagging=True,
        projection_at=projection_at,
        freshest_artifact=freshest.label,
        lag_minutes=delta,
        max_lag_minutes=max_lag_minutes,
        detail=(
            f"Projection is {delta:.0f}min older than the freshest underlying artifact "
            f"({freshest.label}) -- max budget {max_lag_minutes:.0f}min. "
            "The cockpit reflects a state the underlying data has already moved past; rebuild it."
        ),
    )


def build_projection_lag_alert_message(finding: ProjectionLagFinding) -> tuple[str, str]:
    """Return ``(message, next_command)`` for a lagging finding.

    Kept as a separate helper so the cockpit wiring can format the alert
    payload without importing the alerts module into the detector (which stays
    a pure read-and-compare with no alert side effects).
    """
    assert finding.is_lagging and finding.freshest_artifact is not None
    message = (
        f"Projection lag: cockpit snapshot is {finding.lag_minutes:.0f}min behind the freshest "
        f"underlying artifact ({finding.freshest_artifact}), over the {finding.max_lag_minutes:.0f}min budget."
    )
    next_command = "vertex cockpit show --program {program}  # rebuild the projection"
    return message, next_command


__all__ = [
    "DEFAULT_MAX_LAG_MINUTES",
    "ProjectionLagFinding",
    "UnderlyingArtifactSignal",
    "build_projection_lag_alert_message",
    "detect_projection_lag",
]
