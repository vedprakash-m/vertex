"""arch-fix.md Phase 0 (§8/§A.0): NFR/OpEx budget freeze contract.

`governance/nfr-budgets.yaml` seeds candidate contracts from arch-fix.md's
§8 table. Ratification (candidate -> ratified) is a human/PM decision — no
budget in this file is ratified yet. Once one is, this test's
`_RATIFIED_FLOOR` snapshot must be updated in the SAME change that ratifies
it, and a later change may only tighten (never loosen) a ratified value —
"no later phase may lower a budget to pass" (arch-fix.md §A.0).
"""
from __future__ import annotations

from src.core.nfr_budgets import load_nfr_budgets, ratified_budgets

# Snapshot of the last-known-good value for every RATIFIED budget, keyed by
# id. Empty today because nothing has been ratified — the moment an entry's
# `status` flips to `ratified` in governance/nfr-budgets.yaml, add its id +
# value here in the same commit.
_RATIFIED_FLOOR: dict[str, float] = {}


def test_registry_loads_and_validates() -> None:
    budgets = load_nfr_budgets()
    assert len(budgets) > 0
    ids = [b.id for b in budgets]
    assert len(ids) == len(set(ids)), "duplicate budget ids"


def test_every_area_from_spec_table_is_present() -> None:
    budgets = load_nfr_budgets()
    areas = {b.area for b in budgets}
    expected = {
        "Audit write",
        "Context compile",
        "Local encode",
        "Startup",
        "Capacity",
        "Concurrency",
        "Reliability",
        "Security",
        "Privacy",
        "Observability",
        "OpEx",
        "Compatibility",
        "Operations",
    }
    missing = expected - areas
    assert not missing, f"nfr-budgets.yaml is missing area(s) from arch-fix.md §8: {sorted(missing)}"


def test_nothing_is_ratified_yet() -> None:
    # Documents current state; update this test (and _RATIFIED_FLOOR above)
    # the moment a real ratification happens.
    assert ratified_budgets() == (), (
        "a budget was ratified without updating this test's _RATIFIED_FLOOR snapshot"
    )


def test_ratified_ceiling_budgets_never_loosen() -> None:
    for budget in ratified_budgets():
        if budget.direction != "ceiling" or budget.value is None:
            continue
        floor = _RATIFIED_FLOOR.get(budget.id)
        assert floor is not None, f"ratified ceiling budget {budget.id!r} has no recorded floor to check against"
        assert budget.value <= floor, (
            f"ratified ceiling budget {budget.id!r} was loosened from {floor} to {budget.value} — "
            "not allowed once ratified (arch-fix.md §A.0)"
        )


def test_ratified_floor_budgets_never_loosen() -> None:
    for budget in ratified_budgets():
        if budget.direction != "floor" or budget.value is None:
            continue
        floor = _RATIFIED_FLOOR.get(budget.id)
        assert floor is not None, f"ratified floor budget {budget.id!r} has no recorded floor to check against"
        assert budget.value >= floor, (
            f"ratified floor budget {budget.id!r} was loosened from {floor} to {budget.value} — "
            "not allowed once ratified (arch-fix.md §A.0)"
        )
