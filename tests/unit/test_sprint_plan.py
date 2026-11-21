from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from src.core.exceptions import ConfigError
from src.core.models import RiskLevel, WorkItem
from src.core.sprint_plan import (
    SprintBacklogItem,
    SprintCapacityInput,
    SprintCompletedPointsInput,
    build_ado_sprint_plan,
    plan_sprint,
)


def test_planner_uses_explicit_completed_story_points_and_capacity_gate() -> None:
    artifact = plan_sprint(
        iteration_path="Program\\Sprint 4",
        backlog=(
            SprintBacklogItem(3, "Unblocked first", Decimal("3"), (), "WI:3"),
            SprintBacklogItem(2, "Second", Decimal("4"), (3,), "WI:2"),
            SprintBacklogItem(1, "Does not fit", Decimal("2"), (), "WI:1"),
        ),
        capacity=SprintCapacityInput(
            available_hours=Decimal("80"),
            source_ref="ADO:capacity:team:iteration",
        ),
        completed_sprints=(
            SprintCompletedPointsInput("Sprint 1", Decimal("5"), ("ADO:analytics:Sprint 1",)),
            SprintCompletedPointsInput("Sprint 2", Decimal("8"), ("ADO:analytics:Sprint 2",)),
        ),
    )

    assert artifact.point_capacity == Decimal("6.5")
    assert tuple(entry.work_item_id for entry in artifact.selected) == (3, 1)
    assert artifact.selected_story_points == Decimal("5")
    assert artifact.remaining_story_points == Decimal("1.5")
    assert tuple(entry.work_item_id for entry in artifact.deferred) == (2,)
    assert artifact.deferred[0].reason == "over_point_capacity"
    assert artifact.capacity_source_ref == "ADO:capacity:team:iteration"
    assert artifact.completed_sprint_refs == (
        "ADO:analytics:Sprint 1",
        "ADO:analytics:Sprint 2",
    )


def test_planner_does_not_substitute_item_throughput_or_invent_history() -> None:
    artifact = plan_sprint(
        iteration_path="Program\\Sprint 4",
        backlog=(SprintBacklogItem(7, "Explicit backlog", Decimal("3"), (), "WI:7"),),
        capacity=SprintCapacityInput(Decimal("40"), "ADO:capacity:team:iteration"),
        completed_sprints=(),
    )

    assert artifact.point_capacity is None
    assert artifact.selected == ()
    assert artifact.deferred[0].reason == "no_completed_sprint_story_points"


def test_planner_refuses_zero_capacity_even_with_completed_points() -> None:
    artifact = plan_sprint(
        iteration_path="Program\\Sprint 4",
        backlog=(SprintBacklogItem(7, "Explicit backlog", Decimal("3"), (), "WI:7"),),
        capacity=SprintCapacityInput(Decimal("0"), "ADO:capacity:team:iteration"),
        completed_sprints=(
            SprintCompletedPointsInput("Sprint 3", Decimal("10"), ("ADO:analytics:Sprint 3",)),
        ),
    )

    assert artifact.point_capacity == Decimal("0")
    assert artifact.selected == ()
    assert artifact.deferred[0].reason == "zero_capacity"


def test_planner_topologically_orders_backlog_and_blocks_unresolved_dependencies() -> None:
    artifact = plan_sprint(
        iteration_path="Program\\Sprint 4",
        backlog=(
            SprintBacklogItem(10, "Depends on external", Decimal("2"), (99,), "WI:10"),
            SprintBacklogItem(11, "Depends on earlier", Decimal("2"), (12,), "WI:11"),
            SprintBacklogItem(12, "Earlier", Decimal("2"), (), "WI:12"),
        ),
        capacity=SprintCapacityInput(Decimal("80"), "ADO:capacity:team:iteration"),
        completed_sprints=(
            SprintCompletedPointsInput("Sprint 3", Decimal("10"), ("ADO:analytics:Sprint 3",)),
        ),
    )

    assert tuple(entry.work_item_id for entry in artifact.selected) == (12, 11)
    assert {
        entry.work_item_id: entry.reason
        for entry in artifact.deferred
    } == {
        10: "external_prerequisite",
    }


def test_planner_rejects_dependency_cycles_instead_of_publishing_partial_order() -> None:
    with pytest.raises(ConfigError, match="Sprint dependency cycle"):
        plan_sprint(
            iteration_path="Program\\Sprint 4",
            backlog=(
                SprintBacklogItem(1, "A", Decimal("2"), (2,), "WI:1"),
                SprintBacklogItem(2, "B", Decimal("2"), (1,), "WI:2"),
            ),
            capacity=SprintCapacityInput(Decimal("80"), "ADO:capacity:team:iteration"),
            completed_sprints=(
                SprintCompletedPointsInput("Sprint 3", Decimal("10"), ("ADO:analytics:Sprint 3",)),
            ),
        )


def _item(item_id: int, *, state: str, points: str, iteration_path: str) -> WorkItem:
    return WorkItem(
        id=item_id,
        type="Product Backlog Item",
        title=f"Item {item_id}",
        state=state,
        assigned_to=None,
        assigned_to_email=None,
        area_path="Program",
        iteration_path=iteration_path,
        target_date=date(2026, 12, 31),
        risk_level=RiskLevel.LOW,
        tags=[],
        custom_fields={
            "story_points": points,
            "story_points_source": f"ADO:WI:{item_id}:Microsoft.VSTS.Scheduling.StoryPoints",
        },
    )


def test_ado_adapter_uses_only_attributable_item_and_snapshot_story_points() -> None:
    artifact = build_ado_sprint_plan(
        iteration_path="Program\\Sprint 4",
        items=(
            _item(1, state="Active", points="5", iteration_path="Program\\Sprint 4"),
            _item(2, state="Active", points="3", iteration_path="Program\\Sprint 4"),
            WorkItem(
                id=3,
                type="Task",
                title="No explicit points",
                state="Active",
                assigned_to=None,
                assigned_to_email=None,
                area_path="Program",
                iteration_path="Program\\Sprint 4",
                target_date=None,
                risk_level=RiskLevel.LOW,
                tags=[],
                custom_fields={},
            ),
        ),
        capacity_per_day=Decimal("8"),
        total_business_days=10,
        capacity_source_ref="ADO:capacity:team:iteration",
        sprint_snapshot_rows=[
            {
                "DateSK": 20261001,
                "WorkItemId": 90,
                "State": "Closed",
                "IterationPath": "Program\\Sprint 1",
                "StoryPoints": 7,
            },
            {
                "DateSK": 20261002,
                "WorkItemId": 90,
                "State": "Closed",
                "IterationPath": "Program\\Sprint 1",
                "StoryPoints": 7,
            },
            {
                "DateSK": 20261015,
                "WorkItemId": 91,
                "State": "Closed",
                "IterationPath": "Program\\Sprint 2",
                "Effort": 9,
            },
            {
                "DateSK": 20261016,
                "WorkItemId": 92,
                "State": "Closed",
                "IterationPath": "Program\\Sprint 3",
                "StoryPoints": None,
            },
        ],
        team_name="Team A",
        iteration_id="iteration-4",
    )

    assert artifact.point_capacity == Decimal("8")
    assert tuple(entry.work_item_id for entry in artifact.selected) == (1, 2)
    assert artifact.omitted_work_item_ids == (3,)
    assert tuple(sample.iteration_path for sample in artifact.completed_sprints) == (
        "Program\\Sprint 1",
        "Program\\Sprint 2",
    )
    assert artifact.completed_sprint_refs == (
        "ADO:sprint-points:Team A:Program\\Sprint 1",
        "ADO:sprint-points:Team A:Program\\Sprint 2",
    )
