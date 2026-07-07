"""Activation benefit and rollback evaluators.

These helpers keep activation.md's Vision-bar measurement contracts executable
without depending on live XPF evidence. They do not certify benefit by
themselves; they define the deterministic pass/fail math used once real issue
samples and sustaining quality checks exist.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BenefitTrendSample:
    issue_number: int
    auto_approved_signal_rate: float | None = None
    operator_review_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class BenefitTrendVerdict:
    passed: bool
    reasons: tuple[str, ...]
    sample_count: int
    auto_approved_delta: float | None
    review_seconds_delta: float | None


@dataclass(frozen=True, slots=True)
class SustainingQualitySample:
    family: str
    kappa: float | None
    precision_ci_low: float | None
    recall_ci_low: float | None


@dataclass(frozen=True, slots=True)
class CorpusRollbackVerdict:
    rollback_required: bool
    reasons: tuple[str, ...]
    family: str


def evaluate_longitudinal_benefit(
    samples: tuple[BenefitTrendSample, ...],
    *,
    min_samples: int = 3,
    min_auto_approved_delta: float = 0.0,
    min_review_seconds_reduction: float = 0.0,
) -> BenefitTrendVerdict:
    """Evaluate AG-16: review should shrink over time.

    A pass requires enough issue samples and at least one positive trend:
    auto-approved-signal rate increases or operator review time decreases.
    """
    ordered = tuple(sorted(samples, key=lambda sample: sample.issue_number))
    reasons: list[str] = []
    if len(ordered) < min_samples:
        reasons.append(f"sample_count {len(ordered)} < {min_samples}")

    auto_delta = _delta(
        tuple(sample.auto_approved_signal_rate for sample in ordered if sample.auto_approved_signal_rate is not None)
    )
    review_delta = _delta(
        tuple(sample.operator_review_seconds for sample in ordered if sample.operator_review_seconds is not None)
    )

    auto_improved = auto_delta is not None and auto_delta > min_auto_approved_delta
    review_improved = review_delta is not None and review_delta < -min_review_seconds_reduction
    if not (auto_improved or review_improved):
        reasons.append("no positive longitudinal trend")

    return BenefitTrendVerdict(
        passed=not reasons,
        reasons=tuple(reasons),
        sample_count=len(ordered),
        auto_approved_delta=auto_delta,
        review_seconds_delta=review_delta,
    )


def evaluate_corpus_rollback(
    sample: SustainingQualitySample,
    *,
    kappa_floor: float = 0.70,
    precision_ci_low_floor: float = 0.80,
    recall_ci_low_floor: float = 0.60,
) -> CorpusRollbackVerdict:
    """Evaluate §6.14.20 corpus/quality rollback triggers."""
    reasons: list[str] = []
    if sample.kappa is None or sample.kappa < kappa_floor:
        reasons.append(f"kappa {sample.kappa!r} < {kappa_floor}")
    if sample.precision_ci_low is None or sample.precision_ci_low < precision_ci_low_floor:
        reasons.append(f"precision_ci_low {sample.precision_ci_low!r} < {precision_ci_low_floor}")
    if sample.recall_ci_low is None or sample.recall_ci_low < recall_ci_low_floor:
        reasons.append(f"recall_ci_low {sample.recall_ci_low!r} < {recall_ci_low_floor}")
    return CorpusRollbackVerdict(
        rollback_required=bool(reasons),
        reasons=tuple(reasons),
        family=sample.family,
    )


def _delta(values: tuple[float, ...]) -> float | None:
    if len(values) < 2:
        return None
    return values[-1] - values[0]
