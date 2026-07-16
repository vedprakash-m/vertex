"""ADF-W0.4: ratified quality-floor constants + denominator plan."""
from __future__ import annotations

from src.core.adf_quality_floors import (
    ADF_ABSTENTION_FLOOR,
    ADF_ADVISORY_CRITICAL_RECALL_FLOOR,
    ADF_COVERAGE_FLOOR,
    ADF_FLEET_CRITICAL_RECALL_FLOOR,
    ADF_PRECISION_FLOOR,
    adf_denominator_plan,
)
from src.core.rev.quality_metrics import (
    G_CRITICAL_FAMILY_RECALL_FLOOR,
    wilson_ci,
)


def test_precision_floor_matches_om3b() -> None:
    assert ADF_PRECISION_FLOOR == 0.95


def test_advisory_critical_recall_matches_existing_code_floor() -> None:
    # ADR-0016 Decision 1: the advisory tier deliberately matches the
    # pre-existing quality_metrics.py floor, not a new number.
    assert ADF_ADVISORY_CRITICAL_RECALL_FLOOR == G_CRITICAL_FAMILY_RECALL_FLOOR


def test_fleet_critical_recall_is_stricter_than_advisory() -> None:
    assert ADF_FLEET_CRITICAL_RECALL_FLOOR > ADF_ADVISORY_CRITICAL_RECALL_FLOOR
    assert ADF_FLEET_CRITICAL_RECALL_FLOOR == 0.90


def test_coverage_floor_matches_entity_binding_precedent() -> None:
    assert ADF_COVERAGE_FLOOR == 0.80


def test_abstention_floor_is_gated_not_reported_only() -> None:
    assert ADF_ABSTENTION_FLOOR == 0.90


def test_denominator_plan_covers_every_ratified_floor() -> None:
    plan = adf_denominator_plan()
    metrics = {req.metric for req in plan}
    assert metrics == {
        "adf_precision_ci_low",
        "adf_advisory_critical_recall_ci_low",
        "adf_fleet_critical_recall_ci_low",
        "adf_coverage_ci_low",
        "adf_abstention_ci_low",
    }


def test_denominator_plan_min_total_is_internally_consistent() -> None:
    # For every requirement, N-out-of-N at min_total_if_perfect must clear
    # the floor (that's the definition of min_total_if_perfect).
    for req in adf_denominator_plan():
        lower, _ = wilson_ci(req.min_total_if_perfect, req.min_total_if_perfect)
        assert lower >= req.floor, (
            f"{req.metric}: N={req.min_total_if_perfect} does not actually clear floor {req.floor}"
        )


def test_stricter_floor_requires_larger_denominator() -> None:
    plan = {req.metric: req for req in adf_denominator_plan()}
    # 0.90 fleet recall must require a larger perfect-N than 0.60 advisory recall.
    assert (
        plan["adf_fleet_critical_recall_ci_low"].min_total_if_perfect
        > plan["adf_advisory_critical_recall_ci_low"].min_total_if_perfect
    )
