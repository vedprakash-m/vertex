"""ADF-W0.4 (specs/arch-data-fix.md): ratified per-class quality floors and
the Wilson-based denominator/sample-plan they imply.

Ratified by ADR-0016 (`governance/decisions/0016-adf-w04-sample-plan-floors.md`,
2026-07-13) via a live decision with the Platform DRI. This module does not
reimplement Wilson-interval math -- it reuses the existing z=1.96 (95% CI)
implementation in `src.core.rev.quality_metrics`, per the spec's explicit
instruction not to reuse the *90%*-CI helper in `entity_binding_gate.py`
unchanged for ADF's own certification.

Two-tier design (ADR-0016 Decision 1): a lower ADVISORY floor governs
single-program advisory authority (ADF-OM3A); a stricter FLEET floor governs
>=3-program fleet certification (ADF-OM3B). The two floors are deliberately
different for critical-family recall; every other floor here is one number
used at both tiers.

Zone A -- no AI or M365 imports.
"""
from __future__ import annotations

from src.core.rev.quality_metrics import (
    WilsonDenominatorRequirement,
    minimum_successes_for_wilson_floor,
    minimum_total_for_perfect_wilson_floor,
    wilson_ci,
)

# Precision floor: matches ADF-OM3B's "lower Wilson 95% precision bound
# >=0.95" and the pre-existing entity_binding_gate.py PRECISION_FLOOR -- one
# number, no advisory/fleet split needed (both tiers already agree).
ADF_PRECISION_FLOOR = 0.95

# Critical-family recall: two-tier per ADR-0016 Decision 1.
ADF_ADVISORY_CRITICAL_RECALL_FLOOR = 0.60  # matches G_CRITICAL_FAMILY_RECALL_FLOOR
ADF_FLEET_CRITICAL_RECALL_FLOOR = 0.90     # matches ADF-OM3B

# Coverage floor: ADR-0016 Decision 2 -- reuses entity_binding_gate.py's
# COVERAGE_FLOOR for cross-domain consistency (entity-binding, risk,
# dependency classes all share one number rather than a bespoke per-domain
# value).
ADF_COVERAGE_FLOOR = 0.80

# Abstention floor: ADR-0016 Decision 3 -- promotes quality_metrics.py's
# previously reported-only "abstention coverage" metric (fraction of
# labeled rows with a matched staged candidate) to a gated floor. At least
# this fraction of true candidates must be staged; the rest is the maximum
# tolerated silent-drop rate before a human ever reviews anything.
ADF_ABSTENTION_FLOOR = 0.90


def adf_denominator_plan(*, data_floor: int = 30) -> tuple[WilsonDenominatorRequirement, ...]:
    """Minimum-denominator guidance for every ADF-ratified floor.

    Reuses the existing z=1.96 Wilson primitives in `quality_metrics.py`
    rather than reimplementing them. `data_floor` is the assumed corpus size
    to report `min_successes_at_data_floor` against (default 30, matching
    `quality_metrics.py::activation_denominator_plan`'s own default).
    """
    specs = (
        ("adf_precision_ci_low", ADF_PRECISION_FLOOR),
        ("adf_advisory_critical_recall_ci_low", ADF_ADVISORY_CRITICAL_RECALL_FLOOR),
        ("adf_fleet_critical_recall_ci_low", ADF_FLEET_CRITICAL_RECALL_FLOOR),
        ("adf_coverage_ci_low", ADF_COVERAGE_FLOOR),
        ("adf_abstention_ci_low", ADF_ABSTENTION_FLOOR),
    )
    return tuple(
        WilsonDenominatorRequirement(
            metric=metric,
            floor=floor,
            min_total_if_perfect=minimum_total_for_perfect_wilson_floor(floor=floor),
            min_successes_at_data_floor=minimum_successes_for_wilson_floor(total=data_floor, floor=floor),
            data_floor=data_floor,
            ci_low_at_data_floor_if_perfect=wilson_ci(data_floor, data_floor)[0],
        )
        for metric, floor in specs
    )


__all__ = [
    "ADF_PRECISION_FLOOR",
    "ADF_ADVISORY_CRITICAL_RECALL_FLOOR",
    "ADF_FLEET_CRITICAL_RECALL_FLOOR",
    "ADF_COVERAGE_FLOOR",
    "ADF_ABSTENTION_FLOOR",
    "adf_denominator_plan",
]
