"""Activation fleet, residency, and operator-review evaluators."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping


@dataclass(frozen=True, slots=True)
class AccessorRolloutPlan:
    accessor_count: int
    shared_accessors: dict[str, tuple[str, ...]]
    unsupported_claims: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FleetProgramSample:
    program_id: str
    rendered_from_program_reality: bool
    quiet_lane: bool = False
    isolation_violations: int = 0
    growth_mb_this_cycle: float | None = None
    cost_usd_this_cycle: float | None = None
    observed_concurrency: int | None = None


@dataclass(frozen=True, slots=True)
class FleetSoakBudget:
    min_programs: int = 3
    growth_mb_per_program: float = 25.0
    cost_usd_per_program: float = 5.0
    fleet_concurrency_cap: int = 12


@dataclass(frozen=True, slots=True)
class FleetSoakVerdict:
    passed: bool
    reasons: tuple[str, ...]
    program_count: int
    quiet_lane_count: int


@dataclass(frozen=True, slots=True)
class DataResidencyRequirement:
    source: str
    classification: str
    residency: str
    approved: bool


@dataclass(frozen=True, slots=True)
class DataResidencyVerdict:
    passed: bool
    reasons: tuple[str, ...]
    source_count: int


@dataclass(frozen=True, slots=True)
class OperatorClarityReview:
    reviewer: str
    explain_min_present: bool
    disputed_badge_present: bool
    downgrade_banner_present: bool
    accessibility_notes_present: bool
    approved: bool


@dataclass(frozen=True, slots=True)
class OperatorClarityVerdict:
    passed: bool
    reasons: tuple[str, ...]


def build_accessor_rollout_plan(rows: Iterable[Mapping[str, object]]) -> AccessorRolloutPlan:
    """Summarize v1 authority claims by ProgramReality accessor unit."""
    by_accessor: dict[str, list[str]] = defaultdict(list)
    unsupported: list[str] = []
    for row in rows:
        claim = str(row.get("claim_event_type") or "")
        accessor = row.get("accessor")
        status = str(row.get("status") or "")
        if accessor:
            by_accessor[str(accessor)].append(claim)
        elif status.startswith("recommended_unsupported"):
            unsupported.append(claim)
    shared = {
        accessor: tuple(sorted(claims))
        for accessor, claims in sorted(by_accessor.items())
        if len(claims) > 1
    }
    return AccessorRolloutPlan(
        accessor_count=len(by_accessor),
        shared_accessors=shared,
        unsupported_claims=tuple(sorted(unsupported)),
    )


def evaluate_fleet_soak(
    samples: tuple[FleetProgramSample, ...],
    *,
    budget: FleetSoakBudget = FleetSoakBudget(),
) -> FleetSoakVerdict:
    reasons: list[str] = []
    program_ids = {sample.program_id for sample in samples}
    if len(program_ids) < budget.min_programs:
        reasons.append(f"program_count {len(program_ids)} < {budget.min_programs}")
    quiet_lane_count = sum(1 for sample in samples if sample.quiet_lane)
    if quiet_lane_count < 1:
        reasons.append("quiet_lane_count 0 < 1")
    for sample in samples:
        if not sample.rendered_from_program_reality:
            reasons.append(f"{sample.program_id}: not rendered_from_program_reality")
        if sample.isolation_violations:
            reasons.append(f"{sample.program_id}: isolation_violations {sample.isolation_violations}")
        if sample.growth_mb_this_cycle is None or sample.growth_mb_this_cycle > budget.growth_mb_per_program:
            reasons.append(f"{sample.program_id}: growth_mb_this_cycle {sample.growth_mb_this_cycle!r}")
        if sample.cost_usd_this_cycle is None or sample.cost_usd_this_cycle > budget.cost_usd_per_program:
            reasons.append(f"{sample.program_id}: cost_usd_this_cycle {sample.cost_usd_this_cycle!r}")
        if sample.observed_concurrency is None or sample.observed_concurrency > budget.fleet_concurrency_cap:
            reasons.append(f"{sample.program_id}: observed_concurrency {sample.observed_concurrency!r}")
    return FleetSoakVerdict(
        passed=not reasons,
        reasons=tuple(reasons),
        program_count=len(program_ids),
        quiet_lane_count=quiet_lane_count,
    )


def evaluate_data_residency(
    requirements: tuple[DataResidencyRequirement, ...],
) -> DataResidencyVerdict:
    reasons = tuple(
        f"{req.source}: {req.classification}/{req.residency} not approved"
        for req in requirements
        if not req.approved
    )
    return DataResidencyVerdict(
        passed=not reasons and bool(requirements),
        reasons=reasons if requirements else ("no residency requirements supplied",),
        source_count=len(requirements),
    )


def evaluate_operator_clarity(review: OperatorClarityReview) -> OperatorClarityVerdict:
    reasons: list[str] = []
    if not review.reviewer.strip():
        reasons.append("reviewer missing")
    if not review.explain_min_present:
        reasons.append("EXPLAIN-min missing")
    if not review.disputed_badge_present:
        reasons.append("disputed badge missing")
    if not review.downgrade_banner_present:
        reasons.append("downgrade banner missing")
    if not review.accessibility_notes_present:
        reasons.append("accessibility notes missing")
    if not review.approved:
        reasons.append("operator review not approved")
    return OperatorClarityVerdict(passed=not reasons, reasons=tuple(reasons))
