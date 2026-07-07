"""REV multi-budget governor (Zone A).

specs/program-context-intelligence.md §5.1/§5.10. A per-cycle, in-process
multi-budget governor that stops the pipeline **deterministically** before any
single budget is breached. It reuses the existing ``src/core/retry.py`` (429 →
Retry-After) and ``src/core/circuit_breaker.py`` (cross-cycle provider breaker)
for the *retry/circuit* concerns; this module owns the *budget* concern —
items, bytes, chunks, tokens, content-safety requests, monetized spend, and
wall-clock.

The governor is consulted after every port call. A breached budget returns a
``GovernorDecision(continue=False, reason, category)`` whose ``category`` is
the G-enum bucket the run-state records (``truncated_by_budget`` for a soft
budget stop, ``provider_limited`` for a circuit open). The pipeline then stops
**cleanly** — already-vaulted excerpts and staged candidates are preserved
(crash-safety §5.10); only the in-flight ephemeral stage is reverted.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from src.core.rev.result import (
    Forbidden,
    Incomplete,
    RateLimited,
    Unsupported,
    outcome_category,
)


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    """Per-cycle budget ceilings (mirrors ``RevBudgets`` in models_v2.py).

    Concurrency controls (§5.10):
    * ``concurrency_per_provider`` — bounded I/O concurrency per provider
      (floor); the P1 synchronous pipeline runs at 1; async P2 pipeline
      honours this as a semaphore size.
    * ``fleet_concurrency_cap`` — shared ceiling across programs/lanes so
      multiple programs do not independently exhaust user/tenant quota.
    * ``quiet_lane_relevance_threshold`` — if all remaining candidates in a
      lane score below this, the lane exits early without consuming further
      budget (§5.10 quiet-lane early exit).
    """

    max_search_requests_total_per_cycle: int = 60
    max_search_requests_per_entity_per_cycle: int = 20
    max_hydrated_bytes_per_cycle: int = 10_485_760
    max_hydrated_bytes_per_item: int = 1_048_576
    max_chunk_count_per_cycle: int = 200
    max_chunk_count_per_item: int = 40
    max_model_tokens_in_per_cycle: int = 500_000
    max_model_tokens_out_per_cycle: int = 50_000
    max_content_safety_requests_per_cycle: int = 200
    max_monetized_spend_per_cycle_usd: float = 2.0
    max_wall_clock_seconds: int = 600
    concurrency_per_provider: int = 4
    fleet_concurrency_cap: int = 12
    quiet_lane_relevance_threshold: float = 0.05   # below this → quiet-lane early exit

    @classmethod
    def from_rev_budgets(cls, budgets: Any) -> "BudgetLimits":
        """Build from a ``RevBudgets`` (models_v2) by attribute name."""

        def _get(name: str, default: int | float) -> int | float:
            return getattr(budgets, name, default)

        return cls(
            max_search_requests_total_per_cycle=int(_get("max_search_requests_total_per_cycle", 60)),
            max_search_requests_per_entity_per_cycle=int(_get("max_search_requests_per_entity_per_cycle", 20)),
            max_hydrated_bytes_per_cycle=int(_get("max_hydrated_bytes_per_cycle", 10_485_760)),
            max_hydrated_bytes_per_item=int(_get("max_hydrated_bytes_per_item", 1_048_576)),
            max_chunk_count_per_cycle=int(_get("max_chunk_count_per_cycle", 200)),
            max_chunk_count_per_item=int(_get("max_chunk_count_per_item", 40)),
            max_model_tokens_in_per_cycle=int(_get("max_model_tokens_in_per_cycle", 500_000)),
            max_model_tokens_out_per_cycle=int(_get("max_model_tokens_out_per_cycle", 50_000)),
            max_content_safety_requests_per_cycle=int(_get("max_content_safety_requests_per_cycle", 200)),
            max_monetized_spend_per_cycle_usd=float(_get("max_monetized_spend_per_cycle_usd", 2.0)),
            max_wall_clock_seconds=int(_get("max_wall_clock_seconds", 600)),
            concurrency_per_provider=int(_get("concurrency_per_provider", 4)),
            fleet_concurrency_cap=int(_get("fleet_concurrency_cap", 12)),
            quiet_lane_relevance_threshold=float(_get("quiet_lane_relevance_threshold", 0.05)),
        )


@dataclass(slots=True)
class BudgetUsage:
    search_requests_total: int = 0
    search_requests_per_entity: dict[str, int] = field(default_factory=dict)
    hydrated_bytes: int = 0
    chunk_count: int = 0
    model_tokens_in: int = 0
    model_tokens_out: int = 0
    content_safety_requests: int = 0
    monetized_spend_usd: float = 0.0
    started_monotonic: float = field(default_factory=time.monotonic)

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_monotonic


@dataclass(frozen=True, slots=True)
class GovernorDecision:
    continue_run: bool
    reason: str = ""
    category: str = "complete"   # G-enum bucket to record on stop
    breached_budget: str = ""    # which budget name stopped the run ("" if none)


class Governor:
    """Multi-budget governor — consulted after every port call (§5.1)."""

    def __init__(self, limits: BudgetLimits) -> None:
        self.limits = limits
        self.usage = BudgetUsage()

    def record_search(self, entity_type: str) -> GovernorDecision:
        self.usage.search_requests_total += 1
        self.usage.search_requests_per_entity[entity_type] = self.usage.search_requests_per_entity.get(entity_type, 0) + 1
        if self.usage.search_requests_total > self.limits.max_search_requests_total_per_cycle:
            return self._stop("search_requests_total", "max_search_requests_total_per_cycle")
        if self.usage.search_requests_per_entity[entity_type] > self.limits.max_search_requests_per_entity_per_cycle:
            return self._stop("search_requests_per_entity", "max_search_requests_per_entity_per_cycle")
        return self._ok()

    def record_hydration(self, *, item_bytes: int, chunk_count: int) -> GovernorDecision:
        self.usage.hydrated_bytes += item_bytes
        self.usage.chunk_count += chunk_count
        if item_bytes > self.limits.max_hydrated_bytes_per_item:
            return self._stop("hydrated_bytes_per_item", "max_hydrated_bytes_per_item", category="truncated_by_budget")
        if self.usage.hydrated_bytes > self.limits.max_hydrated_bytes_per_cycle:
            return self._stop("hydrated_bytes", "max_hydrated_bytes_per_cycle")
        if chunk_count > self.limits.max_chunk_count_per_item:
            return self._stop("chunk_count_per_item", "max_chunk_count_per_item", category="truncated_by_budget")
        if self.usage.chunk_count > self.limits.max_chunk_count_per_cycle:
            return self._stop("chunk_count", "max_chunk_count_per_cycle")
        return self._ok()

    def record_model_tokens(self, *, tokens_in: int = 0, tokens_out: int = 0) -> GovernorDecision:
        self.usage.model_tokens_in += tokens_in
        self.usage.model_tokens_out += tokens_out
        if self.usage.model_tokens_in > self.limits.max_model_tokens_in_per_cycle:
            return self._stop("model_tokens_in", "max_model_tokens_in_per_cycle")
        if self.usage.model_tokens_out > self.limits.max_model_tokens_out_per_cycle:
            return self._stop("model_tokens_out", "max_model_tokens_out_per_cycle")
        return self._ok()

    def record_content_safety(self) -> GovernorDecision:
        self.usage.content_safety_requests += 1
        if self.usage.content_safety_requests > self.limits.max_content_safety_requests_per_cycle:
            return self._stop("content_safety_requests", "max_content_safety_requests_per_cycle")
        return self._ok()

    def record_spend(self, *, usd: float) -> GovernorDecision:
        self.usage.monetized_spend_usd += usd
        if self.usage.monetized_spend_usd > self.limits.max_monetized_spend_per_cycle_usd:
            return self._stop("monetized_spend", "max_monetized_spend_per_cycle_usd")
        return self._ok()

    def check_wall_clock(self) -> GovernorDecision:
        if self.usage.elapsed_seconds() > self.limits.max_wall_clock_seconds:
            return self._stop("wall_clock", "max_wall_clock_seconds")
        return self._ok()

    def check_quiet_lane(self, remaining_relevance_scores: tuple[float, ...]) -> GovernorDecision:
        """Quiet-lane early exit (§5.10).

        If every remaining candidate scores below ``quiet_lane_relevance_threshold``,
        there is no budget value in continuing — exit cleanly without consuming
        further requests/bytes/tokens. This prevents exhausting the budget on
        low-signal noise in dormant programs.
        """
        if not remaining_relevance_scores:
            return GovernorDecision(continue_run=False, reason="quiet_lane:no_candidates", category="complete")
        threshold = self.limits.quiet_lane_relevance_threshold
        if all(score < threshold for score in remaining_relevance_scores):
            return GovernorDecision(
                continue_run=False,
                reason=f"quiet_lane:all_below_threshold({threshold})",
                category="complete",
                breached_budget="quiet_lane",
            )
        return self._ok()

    def decide_for_port_result(self, result: object) -> GovernorDecision:
        """Map a port result's category to a governor decision (§5.10).

        A 429 (``RateLimited``) is already retried by ``retry_with_backoff`` at
        the port boundary; if it still surfaces, the governor records a
        ``provider_limited`` stop. A 403 (``Forbidden``) stops with
        ``provider_limited`` and visible degrade (no retry). ``Unsupported`` is
        reported but does not stop the *run* (only that capability is skipped).
        ``Incomplete`` → ``truncated_by_budget`` if the reason is a budget stop,
        else continues.
        """
        category = outcome_category(result)
        if isinstance(result, RateLimited):
            return GovernorDecision(continue_run=False, reason=f"rate_limited:{getattr(result, 'provider', '')}", category="provider_limited", breached_budget="rate_limit")
        if isinstance(result, Forbidden):
            return GovernorDecision(continue_run=False, reason=f"forbidden:{getattr(result, 'scope', '')}", category="provider_limited", breached_budget="forbidden")
        if isinstance(result, Unsupported):
            # capability unsupported for this entity — skip it, don't stop the run
            return self._ok()
        if isinstance(result, Incomplete):
            if category == "truncated_by_budget":
                return GovernorDecision(continue_run=False, reason=getattr(result, "reason", "incomplete"), category="truncated_by_budget", breached_budget="incomplete")
            return self._ok()
        return self._ok()

    def _ok(self) -> GovernorDecision:
        return GovernorDecision(continue_run=True)

    def _stop(self, label: str, budget_name: str, *, category: str = "truncated_by_budget") -> GovernorDecision:
        return GovernorDecision(continue_run=False, reason=f"budget_exceeded:{label}", category=category, breached_budget=budget_name)


__all__ = [
    "BudgetLimits",
    "BudgetUsage",
    "Governor",
    "GovernorDecision",
]