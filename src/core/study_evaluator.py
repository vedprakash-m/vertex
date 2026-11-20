"""RELO-W6.1 / W6.2 matched-cycle study & Decision M agreement evaluator (Zone A pure).

Implements the mathematical and protocol acceptance predicates specified in
specs/re-re-vertex.md §7 and specs/re-vertex.md §0.6:
- Decision M (`W6.1`): Stratified dual-label audit with minimum 60 items per
  stratum; >=2 divergences pause model-assisted labeling for that stratum and
  expand required re-verification sample size to 120.
- Decision L (`W6.2`): Minimum 8 matched weekly operating cycles spanning >=49
  calendar days, with one-sided 95% lower confidence bound (LCB) on
  quality-adjusted time saved exceeding 80% of the 420-minute baseline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math

_DEFAULT_MIN_STRATUM_SAMPLES = 60
_DEFAULT_EXPANDED_STRATUM_SAMPLES = 120
_DEFAULT_DIVERGENCE_PAUSE_THRESHOLD = 2
_DEFAULT_BASELINE_MINUTES = 420.0
_DEFAULT_MIN_CYCLES = 8
_DEFAULT_MIN_CALENDAR_DAYS = 49
_DEFAULT_LCB_THRESHOLD = 0.80

# Exact one-sided 95% Student-t critical values (t_{0.95, df}) for df = 1..30.
_STUDENT_T_95_ONE_SIDED: dict[int, float] = {
    1: 6.313752,
    2: 2.919986,
    3: 2.353363,
    4: 2.131847,
    5: 2.015048,
    6: 1.943180,
    7: 1.894579,
    8: 1.859548,
    9: 1.833113,
    10: 1.812461,
    11: 1.795885,
    12: 1.782288,
    13: 1.770933,
    14: 1.761310,
    15: 1.753050,
    16: 1.745884,
    17: 1.739607,
    18: 1.734064,
    19: 1.729133,
    20: 1.724718,
    21: 1.720743,
    22: 1.717144,
    23: 1.713872,
    24: 1.710882,
    25: 1.708141,
    26: 1.705618,
    27: 1.703288,
    28: 1.701131,
    29: 1.699127,
    30: 1.697261,
}
_NORMAL_Z_95_ONE_SIDED = 1.644854


@dataclass(frozen=True, slots=True)
class StratumSample:
    stratum_id: str
    sample_count: int
    divergence_count: int
    reverified_after_pause: bool = False


@dataclass(frozen=True, slots=True)
class StratumAgreementResult:
    stratum_id: str
    sample_count: int
    divergence_count: int
    required_samples: int
    paused_for_recheck: bool
    approved: bool
    reason: str


@dataclass(frozen=True, slots=True)
class MatchedWeeklyCycle:
    cycle_id: str
    week_start: date
    assisted_minutes: float
    quality_passed: bool
    baseline_minutes: float = _DEFAULT_BASELINE_MINUTES
    quality_adjusted_minutes_saved: float | None = None


@dataclass(frozen=True, slots=True)
class StudyEvaluationResult:
    accepted: bool
    cycle_count: int
    elapsed_calendar_days: int
    mean_saved_fraction: float
    lcb_95_saved_fraction: float | None
    stratum_results: tuple[StratumAgreementResult, ...]
    blocking_reasons: tuple[str, ...]


def evaluate_stratum_agreement(
    samples: tuple[StratumSample, ...],
    *,
    min_samples: int = _DEFAULT_MIN_STRATUM_SAMPLES,
    expanded_samples: int = _DEFAULT_EXPANDED_STRATUM_SAMPLES,
    divergence_pause_threshold: int = _DEFAULT_DIVERGENCE_PAUSE_THRESHOLD,
) -> tuple[StratumAgreementResult, ...]:
    """Evaluate Decision M stratified dual-label agreement per section stratum."""
    results: list[StratumAgreementResult] = []
    for sample in samples:
        if sample.sample_count < 0 or sample.divergence_count < 0:
            raise ValueError(f"Negative sample or divergence count in stratum {sample.stratum_id!r}.")
        if sample.divergence_count > sample.sample_count:
            raise ValueError(f"Divergence count cannot exceed sample count in stratum {sample.stratum_id!r}.")
        if sample.divergence_count >= divergence_pause_threshold:
            approved = sample.sample_count >= expanded_samples and sample.reverified_after_pause
            reason = (
                f"Stratum {sample.stratum_id!r} passed expanded {expanded_samples}-item re-verification after divergence pause."
                if approved
                else (
                    f"Stratum {sample.stratum_id!r} paused on {sample.divergence_count} divergences "
                    f"(>= {divergence_pause_threshold}); requires >= {expanded_samples} samples and re-verification."
                )
            )
            results.append(
                StratumAgreementResult(
                    stratum_id=sample.stratum_id,
                    sample_count=sample.sample_count,
                    divergence_count=sample.divergence_count,
                    required_samples=expanded_samples,
                    paused_for_recheck=True,
                    approved=approved,
                    reason=reason,
                )
            )
        elif sample.sample_count < min_samples:
            results.append(
                StratumAgreementResult(
                    stratum_id=sample.stratum_id,
                    sample_count=sample.sample_count,
                    divergence_count=sample.divergence_count,
                    required_samples=min_samples,
                    paused_for_recheck=False,
                    approved=False,
                    reason=f"Stratum {sample.stratum_id!r} has {sample.sample_count} samples (< {min_samples} required).",
                )
            )
        else:
            results.append(
                StratumAgreementResult(
                    stratum_id=sample.stratum_id,
                    sample_count=sample.sample_count,
                    divergence_count=sample.divergence_count,
                    required_samples=min_samples,
                    paused_for_recheck=False,
                    approved=True,
                    reason=f"Stratum {sample.stratum_id!r} approved ({sample.sample_count} samples, {sample.divergence_count} divergences).",
                )
            )
    return tuple(results)


def _one_sided_t_95(degrees_of_freedom: int) -> float:
    if degrees_of_freedom <= 0:
        raise ValueError("Degrees of freedom must be positive.")
    return _STUDENT_T_95_ONE_SIDED.get(degrees_of_freedom, _NORMAL_Z_95_ONE_SIDED)


def evaluate_matched_cycles(
    cycles: tuple[MatchedWeeklyCycle, ...],
    *,
    strata: tuple[StratumSample, ...] = (),
    min_cycles: int = _DEFAULT_MIN_CYCLES,
    min_calendar_days: int = _DEFAULT_MIN_CALENDAR_DAYS,
    lcb_threshold: float = _DEFAULT_LCB_THRESHOLD,
) -> StudyEvaluationResult:
    """Evaluate W6.1/W6.2 acceptance criteria including >=8 cycles, >=49 days, and 95% LCB > 0.80."""
    stratum_results = evaluate_stratum_agreement(strata)
    blockers: list[str] = [res.reason for res in stratum_results if not res.approved]
    if not strata:
        blockers.append("Decision M stratified dual-label agreement evidence is required.")

    cycle_count = len(cycles)
    if cycle_count == 0:
        blockers.append(f"At least {min_cycles} matched weekly cycles are required (observed 0).")
        return StudyEvaluationResult(
            accepted=False,
            cycle_count=0,
            elapsed_calendar_days=0,
            mean_saved_fraction=0.0,
            lcb_95_saved_fraction=None,
            stratum_results=stratum_results,
            blocking_reasons=tuple(blockers),
        )

    ratios: list[float] = []
    for cycle in cycles:
        if cycle.baseline_minutes <= 0:
            raise ValueError(f"Cycle {cycle.cycle_id!r} baseline_minutes must be positive.")
        if cycle.assisted_minutes < 0:
            raise ValueError(f"Cycle {cycle.cycle_id!r} assisted_minutes cannot be negative.")
        if not cycle.quality_passed:
            ratios.append(0.0)
            continue
        saved = (
            cycle.quality_adjusted_minutes_saved
            if cycle.quality_adjusted_minutes_saved is not None
            else max(0.0, cycle.baseline_minutes - cycle.assisted_minutes)
        )
        ratios.append(saved / cycle.baseline_minutes)

    elapsed_days = (max(c.week_start for c in cycles) - min(c.week_start for c in cycles)).days
    if cycle_count < min_cycles:
        blockers.append(f"Observed {cycle_count} matched weekly cycles (< {min_cycles} required).")
    if elapsed_days < min_calendar_days:
        blockers.append(f"Elapsed study window is {elapsed_days} calendar days (< {min_calendar_days} required).")

    mean_saved = sum(ratios) / cycle_count
    lcb_95: float | None = None
    if cycle_count >= 2:
        variance = sum((value - mean_saved) ** 2 for value in ratios) / (cycle_count - 1)
        std_err = math.sqrt(variance / cycle_count)
        lcb_95 = mean_saved - (_one_sided_t_95(cycle_count - 1) * std_err)
        if lcb_95 <= lcb_threshold:
            blockers.append(
                f"Quality-adjusted time saved 95% LCB is {lcb_95:.4f} (must exceed {lcb_threshold:.2f})."
            )
    else:
        blockers.append("At least 2 cycles are required to compute a 95% lower confidence bound.")

    return StudyEvaluationResult(
        accepted=not blockers,
        cycle_count=cycle_count,
        elapsed_calendar_days=elapsed_days,
        mean_saved_fraction=mean_saved,
        lcb_95_saved_fraction=lcb_95,
        stratum_results=stratum_results,
        blocking_reasons=tuple(blockers),
    )
