"""ADF-W4.4 (Section 8.10.2): inferred-dependency evidence hierarchy + metrics.

Section 8.10.2 defines a five-level evidence priority for dependency links:

1. authoritative ADO relation (``DependencyEvidenceTier.AUTHORITATIVE_RELATION``)
2. authored dependency (``AUTHORED``)
3. source statement with dependency language (``SOURCE_STATEMENT``)
4. ETA co-movement and corroboration (``ETA_CO_MOVEMENT``)
5. co-mention only, never above low confidence (``INFERRED_COMENTION``)

This module classifies a dependency set into deterministic-vs-inferred buckets
and computes per-tier quality metrics so the cockpit/report can report
"deterministic vs. inferred metrics separated" (the work item's acceptance
evidence) and so an ``INFERRED_COMENTION`` link is provably capped at
``Confidence.LOW`` and never authorizes an actuation.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.models import Confidence
from src.core.models_v2 import Dependency, DependencyEvidenceTier


#: Tiers considered "deterministic" (authoritative relation or authored).
_DETERMINISTIC_TIERS = frozenset(
    {DependencyEvidenceTier.AUTHORITATIVE_RELATION, DependencyEvidenceTier.AUTHORED}
)

#: The maximum confidence an inferred co-mention dependency may carry.
_COMENTION_MAX_CONFIDENCE = Confidence.LOW


@dataclass(frozen=True, slots=True)
class DependencyEvidenceReport:
    """Per-program dependency evidence breakdown (ADF-W4.4)."""

    total: int
    authoritative_relation: int
    authored: int
    source_statement: int
    eta_co_movement: int
    inferred_comention: int
    deterministic_count: int
    inferred_count: int

    @property
    def deterministic_ratio(self) -> float:
        return self.deterministic_count / self.total if self.total else 0.0


def tier_for_dependency(dependency: Dependency) -> DependencyEvidenceTier:
    """Return the evidence tier of a dependency, defaulting safely."""
    return dependency.evidence_tier


def cap_confidence_for_tier(
    tier: DependencyEvidenceTier,
    proposed: Confidence,
) -> Confidence:
    """Enforce the Section 8.10.2 confidence cap for weak evidence tiers.

    A co-mention-only dependency may never exceed ``Confidence.LOW`` regardless
    of what a proposer suggested.
    """
    if tier is DependencyEvidenceTier.INFERRED_COMENTION:
        # LOW is "higher" than NONE; keep the min of the two.
        if proposed in (Confidence.HIGH, Confidence.MEDIUM):
            return _COMENTION_MAX_CONFIDENCE
    return proposed


def build_evidence_report(dependencies: tuple[Dependency, ...]) -> DependencyEvidenceReport:
    """Classify a dependency set and compute per-tier counts."""
    counts: dict[DependencyEvidenceTier, int] = {
        DependencyEvidenceTier.AUTHORITATIVE_RELATION: 0,
        DependencyEvidenceTier.AUTHORED: 0,
        DependencyEvidenceTier.SOURCE_STATEMENT: 0,
        DependencyEvidenceTier.ETA_CO_MOVEMENT: 0,
        DependencyEvidenceTier.INFERRED_COMENTION: 0,
    }
    for dep in dependencies:
        counts[dep.evidence_tier] = counts.get(dep.evidence_tier, 0) + 1
    deterministic = counts[DependencyEvidenceTier.AUTHORITATIVE_RELATION] + counts[DependencyEvidenceTier.AUTHORED]
    inferred = sum(counts.values()) - deterministic
    return DependencyEvidenceReport(
        total=len(dependencies),
        authoritative_relation=counts[DependencyEvidenceTier.AUTHORITATIVE_RELATION],
        authored=counts[DependencyEvidenceTier.AUTHORED],
        source_statement=counts[DependencyEvidenceTier.SOURCE_STATEMENT],
        eta_co_movement=counts[DependencyEvidenceTier.ETA_CO_MOVEMENT],
        inferred_comention=counts[DependencyEvidenceTier.INFERRED_COMENTION],
        deterministic_count=deterministic,
        inferred_count=inferred,
    )


def is_actuation_eligible(tier: DependencyEvidenceTier) -> bool:
    """An INFERRED_COMENTION dependency cannot authorize an external actuation."""
    return tier is not DependencyEvidenceTier.INFERRED_COMENTION


__all__ = [
    "DependencyEvidenceReport",
    "build_evidence_report",
    "cap_confidence_for_tier",
    "is_actuation_eligible",
    "tier_for_dependency",
]
