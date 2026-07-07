from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from src.core.models_v2 import TrajectoryPoint


PatternName = Literal[
    "eta_drift",
    "chronic_reassign",
    "state_oscillation",
    "stale",
    "scope_creep",
    "priority_flip",
    "blocked_long",
    "eta_compression",
]
PatternSeverity = Literal["high", "medium", "low"]


@dataclass(frozen=True, slots=True)
class DriftPattern:
    work_item_id: int
    pattern: PatternName
    severity: PatternSeverity
    detail: str
    occurrences: int
    window_days: int


def analyze_trajectories(
    trajectories: dict[int, tuple[TrajectoryPoint, ...]],
    *,
    window_days: int = 90,
    as_of: date | None = None,
) -> tuple[DriftPattern, ...]:
    reference_date = as_of or datetime.now(timezone.utc).date()
    window_start = reference_date - timedelta(days=window_days)
    detected: list[DriftPattern] = []
    ordered_trajectories: dict[int, tuple[TrajectoryPoint, ...]] = {}

    for work_item_id, points in sorted(trajectories.items()):
        ordered = tuple(sorted(points, key=lambda point: point.date))
        if not ordered:
            continue
        ordered_trajectories[work_item_id] = ordered
        in_window = tuple(point for point in ordered if point.date >= window_start)
        detected.extend(_detect_eta_drift(work_item_id, in_window, window_days))
        detected.extend(_detect_chronic_reassign(work_item_id, in_window, window_days))
        detected.extend(_detect_state_oscillation(work_item_id, in_window, window_days))
        detected.extend(_detect_stale(work_item_id, ordered, in_window, window_days))
        detected.extend(_detect_priority_flip(work_item_id, ordered, as_of=reference_date))
        detected.extend(_detect_blocked_long(work_item_id, ordered, as_of=reference_date))
        detected.extend(_detect_eta_compression(work_item_id, in_window, window_days))

    detected.extend(_detect_scope_creep(ordered_trajectories, window_days=window_days, as_of=reference_date))

    detected.sort(key=lambda entry: (_severity_rank(entry.severity), entry.work_item_id, entry.pattern))
    return tuple(detected)


def count_eta_slips(
    points: tuple[TrajectoryPoint, ...],
    *,
    window_days: int = 90,
    as_of: date | None = None,
) -> int:
    reference_date = as_of or datetime.now(timezone.utc).date()
    window_start = reference_date - timedelta(days=window_days)
    ordered = tuple(sorted(points, key=lambda point: point.date))
    in_window = tuple(point for point in ordered if point.date >= window_start)
    slips = 0
    for previous, current in zip(in_window, in_window[1:], strict=False):
        if previous.target_date is None or current.target_date is None:
            continue
        if current.target_date > previous.target_date:
            slips += 1
    return slips


def _detect_eta_drift(
    work_item_id: int,
    points: tuple[TrajectoryPoint, ...],
    window_days: int,
) -> tuple[DriftPattern, ...]:
    target_date_changes = 0
    later_slips = 0

    for previous, current in zip(points, points[1:], strict=False):
        previous_target = previous.target_date
        current_target = current.target_date
        if previous_target is None or current_target is None:
            continue
        if previous_target == current_target:
            continue
        target_date_changes += 1
        if current_target <= previous_target:
            continue
        later_slips += 1

    if target_date_changes < 2:
        return ()

    severity: PatternSeverity = "high" if later_slips >= 3 else "medium"
    return (
        DriftPattern(
            work_item_id=work_item_id,
            pattern="eta_drift",
            severity=severity,
            detail=f"Target date slipped {later_slips} times in the last {window_days} days.",
            occurrences=later_slips,
            window_days=window_days,
        ),
    )


def _detect_chronic_reassign(
    work_item_id: int,
    points: tuple[TrajectoryPoint, ...],
    window_days: int,
) -> tuple[DriftPattern, ...]:
    reassignments = sum(
        1
        for previous, current in zip(points, points[1:], strict=False)
        if _normalized_owner(previous.assigned_to) != _normalized_owner(current.assigned_to)
    )
    if reassignments < 3:
        return ()
    return (
        DriftPattern(
            work_item_id=work_item_id,
            pattern="chronic_reassign",
            severity="medium",
            detail=f"Assigned owner changed {reassignments} times in the last {window_days} days.",
            occurrences=reassignments,
            window_days=window_days,
        ),
    )


def _detect_state_oscillation(
    work_item_id: int,
    points: tuple[TrajectoryPoint, ...],
    window_days: int,
) -> tuple[DriftPattern, ...]:
    oscillations = sum(
        1
        for previous, current in zip(points, points[1:], strict=False)
        if {_normalize_state(previous.state), _normalize_state(current.state)} == {"active", "resolved"}
    )
    if oscillations < 2:
        return ()
    return (
        DriftPattern(
            work_item_id=work_item_id,
            pattern="state_oscillation",
            severity="medium",
            detail=f"State toggled between Active and Resolved {oscillations} times in the last {window_days} days.",
            occurrences=oscillations,
            window_days=window_days,
        ),
    )


def _detect_stale(
    work_item_id: int,
    ordered_points: tuple[TrajectoryPoint, ...],
    in_window: tuple[TrajectoryPoint, ...],
    window_days: int,
) -> tuple[DriftPattern, ...]:
    if in_window:
        return ()
    latest = ordered_points[-1]
    if _normalize_state(latest.state) != "active":
        return ()
    return (
        DriftPattern(
            work_item_id=work_item_id,
            pattern="stale",
            severity="low",
            detail=f"No trajectory updates in the last {window_days} days while the item remains Active.",
            occurrences=1,
            window_days=window_days,
        ),
    )


def _detect_scope_creep(
    trajectories: dict[int, tuple[TrajectoryPoint, ...]],
    *,
    window_days: int,
    as_of: date,
) -> tuple[DriftPattern, ...]:
    window_start = as_of - timedelta(days=window_days)
    burst_start = as_of - timedelta(days=7)
    detected: list[DriftPattern] = []
    matched_item_ids: set[int] = set()
    grouped_new_items: dict[str, list[int]] = {}
    display_area_paths: dict[str, str] = {}

    for work_item_id, ordered in sorted(trajectories.items()):
        in_window = tuple(point for point in ordered if point.date >= window_start)
        migrations = tuple(
            (previous, current)
            for previous, current in zip(in_window, in_window[1:], strict=False)
            if _normalize_area_path(previous.area_path) != _normalize_area_path(current.area_path)
        )
        if migrations:
            first_migration = migrations[0]
            detected.append(
                DriftPattern(
                    work_item_id=work_item_id,
                    pattern="scope_creep",
                    severity="medium",
                    detail=(
                        f"Area path moved from {first_migration[0].area_path} to {first_migration[1].area_path} "
                        f"in the last {window_days} days."
                    ),
                    occurrences=len(migrations),
                    window_days=window_days,
                )
            )
            matched_item_ids.add(work_item_id)

        first_point = ordered[0]
        latest_point = ordered[-1]
        if first_point.date < burst_start:
            continue
        area_key = _normalize_area_path(latest_point.area_path)
        display_area_paths.setdefault(area_key, latest_point.area_path)
        grouped_new_items.setdefault(area_key, []).append(work_item_id)

    for area_key, work_item_ids in sorted(grouped_new_items.items()):
        if len(work_item_ids) <= 3:
            continue
        area_path = display_area_paths.get(area_key, area_key)
        for work_item_id in work_item_ids:
            if work_item_id in matched_item_ids:
                continue
            detected.append(
                DriftPattern(
                    work_item_id=work_item_id,
                    pattern="scope_creep",
                    severity="medium",
                    detail=f"{len(work_item_ids)} new items appeared under {area_path} in the last 7 days.",
                    occurrences=len(work_item_ids),
                    window_days=7,
                )
            )

    return tuple(detected)


def _detect_priority_flip(
    work_item_id: int,
    ordered_points: tuple[TrajectoryPoint, ...],
    *,
    as_of: date,
) -> tuple[DriftPattern, ...]:
    window_days = 30
    window_start = as_of - timedelta(days=window_days)
    recent_points = tuple(point for point in ordered_points if point.date >= window_start)
    risk_levels = _dedup_risk_levels(recent_points)
    if len(risk_levels) < 3:
        return ()

    reversions = sum(
        1
        for first, second, third in zip(risk_levels, risk_levels[1:], risk_levels[2:], strict=False)
        if first != second and first == third
    )
    flip_count = len(risk_levels) - 1
    if reversions < 1 or flip_count < 2:
        return ()

    path = " -> ".join(level.value.title() for level in risk_levels)
    return (
        DriftPattern(
            work_item_id=work_item_id,
            pattern="priority_flip",
            severity="medium",
            detail=f"Risk level flipped {flip_count} times in the last {window_days} days ({path}).",
            occurrences=flip_count,
            window_days=window_days,
        ),
    )


def _detect_blocked_long(
    work_item_id: int,
    ordered_points: tuple[TrajectoryPoint, ...],
    *,
    as_of: date,
) -> tuple[DriftPattern, ...]:
    if not ordered_points:
        return ()
    latest = ordered_points[-1]
    if not _is_blocked(latest):
        return ()

    blocked_since = latest.date
    for point in reversed(ordered_points[:-1]):
        if not _is_blocked(point):
            break
        blocked_since = point.date

    blocked_days = (as_of - blocked_since).days
    if blocked_days <= 14:
        return ()

    owner_label = latest.assigned_to or "unassigned"
    return (
        DriftPattern(
            work_item_id=work_item_id,
            pattern="blocked_long",
            severity="high",
            detail=f"Item has remained blocked for {blocked_days} days; current owner: {owner_label}.",
            occurrences=1,
            window_days=14,
        ),
    )


def _detect_eta_compression(
    work_item_id: int,
    points: tuple[TrajectoryPoint, ...],
    window_days: int,
) -> tuple[DriftPattern, ...]:
    slip_count = 0

    for previous, current in zip(points, points[1:], strict=False):
        if previous.target_date is None or current.target_date is None or previous.target_date == current.target_date:
            continue
        if current.target_date > previous.target_date:
            slip_count += 1
            continue
        if slip_count < 2:
            continue
        compression_days = (previous.target_date - current.target_date).days
        if compression_days <= 0:
            continue
        return (
            DriftPattern(
                work_item_id=work_item_id,
                pattern="eta_compression",
                severity="medium",
                detail=(
                    f"Target date slipped {slip_count} times, then moved {compression_days} days earlier "
                    f"in the last {window_days} days."
                ),
                occurrences=slip_count + 1,
                window_days=window_days,
            ),
        )

    return ()


def _normalized_owner(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip().lower()
    return text or None


def _dedup_risk_levels(points: tuple[TrajectoryPoint, ...]) -> tuple:
    risk_levels = []
    previous_level = None
    for point in points:
        if point.risk_level is None:
            continue
        if point.risk_level == previous_level:
            continue
        risk_levels.append(point.risk_level)
        previous_level = point.risk_level
    return tuple(risk_levels)


def _is_blocked(point: TrajectoryPoint) -> bool:
    if "blocked" in _normalize_state(point.state):
        return True
    return any("blocked" in tag.strip().lower() for tag in point.tags)


def _normalize_area_path(value: str) -> str:
    return value.strip().lower()


def _normalize_state(value: str) -> str:
    return value.strip().lower()


def _severity_rank(severity: PatternSeverity) -> int:
    order = {"high": 0, "medium": 1, "low": 2}
    return order[severity]


def compute_eta_credibility(
    points: tuple[TrajectoryPoint, ...],
    *,
    window_days: int = 90,
    as_of: date | None = None,
) -> tuple[float, tuple[date, ...]]:
    """FR-SG-21: Compute ETA credibility score and ordered slip dates for a trajectory.

    credibility = max(0.0, 1.0 - slip_count * 0.15 - slip_magnitude / 60.0)
    where slip_count = forward ETA moves in window, slip_magnitude = sum of positive deltas in days.

    Returns (credibility, slip_dates) where slip_dates are the pre-slip ETA dates, ordered.
    """
    reference_date = as_of or datetime.now(timezone.utc).date()
    window_start = reference_date - timedelta(days=window_days)
    ordered = tuple(sorted(points, key=lambda pt: pt.date))
    in_window = tuple(pt for pt in ordered if pt.date >= window_start)
    slip_count = 0
    slip_magnitude_days = 0
    slip_dates: list[date] = []
    for previous, current in zip(in_window, in_window[1:], strict=False):
        if previous.target_date is None or current.target_date is None:
            continue
        delta = (current.target_date - previous.target_date).days
        if delta > 0:
            slip_count += 1
            slip_magnitude_days += delta
            slip_dates.append(previous.target_date)
    credibility = max(0.0, 1.0 - slip_count * 0.15 - slip_magnitude_days / 60.0)
    return credibility, tuple(sorted(slip_dates))


def build_slip_history_markdown(
    points: tuple[TrajectoryPoint, ...],
    *,
    window_days: int = 90,
    as_of: date | None = None,
) -> str:
    """FR-SG-22: Render slip history as markdown with strikethrough for slipped ETAs.

    Format: ~~2024-01-15~~ ~~2024-02-28~~ 2024-03-31
    (slipped ETAs in strikethrough, current ETA unformatted)
    Returns empty string if no trajectory points.
    """
    _, slip_dates = compute_eta_credibility(points, window_days=window_days, as_of=as_of)
    ordered = tuple(sorted(points, key=lambda pt: pt.date))
    current_eta: date | None = None
    for pt in reversed(ordered):
        if pt.target_date is not None:
            current_eta = pt.target_date
            break
    parts = [f"~~{d.isoformat()}~~" for d in slip_dates]
    if current_eta is not None and (not slip_dates or current_eta != slip_dates[-1]):
        parts.append(current_eta.isoformat())
    return " ".join(parts)
