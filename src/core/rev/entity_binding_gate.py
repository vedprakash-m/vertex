"""S-6: Entity-binding gate harness (Zone A).

Evaluates entity binding quality for a set of REV candidates: measures binding
precision (correctly resolved / total named-ref attempts) and binding coverage
(resolved / all entity-ref slots), gates before minting new entities, and marks
low-confidence refs ``UNRESOLVED:`` rather than silently inventing new entities.

Invariants (§S-6 gate contract):
  - Binding precision  ≥ PRECISION_FLOOR (0.95): resolved refs / all named-refs
    that were attempted (excludes empty-ref candidates).
  - Binding coverage   ≥ COVERAGE_FLOOR  (0.80): resolved refs / all ref slots
    (including candidates that had zero refs — these count against coverage).
  - Low-confidence candidate → operator-intent gate before minting; prefer
    ``UNRESOLVED:<original>`` over inventing a new canonical entity.
  - Gate exits with ``ok=False`` and a non-empty ``failures`` list when any
    threshold is breached; ``ok=True`` means both gates pass.

Zone A — no AI or M365 imports.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable

_UNRESOLVED_PREFIX = "UNRESOLVED:"

# Gate thresholds (§S-6).
PRECISION_FLOOR = 0.95   # resolved_refs / attempted_refs ≥ 95 %
COVERAGE_FLOOR  = 0.80   # resolved_refs / total_ref_slots ≥ 80 %

# Min sample before gates fire (avoids 1-candidate batches failing on N=1 noise).
MIN_SAMPLE_FOR_GATE = 3

# Wilson CI z for 90 % confidence interval (1.645 ≈ 90 % CI).
_WILSON_Z = 1.645


@dataclass(frozen=True, slots=True)
class CandidateBindingRecord:
    """Summary of entity-binding attempts for one candidate.

    ``total_ref_slots``  — total entity-ref fields in the candidate
                            (may be 0 for facts with no entity refs).
    ``attempted_refs``   — ref slots where a name/id was present (non-empty).
    ``resolved_refs``    — of those, how many resolved to a canonical entity
                            (i.e. not prefixed with ``UNRESOLVED:``).
    ``unresolved_refs``  — ref slots with the ``UNRESOLVED:`` prefix.
    ``minted_refs``      — ref slots where a *new* entity was minted by the
                            extractor (vs matched against existing). An operator
                            gate fires before minting is allowed.
    """

    candidate_id: str
    total_ref_slots: int = 0
    attempted_refs: int = 0
    resolved_refs: int = 0
    unresolved_refs: int = 0
    minted_refs: int = 0

    @property
    def precision(self) -> float:
        if self.attempted_refs == 0:
            return 1.0  # no attempts → not counted against precision
        return self.resolved_refs / self.attempted_refs

    @property
    def coverage(self) -> float:
        if self.total_ref_slots == 0:
            return 1.0  # no ref slots → not counted against coverage
        return self.resolved_refs / self.total_ref_slots


def _wilson_ci(successes: int, total: int) -> tuple[float, float]:
    """Wilson score 90 % confidence interval (lower, upper)."""
    if total == 0:
        return (0.0, 1.0)
    p_hat = successes / total
    z = _WILSON_Z
    denom = 1 + z ** 2 / total
    center = (p_hat + z ** 2 / (2 * total)) / denom
    margin = (
        z * math.sqrt(p_hat * (1 - p_hat) / total + z ** 2 / (4 * total ** 2))
        / denom
    )
    return (max(0.0, center - margin), min(1.0, center + margin))


@dataclass
class EntityBindingReport:
    """Aggregated binding quality report for a batch of candidates."""

    program_id: str
    n_candidates: int = 0
    total_ref_slots: int = 0
    attempted_refs: int = 0
    resolved_refs: int = 0
    unresolved_refs: int = 0
    minted_refs: int = 0

    # Computed metrics (populated by evaluate()).
    binding_precision: float = 0.0
    binding_precision_ci_low: float = 0.0
    binding_precision_ci_high: float = 1.0
    binding_coverage: float = 0.0
    binding_coverage_ci_low: float = 0.0
    binding_coverage_ci_high: float = 1.0

    per_candidate: list[CandidateBindingRecord] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict:
        return {
            "program_id": self.program_id,
            "n_candidates": self.n_candidates,
            "total_ref_slots": self.total_ref_slots,
            "attempted_refs": self.attempted_refs,
            "resolved_refs": self.resolved_refs,
            "unresolved_refs": self.unresolved_refs,
            "minted_refs": self.minted_refs,
            "binding_precision": round(self.binding_precision, 4),
            "binding_precision_ci": (
                round(self.binding_precision_ci_low, 4),
                round(self.binding_precision_ci_high, 4),
            ),
            "binding_coverage": round(self.binding_coverage, 4),
            "binding_coverage_ci": (
                round(self.binding_coverage_ci_low, 4),
                round(self.binding_coverage_ci_high, 4),
            ),
            "failures": list(self.failures),
            "warnings": list(self.warnings),
        }


def evaluate_binding(
    records: Iterable[CandidateBindingRecord],
    *,
    program_id: str,
) -> EntityBindingReport:
    """Evaluate entity-binding quality over a batch of candidates.

    Aggregates per-candidate records, computes precision + coverage with 90 %
    Wilson CIs, and fires gate failures when thresholds are breached.

    The caller is responsible for building ``CandidateBindingRecord`` objects
    from the candidate store (see ``binding_record_from_entity_refs``).
    """
    report = EntityBindingReport(program_id=program_id)
    records_list = list(records)
    report.n_candidates = len(records_list)
    report.per_candidate = records_list

    for rec in records_list:
        report.total_ref_slots  += rec.total_ref_slots
        report.attempted_refs   += rec.attempted_refs
        report.resolved_refs    += rec.resolved_refs
        report.unresolved_refs  += rec.unresolved_refs
        report.minted_refs      += rec.minted_refs

    if report.n_candidates < MIN_SAMPLE_FOR_GATE:
        report.warnings.append(
            f"small sample (N={report.n_candidates} < {MIN_SAMPLE_FOR_GATE}) — "
            "entity-binding gate not enforced; run again with more candidates."
        )

    # Precision: resolved / attempted (skip if no refs were attempted).
    if report.attempted_refs > 0:
        report.binding_precision = report.resolved_refs / report.attempted_refs
        lo, hi = _wilson_ci(report.resolved_refs, report.attempted_refs)
        report.binding_precision_ci_low, report.binding_precision_ci_high = lo, hi
    else:
        report.binding_precision = 1.0
        report.binding_precision_ci_low = 0.0
        report.binding_precision_ci_high = 1.0

    # Coverage: resolved / total ref slots (0 if no slots at all).
    if report.total_ref_slots > 0:
        report.binding_coverage = report.resolved_refs / report.total_ref_slots
        lo, hi = _wilson_ci(report.resolved_refs, report.total_ref_slots)
        report.binding_coverage_ci_low, report.binding_coverage_ci_high = lo, hi
    else:
        report.binding_coverage = 1.0
        report.binding_coverage_ci_low = 0.0
        report.binding_coverage_ci_high = 1.0

    if report.n_candidates >= MIN_SAMPLE_FOR_GATE:
        if report.binding_precision < PRECISION_FLOOR:
            report.failures.append(
                f"binding precision {report.binding_precision:.1%} < {PRECISION_FLOOR:.0%} "
                f"(resolved={report.resolved_refs}/{report.attempted_refs}) — "
                "resolve entity lookup issues or increase known_natural_keys coverage"
            )
        if report.binding_coverage < COVERAGE_FLOOR:
            report.failures.append(
                f"binding coverage {report.binding_coverage:.1%} < {COVERAGE_FLOOR:.0%} "
                f"(resolved={report.resolved_refs}/{report.total_ref_slots}) — "
                "too many candidates have zero entity-ref slots; "
                "check extractor entity extraction for this event-type family"
            )

    if report.minted_refs > 0:
        report.warnings.append(
            f"operator-intent gate: {report.minted_refs} new entity ref(s) were minted "
            "this cycle — review before confirming (S-6 mint gate). "
            "Low-confidence refs should use UNRESOLVED: prefix, not invent a new entity."
        )

    return report


def binding_record_from_entity_refs(
    *,
    candidate_id: str,
    entity_refs: tuple[str, ...],
    total_ref_slots: int | None = None,
    minted_refs: int = 0,
) -> CandidateBindingRecord:
    """Build a ``CandidateBindingRecord`` from a candidate's resolved entity refs.

    ``entity_refs`` should be the refs *after* resolution (so ``UNRESOLVED:*``
    prefixes are present for unresolved refs). ``total_ref_slots`` is the count
    of ref fields defined by the event-type schema; if ``None``, defaults to
    ``len(entity_refs)``.
    """
    attempted = len(entity_refs)
    resolved = sum(1 for r in entity_refs if r and not r.startswith(_UNRESOLVED_PREFIX))
    unresolved = sum(1 for r in entity_refs if r.startswith(_UNRESOLVED_PREFIX))
    total_slots = total_ref_slots if total_ref_slots is not None else attempted
    return CandidateBindingRecord(
        candidate_id=candidate_id,
        total_ref_slots=total_slots,
        attempted_refs=attempted,
        resolved_refs=resolved,
        unresolved_refs=unresolved,
        minted_refs=minted_refs,
    )


__all__ = [
    "CandidateBindingRecord",
    "EntityBindingReport",
    "PRECISION_FLOOR",
    "COVERAGE_FLOOR",
    "MIN_SAMPLE_FOR_GATE",
    "binding_record_from_entity_refs",
    "evaluate_binding",
]
