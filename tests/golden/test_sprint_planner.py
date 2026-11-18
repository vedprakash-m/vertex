from __future__ import annotations

import pytest

from src.core.sprint_planner import (
    BacklogItem,
    plan_sprint,
    render_sprint_plan_summary,
)


def _item(work_item_id: int, points: float | None, priority: int | None = 2, preds: tuple[int, ...] = ()) -> BacklogItem:
    return BacklogItem(
        work_item_id=work_item_id,
        title=f"Item {work_item_id}",
        priority=priority,
        story_points=points,
        predecessor_ids=preds,
    )


def _ids(items) -> list[int]:
    return [item.work_item_id for item in items]


def test_capacity_is_median_of_last_three_completed_sprints() -> None:
    plan = plan_sprint([], completed_points_history=(100.0, 8.0, 13.0, 10.0))
    assert plan.capacity_points == 10.0


def test_fewer_than_three_sprints_leaves_capacity_unknown_and_plans_nothing() -> None:
    plan = plan_sprint([_item(1, 1.0)], completed_points_history=(10.0, 12.0))
    assert plan.capacity_points is None
    assert plan.planned == ()
    assert _ids(plan.over_capacity) == [1]


def test_greedy_fill_in_priority_then_id_order_skips_items_that_do_not_fit() -> None:
    backlog = [
        _item(30, 5.0, priority=2),
        _item(10, 8.0, priority=1),
        _item(20, 2.0, priority=2),
        _item(40, 1.0, priority=None),
    ]
    plan = plan_sprint(backlog, completed_points_history=(11.0, 11.0, 11.0))
    assert _ids(plan.planned) == [10, 20, 40]
    assert plan.planned_points == 11.0
    assert _ids(plan.over_capacity) == [30]


def test_ordering_is_independent_of_input_order() -> None:
    backlog = [_item(i, 1.0, priority=1 + i % 3) for i in range(1, 12)]
    forward = plan_sprint(backlog, completed_points_history=(5.0, 5.0, 5.0))
    reverse = plan_sprint(list(reversed(backlog)), completed_points_history=(5.0, 5.0, 5.0))
    assert forward == reverse


def test_unestimated_items_are_listed_and_excluded() -> None:
    plan = plan_sprint([_item(1, None), _item(2, 3.0)], completed_points_history=(5.0, 5.0, 5.0))
    assert _ids(plan.unestimated) == [1]
    assert _ids(plan.planned) == [2]


def test_predecessor_done_or_planned_earlier_allows_planning() -> None:
    backlog = [_item(1, 2.0, priority=1), _item(2, 2.0, priority=2, preds=(1, 99))]
    plan = plan_sprint(backlog, completed_points_history=(5.0, 5.0, 5.0), done_ids=frozenset({99}))
    assert _ids(plan.planned) == [1, 2]
    assert plan.blocked == ()


def test_predecessor_planned_later_or_missing_blocks_item() -> None:
    backlog = [
        _item(1, 1.0, priority=1, preds=(2,)),  # predecessor ranks later
        _item(2, 1.0, priority=2),
        _item(3, 1.0, priority=2, preds=(77,)),  # unknown, not done
        _item(4, 1.0, priority=3, preds=(5,)),  # predecessor unestimated
        _item(5, None, priority=3),
    ]
    plan = plan_sprint(backlog, completed_points_history=(10.0, 10.0, 10.0))
    assert _ids(plan.planned) == [2]
    assert [(b.item.work_item_id, b.unmet_predecessor_ids) for b in plan.blocked] == [
        (1, (2,)),
        (3, (77,)),
        (4, (5,)),
    ]


def test_dependency_cycle_is_blocked_not_planned() -> None:
    backlog = [_item(1, 1.0, preds=(2,)), _item(2, 1.0, preds=(1,))]
    plan = plan_sprint(backlog, completed_points_history=(10.0, 10.0, 10.0))
    assert plan.planned == ()
    assert _ids(b.item for b in plan.blocked) == [1, 2]


def test_invalid_inputs_are_rejected() -> None:
    with pytest.raises(ValueError):
        plan_sprint([_item(1, 1.0), _item(1, 2.0)], completed_points_history=(5.0, 5.0, 5.0))
    with pytest.raises(ValueError):
        plan_sprint([_item(1, -1.0)], completed_points_history=(5.0, 5.0, 5.0))
    with pytest.raises(ValueError):
        plan_sprint([], completed_points_history=(5.0, -5.0, 5.0))


def test_summary_states_estimate_unknown_overcommit_wip_and_greedy_caveat() -> None:
    backlog = [_item(1, 3.0, priority=1), _item(2, None), _item(3, 1.0, preds=(9,))]
    plan = plan_sprint(backlog, completed_points_history=(4.0, 6.0, 5.0), wip_count=7)
    text = render_sprint_plan_summary(plan)
    assert "Estimated capacity: 5 story points (median completed, last 3 sprints)" in text
    assert "Planned: 3 of 5 estimated story points (1 item)" in text
    assert "Overcommit probability: Unknown" in text
    assert "Current WIP: 7 (displayed, not enforced)" in text
    assert "Greedy, not optimal" in text
    assert "#2 Item 2" in text
    assert "#3 Item 3 (waiting on #9)" in text


def test_summary_with_unknown_capacity_and_wip() -> None:
    text = render_sprint_plan_summary(plan_sprint([], completed_points_history=()))
    assert "Estimated capacity: Unknown (fewer than 3 completed sprints)" in text
    assert "Current WIP: Unknown" in text
