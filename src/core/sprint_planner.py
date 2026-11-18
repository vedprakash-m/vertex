"""Deterministic sprint planner (RELO §8.4.5, owner-approved reduced scope).

Greedy, dependency-aware fill of ADO story points. Not optimal. Capacity is
an estimate: the median completed story points of the last three sprints.
Leave and personal availability are deliberately not inputs, so no
person-level data can reach rendered output.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from statistics import median
from typing import Iterable, Sequence

CAPACITY_HISTORY_SPRINTS = 3
OVERCOMMIT_PROBABILITY = "Unknown"
METHOD_NOTE = "Greedy, not optimal: highest priority first, then lowest ID."


@dataclass(frozen=True, slots=True)
class BacklogItem:
    work_item_id: int
    title: str
    priority: int | None
    story_points: float | None
    predecessor_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class BlockedItem:
    item: BacklogItem
    unmet_predecessor_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SprintPlan:
    capacity_points: float | None
    planned: tuple[BacklogItem, ...]
    planned_points: float
    over_capacity: tuple[BacklogItem, ...]
    blocked: tuple[BlockedItem, ...]
    unestimated: tuple[BacklogItem, ...]
    wip_count: int | None


def _check_points(value: float, label: str) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} must be a finite non-negative number, got {value!r}")


def _sort_key(item: BacklogItem) -> tuple[bool, int, int]:
    return (item.priority is None, item.priority or 0, item.work_item_id)


def plan_sprint(
    backlog: Iterable[BacklogItem],
    *,
    completed_points_history: Sequence[float],
    done_ids: frozenset[int] = frozenset(),
    wip_count: int | None = None,
) -> SprintPlan:
    """Plan one sprint. ``completed_points_history`` is ordered oldest first."""
    items = sorted(backlog, key=_sort_key)
    seen: set[int] = set()
    for item in items:
        if item.work_item_id in seen:
            raise ValueError(f"duplicate work item ID {item.work_item_id}")
        seen.add(item.work_item_id)
        if item.story_points is not None:
            _check_points(item.story_points, f"story_points of #{item.work_item_id}")
    for value in completed_points_history:
        _check_points(value, "completed sprint points")

    recent = completed_points_history[-CAPACITY_HISTORY_SPRINTS:]
    capacity = float(median(recent)) if len(recent) == CAPACITY_HISTORY_SPRINTS else None

    planned: list[BacklogItem] = []
    planned_ids: set[int] = set()
    planned_points = 0.0
    over_capacity: list[BacklogItem] = []
    blocked: list[BlockedItem] = []
    unestimated: list[BacklogItem] = []
    for item in items:
        if item.story_points is None:
            unestimated.append(item)
            continue
        unmet = tuple(
            pred for pred in item.predecessor_ids if pred not in done_ids and pred not in planned_ids
        )
        if unmet:
            blocked.append(BlockedItem(item=item, unmet_predecessor_ids=unmet))
        elif capacity is None or planned_points + item.story_points > capacity:
            over_capacity.append(item)
        else:
            planned.append(item)
            planned_ids.add(item.work_item_id)
            planned_points += item.story_points

    return SprintPlan(
        capacity_points=capacity,
        planned=tuple(planned),
        planned_points=planned_points,
        over_capacity=tuple(over_capacity),
        blocked=tuple(blocked),
        unestimated=tuple(unestimated),
        wip_count=wip_count,
    )


def _line(item: BacklogItem) -> str:
    return f"- #{item.work_item_id} {item.title}"


def render_sprint_plan_summary(plan: SprintPlan) -> str:
    """Plain-text summary carrying the approved display caveats."""
    if plan.capacity_points is None:
        capacity = f"Unknown (fewer than {CAPACITY_HISTORY_SPRINTS} completed sprints)"
        planned = f"Planned: {len(plan.planned)} items"
    else:
        capacity = (
            f"{plan.capacity_points:g} story points "
            f"(median completed, last {CAPACITY_HISTORY_SPRINTS} sprints)"
        )
        noun = "item" if len(plan.planned) == 1 else "items"
        planned = (
            f"Planned: {plan.planned_points:g} of {plan.capacity_points:g} "
            f"estimated story points ({len(plan.planned)} {noun})"
        )
    wip = "Unknown" if plan.wip_count is None else f"{plan.wip_count} (displayed, not enforced)"
    lines = [
        f"Estimated capacity: {capacity}",
        planned,
        f"Overcommit probability: {OVERCOMMIT_PROBABILITY}",
        f"Current WIP: {wip}",
        METHOD_NOTE,
    ]
    sections = (
        ("Planned", [_line(item) for item in plan.planned]),
        (
            "Not planned: capacity unknown"
            if plan.capacity_points is None
            else "Does not fit remaining capacity",
            [_line(item) for item in plan.over_capacity],
        ),
        (
            "Blocked by dependency",
            [
                f"{_line(b.item)} (waiting on {', '.join(f'#{i}' for i in b.unmet_predecessor_ids)})"
                for b in plan.blocked
            ],
        ),
        ("Excluded: no story-point estimate", [_line(item) for item in plan.unestimated]),
    )
    for heading, entries in sections:
        if entries:
            lines.append(f"{heading}:")
            lines.extend(entries)
    return "\n".join(lines)
