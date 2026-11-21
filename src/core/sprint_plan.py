from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from src.core.exceptions import ConfigError
from src.core.models import WorkItem
from src.core.work_item_states import TERMINAL_WORK_ITEM_STATES


@dataclass(frozen=True, slots=True)
class SprintBacklogItem:
    work_item_id: int
    title: str
    story_points: Decimal
    prerequisite_item_ids: tuple[int, ...]
    source_ref: str


@dataclass(frozen=True, slots=True)
class SprintCapacityInput:
    available_hours: Decimal
    source_ref: str


@dataclass(frozen=True, slots=True)
class SprintCompletedPointsInput:
    iteration_path: str
    story_points: Decimal
    source_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SprintPlanDecision:
    work_item_id: int
    title: str
    story_points: Decimal
    source_ref: str


@dataclass(frozen=True, slots=True)
class SprintPlanDeferral:
    work_item_id: int
    title: str
    story_points: Decimal
    source_ref: str
    reason: str


@dataclass(frozen=True, slots=True)
class SprintPlanArtifact:
    iteration_path: str
    point_capacity: Decimal | None
    selected: tuple[SprintPlanDecision, ...]
    deferred: tuple[SprintPlanDeferral, ...]
    omitted_work_item_ids: tuple[int, ...]
    selected_story_points: Decimal
    remaining_story_points: Decimal | None
    capacity_source_ref: str
    completed_sprints: tuple[SprintCompletedPointsInput, ...]
    completed_sprint_refs: tuple[str, ...]


def plan_sprint(
    *,
    iteration_path: str,
    backlog: tuple[SprintBacklogItem, ...],
    capacity: SprintCapacityInput,
    completed_sprints: tuple[SprintCompletedPointsInput, ...],
) -> SprintPlanArtifact:
    if capacity.available_hours < 0:
        raise ConfigError("Sprint capacity cannot be negative.")
    if any(item.story_points <= 0 for item in backlog):
        raise ConfigError("Sprint backlog story points must be positive.")
    if len({item.work_item_id for item in backlog}) != len(backlog):
        raise ConfigError("Sprint backlog work item ids must be unique.")
    _assert_acyclic(tuple((item.work_item_id, item.prerequisite_item_ids) for item in backlog))
    ordered_backlog = _topological_backlog(backlog)

    completed_sprints = tuple(
        sorted(completed_sprints, key=lambda sample: sample.iteration_path.casefold())
    )
    point_capacity = (
        None
        if not completed_sprints
        else _average_completed_points(completed_sprints) * capacity.available_hours / Decimal(80)
    )

    selected: list[SprintPlanDecision] = []
    deferred: list[SprintPlanDeferral] = []
    selected_ids: set[int] = set()
    backlog_ids = {item.work_item_id for item in backlog}
    selected_points = Decimal(0)
    for item in ordered_backlog:
        decision = SprintPlanDecision(
            item.work_item_id,
            item.title,
            item.story_points,
            item.source_ref,
        )
        external = tuple(
            prerequisite
            for prerequisite in item.prerequisite_item_ids
            if prerequisite not in backlog_ids
        )
        if point_capacity is None:
            reason = "no_completed_sprint_story_points"
        elif point_capacity == 0:
            reason = "zero_capacity"
        elif external:
            reason = "external_prerequisite"
        elif any(prerequisite not in selected_ids for prerequisite in item.prerequisite_item_ids):
            reason = "prerequisite_not_selected"
        elif selected_points + item.story_points > point_capacity:
            reason = "over_point_capacity"
        else:
            selected.append(decision)
            selected_ids.add(item.work_item_id)
            selected_points += item.story_points
            continue
        deferred.append(
            SprintPlanDeferral(
                decision.work_item_id,
                decision.title,
                decision.story_points,
                decision.source_ref,
                reason,
            )
        )
    remaining = None if point_capacity is None else point_capacity - selected_points
    return SprintPlanArtifact(
        iteration_path=iteration_path,
        point_capacity=point_capacity,
        selected=tuple(selected),
        deferred=tuple(deferred),
        omitted_work_item_ids=(),
        selected_story_points=selected_points,
        remaining_story_points=remaining,
        capacity_source_ref=capacity.source_ref,
        completed_sprints=completed_sprints,
        completed_sprint_refs=tuple(
            dict.fromkeys(
                ref for sprint in completed_sprints for ref in sprint.source_refs
            )
        ),
    )


def build_ado_sprint_plan(
    *,
    iteration_path: str,
    items: tuple[WorkItem, ...],
    capacity_per_day: Decimal | None,
    total_business_days: int,
    capacity_source_ref: str,
    sprint_snapshot_rows: list[dict[str, Any]],
    team_name: str | None,
    iteration_id: str | None,
) -> SprintPlanArtifact:
    backlog: list[SprintBacklogItem] = []
    omitted: list[int] = []
    for item in items:
        if item.iteration_path.strip().casefold() != iteration_path.strip().casefold():
            continue
        if item.state.strip().lower() in TERMINAL_WORK_ITEM_STATES:
            continue
        points = _decimal_value(item.custom_fields.get("story_points"))
        source_ref = item.custom_fields.get("story_points_source")
        if points is None or points <= 0 or not isinstance(source_ref, str) or not source_ref:
            omitted.append(item.id)
            continue
        backlog.append(
            SprintBacklogItem(
                item.id,
                item.title,
                points,
                _integer_tuple(item.custom_fields.get("prerequisite_work_item_ids")),
                source_ref,
            )
        )
    available_hours = (
        Decimal(0)
        if capacity_per_day is None
        else capacity_per_day * Decimal(total_business_days)
    )
    completed = _completed_points_from_snapshots(
        sprint_snapshot_rows,
        current_iteration_path=iteration_path,
        team_name=team_name,
        iteration_id=iteration_id,
    )
    artifact = plan_sprint(
        iteration_path=iteration_path,
        backlog=tuple(backlog),
        capacity=SprintCapacityInput(available_hours, capacity_source_ref),
        completed_sprints=completed,
    )
    return replace(artifact, omitted_work_item_ids=tuple(sorted(omitted)))


def _completed_points_from_snapshots(
    rows: Iterable[dict[str, Any]],
    *,
    current_iteration_path: str,
    team_name: str | None,
    iteration_id: str | None,
) -> tuple[SprintCompletedPointsInput, ...]:
    del iteration_id
    latest_by_sprint_item: dict[tuple[str, int], tuple[int, Decimal]] = {}
    for row in rows:
        iteration = str(row.get("IterationPath") or "").strip()
        if not iteration or iteration.casefold() == current_iteration_path.casefold():
            continue
        item_id = _int_value(row.get("WorkItemId"))
        date_sk = _int_value(row.get("DateSK"))
        state = str(row.get("State") or "").strip().lower()
        points = _decimal_value(row.get("StoryPoints"))
        if points is None:
            points = _decimal_value(row.get("Effort"))
        if item_id is None or date_sk is None or points is None or state not in TERMINAL_WORK_ITEM_STATES:
            continue
        key = (iteration, item_id)
        previous = latest_by_sprint_item.get(key)
        if previous is None or date_sk >= previous[0]:
            latest_by_sprint_item[key] = (date_sk, points)
    totals: dict[str, Decimal] = {}
    for (iteration, _item_id), (_date_sk, points) in latest_by_sprint_item.items():
        totals[iteration] = totals.get(iteration, Decimal(0)) + points
    label = team_name or "project"
    return tuple(
        SprintCompletedPointsInput(
            iteration,
            points,
            (f"ADO:sprint-points:{label}:{iteration}",),
        )
        for iteration, points in sorted(totals.items(), key=lambda entry: entry[0].casefold())
    )


def _average_completed_points(
    samples: tuple[SprintCompletedPointsInput, ...],
) -> Decimal:
    return sum((sample.story_points for sample in samples), Decimal(0)) / Decimal(len(samples))


def _assert_acyclic(rows: tuple[tuple[int, tuple[int, ...]], ...]) -> None:
    ids = {row[0] for row in rows}
    graph = {item_id: tuple(dep for dep in dependencies if dep in ids) for item_id, dependencies in rows}
    visiting: set[int] = set()
    visited: set[int] = set()

    def visit(item_id: int) -> None:
        if item_id in visited:
            return
        if item_id in visiting:
            raise ConfigError(f"Sprint dependency cycle detected at WI:{item_id}.")
        visiting.add(item_id)
        for prerequisite in graph[item_id]:
            visit(prerequisite)
        visiting.remove(item_id)
        visited.add(item_id)

    for item_id in graph:
        visit(item_id)


def _topological_backlog(
    backlog: tuple[SprintBacklogItem, ...],
) -> tuple[SprintBacklogItem, ...]:
    items_by_id = {item.work_item_id: item for item in backlog}
    ordered: list[SprintBacklogItem] = []
    remaining = list(backlog)
    completed: set[int] = set()
    while remaining:
        ready = next(
            (
                item
                for item in remaining
                if all(
                    prerequisite not in items_by_id or prerequisite in completed
                    for prerequisite in item.prerequisite_item_ids
                )
            ),
            None,
        )
        if ready is None:
            raise ConfigError("Sprint dependency graph could not be ordered.")
        ordered.append(ready)
        remaining.remove(ready)
        completed.add(ready.work_item_id)
    return tuple(ordered)


def _decimal_value(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _int_value(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _integer_tuple(value: Any) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(
        parsed
        for raw in value
        if (parsed := _int_value(raw)) is not None
    )
