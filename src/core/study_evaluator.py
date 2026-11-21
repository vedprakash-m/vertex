"""RELO-W6.1 / W6.2 matched-cycle study & Decision M agreement evaluator (Zone A pure).

Implements the mathematical and protocol acceptance predicates specified in
specs/re-re-vertex.md §7 and specs/re-vertex.md §0.6:
- Decision M (`W6.1`): Stratified dual-label audit with minimum 60 items per
  stratum; >=2 divergences pause model-assisted labeling for that stratum and
  expand required re-verification sample size to 120.
- Decision L (`W6.2`): Minimum 8 matched weekly operating cycles spanning >=49
  calendar days, with one-sided 95% lower confidence bound (LCB) on
  quality-adjusted time saved exceeding 80% of the baseline, estimated by resampling matched cycles.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math
from random import Random

_DEFAULT_MIN_STRATUM_SAMPLES = 60
_DEFAULT_EXPANDED_STRATUM_SAMPLES = 120
_DEFAULT_DIVERGENCE_PAUSE_THRESHOLD = 2
_DEFAULT_BASELINE_MINUTES = 420.0
_DEFAULT_MIN_CYCLES = 8
_DEFAULT_MIN_CALENDAR_DAYS = 49
_DEFAULT_LCB_THRESHOLD = 0.80

_BOOTSTRAP_RESAMPLES = 20_000


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


@dataclass(frozen=True, slots=True)
class DualLabelPair:
    item_id: str
    human_label: str
    model_label: str
    adjudicated_label: str


@dataclass(frozen=True, slots=True)
class HoldoutAgreementResult:
    sample_count: int
    divergence_count: int
    adjudicated_count: int
    human_model_kappa: float


def evaluate_dual_label_holdout(
    pairs: tuple[DualLabelPair, ...],
    *,
    kappa_floor: float = 0.70,
) -> HoldoutAgreementResult:
    """Evaluate paired human/model labels with explicit human adjudication."""
    if not 0 <= kappa_floor <= 1:
        raise ValueError("kappa_floor must be between 0 and 1.")
    if not pairs:
        raise ValueError("At least one dual-label pair is required.")
    ids = [pair.item_id for pair in pairs]
    if len(set(ids)) != len(ids) or any(not item_id.strip() for item_id in ids):
        raise ValueError("Dual-label item IDs must be unique and non-empty.")
    for pair in pairs:
        if any(not label.strip() for label in (
            pair.human_label,
            pair.model_label,
            pair.adjudicated_label,
        )):
            raise ValueError("Dual-label observations require human, model and adjudicated labels.")

    divergence_count = sum(pair.human_label != pair.model_label for pair in pairs)
    for pair in pairs:
        if pair.human_label != pair.model_label and pair.adjudicated_label not in {
            pair.human_label,
            pair.model_label,
        }:
            raise ValueError(
                f"Divergent pair {pair.item_id!r} requires human adjudication to select a supplied label."
            )
    kappa = _cohen_kappa(pairs)
    if not math.isfinite(kappa) or kappa < kappa_floor:
        raise ValueError(
            f"Human/model kappa {kappa:.4f} is below the required floor {kappa_floor:.4f}."
        )
    return HoldoutAgreementResult(
        sample_count=len(pairs),
        divergence_count=divergence_count,
        adjudicated_count=divergence_count,
        human_model_kappa=kappa,
    )


def _cohen_kappa(pairs: tuple[DualLabelPair, ...]) -> float:
    labels = sorted({
        label
        for pair in pairs
        for label in (pair.human_label, pair.model_label)
    })
    count = len(pairs)
    observed = sum(pair.human_label == pair.model_label for pair in pairs) / count
    if len(labels) == 1:
        return 1.0
    expected = math.fsum(
        (
            sum(pair.human_label == label for pair in pairs) / count
            * sum(pair.model_label == label for pair in pairs)
            / count
        )
        for label in labels
    )
    if expected >= 1.0:
        return 1.0 if observed == 1.0 else 0.0
    return (observed - expected) / (1.0 - expected)


def evaluate_stratum_agreement(
    samples: tuple[StratumSample, ...],
    *,
    min_samples: int = _DEFAULT_MIN_STRATUM_SAMPLES,
    expanded_samples: int = _DEFAULT_EXPANDED_STRATUM_SAMPLES,
    divergence_pause_threshold: int = _DEFAULT_DIVERGENCE_PAUSE_THRESHOLD,
) -> tuple[StratumAgreementResult, ...]:
    """Evaluate Decision M stratified dual-label agreement per section stratum."""
    if len({sample.stratum_id for sample in samples}) != len(samples):
        raise ValueError("Stratum IDs must be unique.")
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


def _bootstrap_lower_bound(ratios: list[float]) -> float:
    # Resample whole matched operating cycles, preserving the pairing and
    # the outcomes of failed workflows already reflected in each cycle.
    rng = Random(0)
    count = len(ratios)
    means = sorted(
        math.fsum(rng.choices(ratios, k=count)) / count
        for _ in range(_BOOTSTRAP_RESAMPLES)
    )
    return means[_BOOTSTRAP_RESAMPLES // 20 - 1]


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
    ordered_cycles = sorted(cycles, key=lambda cycle: cycle.week_start)
    if len({cycle.cycle_id for cycle in cycles}) != cycle_count:
        blockers.append("Matched cycle IDs must be unique.")
    if any((later.week_start - earlier.week_start).days < 7
           for earlier, later in zip(ordered_cycles, ordered_cycles[1:])):
        blockers.append("Matched weekly cycles must not overlap.")
    for cycle in ordered_cycles:
        if not math.isfinite(cycle.baseline_minutes) or cycle.baseline_minutes <= 0:
            raise ValueError(f"Cycle {cycle.cycle_id!r} baseline_minutes must be finite and positive.")
        if not math.isfinite(cycle.assisted_minutes) or cycle.assisted_minutes < 0:
            raise ValueError(f"Cycle {cycle.cycle_id!r} assisted_minutes must be finite and nonnegative.")
        adjusted = cycle.quality_adjusted_minutes_saved
        if adjusted is not None and (not math.isfinite(adjusted) or adjusted > cycle.baseline_minutes):
            raise ValueError(
                f"Cycle {cycle.cycle_id!r} quality_adjusted_minutes_saved must be finite and at most baseline_minutes."
            )
        if not cycle.quality_passed:
            ratios.append(0.0)
            continue
        saved = cycle.baseline_minutes - cycle.assisted_minutes if adjusted is None else adjusted
        ratios.append(saved / cycle.baseline_minutes)

    elapsed_days = (ordered_cycles[-1].week_start - ordered_cycles[0].week_start).days
    if cycle_count < min_cycles:
        blockers.append(f"Observed {cycle_count} matched weekly cycles (< {min_cycles} required).")
    if elapsed_days < min_calendar_days:
        blockers.append(f"Elapsed study window is {elapsed_days} calendar days (< {min_calendar_days} required).")

    mean_saved = math.fsum(ratios) / cycle_count
    lcb_95: float | None = None
    if cycle_count >= 2:
        lcb_95 = _bootstrap_lower_bound(ratios)
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
