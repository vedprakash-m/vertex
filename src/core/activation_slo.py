"""Activation SLO and time-motion ROI gates (activation.md §6.9 / AG-14 / AG-20)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ActivationSloBudget:
    """Provisional single-program activation budgets before first calibration."""

    azure_cs_item_seconds: float = 5.0
    llm_item_seconds: float = 5.0
    rev_wall_clock_seconds_per_100_eml: float = 1000.0
    render_overhead_ms: float = 500.0
    revoke_to_render_seconds: float = 30.0
    growth_mb_per_cycle: float = 25.0
    evidence_vault_ttl_days: int = 90
    cost_usd_per_100_eml: float = 5.0


@dataclass(frozen=True, slots=True)
class ActivationSloSample:
    azure_cs_item_seconds: float | None = None
    llm_item_seconds: float | None = None
    eml_count: int = 0
    rev_wall_clock_seconds: float | None = None
    render_overhead_ms: float | None = None
    revoke_to_render_seconds: float | None = None
    growth_mb_this_cycle: float | None = None
    evidence_vault_ttl_days: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class ActivationSloVerdict:
    passed: bool
    failures: tuple[str, ...]
    budget: ActivationSloBudget

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "failures": list(self.failures),
            "budget": {
                "azure_cs_item_seconds": self.budget.azure_cs_item_seconds,
                "llm_item_seconds": self.budget.llm_item_seconds,
                "rev_wall_clock_seconds_per_100_eml": self.budget.rev_wall_clock_seconds_per_100_eml,
                "render_overhead_ms": self.budget.render_overhead_ms,
                "revoke_to_render_seconds": self.budget.revoke_to_render_seconds,
                "growth_mb_per_cycle": self.budget.growth_mb_per_cycle,
                "evidence_vault_ttl_days": self.budget.evidence_vault_ttl_days,
                "cost_usd_per_100_eml": self.budget.cost_usd_per_100_eml,
            },
        }


@dataclass(frozen=True, slots=True)
class TimeMotionSample:
    manual_export_seconds: float
    triage_seconds: float
    manual_typing_seconds: float


@dataclass(frozen=True, slots=True)
class TimeMotionRoiVerdict:
    passed: bool
    vertex_seconds: float
    manual_typing_seconds: float
    saved_seconds: float
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "vertex_seconds": self.vertex_seconds,
            "manual_typing_seconds": self.manual_typing_seconds,
            "saved_seconds": self.saved_seconds,
            "reason": self.reason,
        }


def evaluate_activation_slo(
    sample: ActivationSloSample,
    *,
    budget: ActivationSloBudget = ActivationSloBudget(),
) -> ActivationSloVerdict:
    """Evaluate one measured activation run against provisional AG-14 budgets."""
    failures: list[str] = []
    _require_at_most(failures, "azure_cs_item_seconds", sample.azure_cs_item_seconds, budget.azure_cs_item_seconds)
    _require_at_most(failures, "llm_item_seconds", sample.llm_item_seconds, budget.llm_item_seconds)
    _require_at_most(failures, "render_overhead_ms", sample.render_overhead_ms, budget.render_overhead_ms)
    _require_at_most(failures, "revoke_to_render_seconds", sample.revoke_to_render_seconds, budget.revoke_to_render_seconds)
    _require_at_most(failures, "growth_mb_this_cycle", sample.growth_mb_this_cycle, budget.growth_mb_per_cycle)
    if sample.evidence_vault_ttl_days is None:
        failures.append("evidence_vault_ttl_days missing")
    elif sample.evidence_vault_ttl_days > budget.evidence_vault_ttl_days:
        failures.append(
            f"evidence_vault_ttl_days {sample.evidence_vault_ttl_days} > {budget.evidence_vault_ttl_days}"
        )
    if sample.rev_wall_clock_seconds is None:
        failures.append("rev_wall_clock_seconds missing")
    else:
        eml_units = max(sample.eml_count, 1) / 100.0
        allowed = budget.rev_wall_clock_seconds_per_100_eml * eml_units
        if sample.rev_wall_clock_seconds > allowed:
            failures.append(f"rev_wall_clock_seconds {sample.rev_wall_clock_seconds} > {round(allowed, 3)}")
    if sample.cost_usd is None:
        failures.append("cost_usd missing")
    else:
        eml_units = max(sample.eml_count, 1) / 100.0
        allowed = budget.cost_usd_per_100_eml * eml_units
        if sample.cost_usd > allowed:
            failures.append(f"cost_usd {sample.cost_usd} > {round(allowed, 3)}")
    return ActivationSloVerdict(passed=not failures, failures=tuple(failures), budget=budget)


def evaluate_time_motion_roi(sample: TimeMotionSample) -> TimeMotionRoiVerdict:
    """AG-20: operator-deposit + triage must beat typing the same update by hand."""
    vertex_seconds = max(0.0, sample.manual_export_seconds) + max(0.0, sample.triage_seconds)
    manual_seconds = max(0.0, sample.manual_typing_seconds)
    saved = manual_seconds - vertex_seconds
    passed = saved > 0
    return TimeMotionRoiVerdict(
        passed=passed,
        vertex_seconds=round(vertex_seconds, 3),
        manual_typing_seconds=round(manual_seconds, 3),
        saved_seconds=round(saved, 3),
        reason="Vertex path is faster than manual typing" if passed else "Vertex path is not faster than manual typing",
    )


def _require_at_most(failures: list[str], name: str, observed: float | None, allowed: float) -> None:
    if observed is None:
        failures.append(f"{name} missing")
    elif observed > allowed:
        failures.append(f"{name} {observed} > {allowed}")


__all__ = [
    "ActivationSloBudget",
    "ActivationSloSample",
    "ActivationSloVerdict",
    "TimeMotionSample",
    "TimeMotionRoiVerdict",
    "evaluate_activation_slo",
    "evaluate_time_motion_roi",
]
