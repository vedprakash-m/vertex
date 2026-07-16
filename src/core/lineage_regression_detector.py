"""ADF-W5.8 (specs/arch-data-fix.md Section 8.2.5): ``lineage_regression`` alert.

Section 8.2.5 lists ``lineage_regression`` as one of the two alert categories
whose v1.31 status row named "zero existing detection logic anywhere... a
coverage-history store... `coverage_ratio` is computed ephemerally and never
persisted, so there is nothing to regress against." ``projection_lag``
(v1.44) closed the first of the two by comparing a snapshot's own timestamp
against its underlying artifacts; this module closes the second by comparing
a snapshot's :attr:`~src.core.cockpit_models.IntelligenceCockpitSummary.
lineage_coverage` against the *retained cockpit history* the cockpit already
persists on every build (Section 9.7) -- the lowest-cost unblock the v1.44
status row itself named, since ``find_nearest_history_snapshot`` already
exists and lineage coverage is already a field on every persisted snapshot.

``detect_lineage_regression`` is a pure comparison: no regression when there
is no prior snapshot to compare against (a program's first cockpit run has
nothing to regress from), and no regression when either side never computed
a coverage ratio (Section 8.14.2: a program with zero active facts has a
``None`` ratio, not a zero-coverage one -- comparing against/from ``None``
would be a fabricated verdict, not a real one).

Zone A -- pure dataclasses and arithmetic; no filesystem/AI/M365 access. The
caller (cockpit show/build) supplies both coverage values, already resolved
from the current and the nearest-prior-history ``CockpitSnapshot``.
"""
from __future__ import annotations

from dataclasses import dataclass

#: A coverage drop of more than this many percentage points (a 0..1
#: fraction) between consecutive retained cockpit snapshots is a regression
#: worth surfacing -- Section 8.2.5's "lineage regression". Deliberately
#: generous: the coverage ratio is a rolling average over the whole active
#: fact set and legitimately drifts a little as new, not-yet-lineaged facts
#: land normally (Section 8.14.2's own three-way lineaged/waived/defect
#: split expects gradual backfill, not instant full coverage). This catches
#: a real backslide -- a reverted lineage-population change, a bulk
#: unlineaged import, or an expired waiver reclassifying facts as defects --
#: not routine noise.
DEFAULT_MAX_DROP = 0.05


@dataclass(frozen=True, slots=True)
class LineageRegressionFinding:
    """The result of comparing one snapshot's lineage coverage against the
    nearest prior retained snapshot's.

    ``is_regressing`` is True iff both coverage values are known and the
    drop from ``previous_coverage`` to ``current_coverage`` exceeds
    ``max_drop``. When either coverage value is ``None`` (nothing to compare
    against, or nothing computable yet), ``is_regressing`` is always False --
    absence of data is never treated as a regression.
    """

    is_regressing: bool
    previous_coverage: float | None
    current_coverage: float | None
    drop: float | None  # previous - current; only meaningful when both are known
    max_drop: float
    detail: str


def detect_lineage_regression(
    *,
    previous_coverage: float | None,
    current_coverage: float | None,
    max_drop: float = DEFAULT_MAX_DROP,
) -> LineageRegressionFinding:
    """Compare two coverage ratios. Pure function -- no I/O, no defaults
    resolved from disk; the caller already looked up both values."""
    if previous_coverage is None:
        return LineageRegressionFinding(
            is_regressing=False,
            previous_coverage=None,
            current_coverage=current_coverage,
            drop=None,
            max_drop=max_drop,
            detail="No prior retained cockpit snapshot to compare lineage coverage against.",
        )
    if current_coverage is None:
        return LineageRegressionFinding(
            is_regressing=False,
            previous_coverage=previous_coverage,
            current_coverage=None,
            drop=None,
            max_drop=max_drop,
            detail="Current lineage coverage is not computable (no active facts); nothing to compare.",
        )

    drop = previous_coverage - current_coverage
    # Float subtraction of two ratios (e.g. 0.80 - 0.75) can land a hair
    # above an exact-decimal budget (0.05000000000000004) purely from binary
    # floating-point representation, not a real regression -- a tiny epsilon
    # keeps the boundary decision matching the decimal values an operator
    # actually configured or read off the cockpit.
    if drop <= max_drop + 1e-9:
        direction = "up" if drop < 0 else "down"
        return LineageRegressionFinding(
            is_regressing=False,
            previous_coverage=previous_coverage,
            current_coverage=current_coverage,
            drop=drop,
            max_drop=max_drop,
            detail=(
                f"Lineage coverage {previous_coverage:.1%} -> {current_coverage:.1%} "
                f"({direction} {abs(drop):.1%}); within the {max_drop:.0%} budget."
            ),
        )

    return LineageRegressionFinding(
        is_regressing=True,
        previous_coverage=previous_coverage,
        current_coverage=current_coverage,
        drop=drop,
        max_drop=max_drop,
        detail=(
            f"Lineage coverage regressed from {previous_coverage:.1%} to {current_coverage:.1%} "
            f"(down {drop:.1%}) -- over the {max_drop:.0%} budget. Check for a reverted "
            "lineage-population change, a bulk unlineaged fact import, or an expired waiver."
        ),
    )


def build_lineage_regression_alert_message(finding: LineageRegressionFinding) -> tuple[str, str]:
    """Return ``(message, next_command)`` for a regressing finding.

    Kept separate from the alert-emission call so the detector stays a pure
    comparison with no alert-store dependency, matching
    ``projection_lag_detector.build_projection_lag_alert_message``.
    """
    assert finding.is_regressing and finding.previous_coverage is not None and finding.current_coverage is not None
    message = (
        f"Lineage regression: coverage dropped from {finding.previous_coverage:.1%} to "
        f"{finding.current_coverage:.1%} (down {finding.drop:.1%}), over the {finding.max_drop:.0%} budget."
    )
    next_command = "vertex doctor --storage --edition {program}  # inspect lineage waivers/defects"
    return message, next_command


__all__ = [
    "DEFAULT_MAX_DROP",
    "LineageRegressionFinding",
    "build_lineage_regression_alert_message",
    "detect_lineage_regression",
]
