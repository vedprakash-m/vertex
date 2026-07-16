"""ADF-W5.9 (specs/arch-data-fix.md Section 9.7): cockpit history
retention/rotation.

Section 9.7's exact rule: "Last 30 builds + weekly keepers for 13 months.
Delete oldest non-keeper." This module is the pure retention-decision
logic (Zone A, filesystem-only); ``src/commands/cockpit.py::
persist_cockpit_snapshot`` calls it after every write, mirroring
``rev_cache_store.py::put_cached``'s own "write then prune" precedent
rather than requiring a separate manual rotation command.

Zone A -- no AI or M365 imports.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

#: Section 9.7, verbatim.
KEEP_LAST_N_BUILDS = 30
KEEP_WEEKLY_FOR_DAYS = 13 * 30  # "13 months" -- a calendar-month-free approximation, documented


def _parse_history_timestamp(path: Path) -> datetime | None:
    try:
        return datetime.strptime(path.stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def rotate_cockpit_history(history_dir: Path, *, now: datetime | None = None) -> tuple[Path, ...]:
    """Applies Section 9.7's retention rule to ``history_dir`` in place.
    Returns the tuple of deleted paths (empty if nothing needed deleting).

    Rule, applied in order:
    1. The most recent ``KEEP_LAST_N_BUILDS`` snapshots are always kept.
    2. Among anything older than that, at most one "weekly keeper" (the
       newest snapshot in each ISO calendar week) is kept, for snapshots
       within ``KEEP_WEEKLY_FOR_DAYS`` of ``now``.
    3. Everything else -- an older non-keeper, or anything past the
       13-month window entirely -- is deleted.
    """
    if not history_dir.is_dir():
        return ()
    resolved_now = now or datetime.now(timezone.utc)

    dated: list[tuple[datetime, Path]] = []
    for path in history_dir.glob("*.json"):
        stamp = _parse_history_timestamp(path)
        if stamp is not None:
            dated.append((stamp, path))
    dated.sort(key=lambda pair: pair[0], reverse=True)  # newest first

    kept_recent = {path for _, path in dated[:KEEP_LAST_N_BUILDS]}
    candidates_for_weekly = dated[KEEP_LAST_N_BUILDS:]

    weekly_cutoff = resolved_now - timedelta(days=KEEP_WEEKLY_FOR_DAYS)
    seen_weeks: set[tuple[int, int]] = set()
    kept_weekly: set[Path] = set()
    for stamp, path in candidates_for_weekly:
        if stamp < weekly_cutoff:
            continue  # past the 13-month window entirely -- never a keeper
        iso_year, iso_week, _ = stamp.isocalendar()
        week_key = (iso_year, iso_week)
        if week_key in seen_weeks:
            continue  # a newer snapshot in this same week is already the keeper
        seen_weeks.add(week_key)
        kept_weekly.add(path)

    to_delete = tuple(
        path for _, path in dated if path not in kept_recent and path not in kept_weekly
    )
    for path in to_delete:
        try:
            path.unlink()
        except OSError:
            pass  # best-effort -- a rotation failure must never break a cockpit write
    return to_delete


__all__ = ["KEEP_LAST_N_BUILDS", "KEEP_WEEKLY_FOR_DAYS", "rotate_cockpit_history"]
