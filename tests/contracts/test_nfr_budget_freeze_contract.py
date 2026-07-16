"""arch-fix.md Phase 0 (§8/§A.0): NFR/OpEx budget freeze contract.

`governance/nfr-budgets.yaml` seeds candidate contracts from arch-fix.md's
§8 table. Ratification (candidate -> ratified) is a human/PM decision. This
test's `_RATIFIED_FLOOR` snapshot must be updated in the SAME change that
ratifies a budget, and a later change may only tighten (never loosen) a
ratified value — "no later phase may lower a budget to pass" (arch-fix.md
§A.0).

ADR-0014 (governance/decisions/0014-scale-budget-ratification.md) ratified
the first 12 budgets on 2026-07-13.
"""
from __future__ import annotations

from src.core.nfr_budgets import load_nfr_budgets, ratified_budgets

# Snapshot of the last-known-good value for every RATIFIED budget, keyed by
# id. Qualitative-direction budgets (no ceiling/floor number) are exempt —
# see test_ratified_ceiling_budgets_never_loosen/test_ratified_floor_budgets_never_loosen.
_RATIFIED_FLOOR: dict[str, float] = {
    "audit-write-latency-local": 25,
    "audit-write-latency-network-lease": 150,
    "context-compile-latency": 300,
    "local-encode-latency": 80,
    "startup-regression": 150,
    "kusto-required-set-gather-latency": 450,
    "capacity-snapshot-cadence-events": 5000,
    "capacity-trace-metadata-retention": 90,
    "capacity-sanitized-excerpt-retention": 90,
    "compatibility-schema-version-window": 2,
}


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


def test_ratified_set_matches_adr_0014() -> None:
    # ADR-0014 ratified exactly these ids on 2026-07-13. Update this test
    # (and _RATIFIED_FLOOR above, and the ADR) in the same change that
    # ratifies or unratifies a budget.
    expected_ratified = {
        "audit-write-latency-local",
        "audit-write-latency-network-lease",
        "context-compile-latency",
        "local-encode-latency",
        "startup-regression",
        "kusto-required-set-gather-latency",
        "capacity-snapshot-cadence-events",
        "capacity-trace-metadata-retention",
        "capacity-sanitized-excerpt-retention",
        "reliability-rpo-authorization-execution",
        "security-encryption-at-rest",
        "compatibility-schema-version-window",
        "operations-accountable-role",
        "privacy-retention-rtbf",
    }
    actual_ratified = {b.id for b in ratified_budgets()}
    assert actual_ratified == expected_ratified, (
        f"ratified set drifted from ADR-0014: missing={expected_ratified - actual_ratified}, "
        f"unexpected={actual_ratified - expected_ratified}"
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
