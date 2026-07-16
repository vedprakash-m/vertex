"""Centralized tiered AI dispatch (D-06 / §7.6 / §10.6).

Before this module, every AI feature reimplemented the deterministic→frontier
ladder inline (e.g. ``claim_extractor.extract_claims``) and *no tier decision was
recorded anywhere*, so the OpEx posture of the system — how often a deterministic
or local tier avoided a frontier (cloud) call — was invisible.

``route_through_tiers`` is the single dispatcher every feature can route through:

    outcome = route_through_tiers(
        "claim_extractor",
        deterministic_fn=lambda: _det_result_or_none(),
        local_fn=None,                       # optional Tier-1 seam
        frontier_fn=lambda: client.structured(...),
    )

Tiers (§7.6):

    Tier 0 deterministic  — regex / structured parsing / state-machine extraction (zero tokens)
    Tier 1 local semantic — local embeddings / fuzzy match / keyword graph (near-zero OpEx)
    Tier 2 frontier       — Azure OpenAI cloud model (budgeted + traced)

The dispatcher records **every** decision (deterministic hit, local hit, frontier
call, frontier blocked, budget/disabled skip) so OpEx can be measured and audited,
and it never raises on a blocked frontier — it returns the best lower-tier value (or
``None``) so callers can degrade to an empty-but-valid result (D-33).
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Callable, Generic, Optional, TypeVar

from src.ai.ai_mode import AIMode, get_ai_mode
from src.ai.llm_trace import get_current_trace_context
from src.core.policy_loader import AIFeaturePolicy, load_ai_feature_policy

T = TypeVar("T")


class Tier(str, Enum):
    """Routing tier that produced (or would have produced) the result."""

    DETERMINISTIC = "deterministic"  # Tier 0
    LOCAL = "local"  # Tier 1
    FRONTIER = "frontier"  # Tier 2
    CACHE = "cache"  # result-cache hit (ADF-F08 / Appendix A.4)
    NONE = "none"  # no tier produced a value


class RouteOutcome(str, Enum):
    """Why a route resolved the way it did — the §10.6 audit vocabulary."""

    DETERMINISTIC_HIT = "deterministic_hit"
    LOCAL_HIT = "local_hit"
    FRONTIER_CALL = "frontier_call"
    FRONTIER_BLOCKED = "frontier_blocked"  # frontier_eligible=False in policy
    FRONTIER_DISABLED = "frontier_disabled"  # AIMode.DISABLED / OBSERVE_ONLY
    CACHE_HIT = "cache_hit"  # shared result-cache served the request (ADF-F08)
    SKIPPED = "skipped"  # no fn produced a value and frontier unavailable


@dataclass(frozen=True, slots=True)
class TierDecision:
    """One recorded routing decision."""

    feature: str
    tier: Tier
    outcome: RouteOutcome
    confidence: float
    frontier_called: bool
    trace_id: Optional[str]
    recorded_at: str


@dataclass(frozen=True, slots=True)
class TierResult(Generic[T]):
    """A lower-tier (deterministic / local) candidate result plus its confidence."""

    value: T
    confidence: float


@dataclass(frozen=True, slots=True)
class RouteResult(Generic[T]):
    """The dispatcher's return: the chosen value (or ``None``) and the decision."""

    value: Optional[T]
    decision: TierDecision

    @property
    def frontier_called(self) -> bool:
        return self.decision.frontier_called


# In-process decision log — always-on observability surface. A bounded ring buffer
# keeps memory flat for long runs while preserving the most recent decisions for
# `doctor`/trace inspection and tests. Persistence to a JSONL sidecar is a clean
# extensibility seam (see `register_decision_sink`).
_DECISION_LOG: deque[TierDecision] = deque(maxlen=2048)
_DECISION_LOCK = Lock()
_DECISION_SINKS: list[Callable[[TierDecision], None]] = []


def register_decision_sink(sink: Callable[[TierDecision], None]) -> None:
    """Register an extra durable sink (e.g. JSONL/trace persistence) for decisions."""
    with _DECISION_LOCK:
        if sink not in _DECISION_SINKS:
            _DECISION_SINKS.append(sink)


def unregister_decision_sink(sink: Callable[[TierDecision], None]) -> None:
    with _DECISION_LOCK:
        if sink in _DECISION_SINKS:
            _DECISION_SINKS.remove(sink)


def recorded_decisions() -> tuple[TierDecision, ...]:
    """Snapshot of recorded routing decisions (most recent last)."""
    with _DECISION_LOCK:
        return tuple(_DECISION_LOG)


def reset_recorded_decisions() -> None:
    """Clear the in-process decision log (test isolation / per-run reset)."""
    with _DECISION_LOCK:
        _DECISION_LOG.clear()


def _record(decision: TierDecision) -> None:
    with _DECISION_LOCK:
        _DECISION_LOG.append(decision)
        sinks = tuple(_DECISION_SINKS)
    # Sinks run outside the lock so a slow/durable sink cannot stall routing.
    for sink in sinks:
        try:
            sink(decision)
        except Exception:  # pragma: no cover - a sink must never break routing
            pass


def _current_trace_id() -> Optional[str]:
    ctx = get_current_trace_context()
    return getattr(ctx, "run_id", None) if ctx is not None else None


def _decide(
    *, feature: str, tier: Tier, outcome: RouteOutcome, confidence: float, frontier_called: bool
) -> TierDecision:
    decision = TierDecision(
        feature=feature,
        tier=tier,
        outcome=outcome,
        confidence=confidence,
        frontier_called=frontier_called,
        trace_id=_current_trace_id(),
        recorded_at=datetime.now(timezone.utc).isoformat(),
    )
    _record(decision)
    return decision


def route_through_tiers(
    feature: str,
    *,
    deterministic_fn: Optional[Callable[[], Optional[TierResult[T]]]] = None,
    local_fn: Optional[Callable[[], Optional[TierResult[T]]]] = None,
    frontier_fn: Optional[Callable[[], T]] = None,
    policy: Optional[AIFeaturePolicy] = None,
    cache_lookup_fn: Optional[Callable[[], Optional[T]]] = None,
    cache_store_fn: Optional[Callable[[T], object]] = None,
) -> RouteResult[T]:
    """Dispatch a feature through Cache → Tier 0 → Tier 1 → Tier 2 (§8.8.3),
    recording the decision.

    - ``cache_lookup_fn`` (ADF-W5.2), when supplied, is checked FIRST —
      before deterministic/local/frontier — matching §8.8.3's
      ``CACHE -> DETERMINISTIC -> LOCAL/ECONOMY -> FRONTIER -> FALLBACK``
      ordering exactly. A hit short-circuits the whole call with
      ``Tier.CACHE``/``RouteOutcome.CACHE_HIT`` and never invokes any other
      fn — "a cache hit never masquerades as a new provider call" (§8.8.3).
      This module has no opinion on cache-key composition or storage; the
      caller supplies both lookup and store as closures (see
      ``src/core/ai_result_cache.py`` for the canonical key shape).
    - ``cache_store_fn``, when supplied, is called with the value ONLY after
      a real frontier call succeeds — never for a deterministic/local hit
      (those are already free; caching them would just add staleness risk
      for no OpEx benefit) and never on a cache hit itself (nothing new to
      store).
    - ``deterministic_fn`` / ``local_fn`` return a ``TierResult`` (value + confidence)
      or ``None`` when they cannot answer. A tier "hits" when it returns a result whose
      confidence meets ``policy.tier0_confidence_threshold``.
    - ``frontier_fn`` is invoked only when no lower tier hits, the feature is
      ``frontier_eligible``, and the current ``AIMode`` permits frontier calls.
    - The function never raises for a blocked/disabled frontier; it returns the best
      available lower-tier value (or ``None``) so callers can return empty-but-valid.

    Exceptions raised *inside* a supplied fn propagate unchanged (the caller owns its
    own error contract).
    """
    resolved_policy = policy if policy is not None else load_ai_feature_policy(feature)
    threshold = resolved_policy.tier0_confidence_threshold

    # Cache — checked before every other tier (§8.8.3 ordering).
    if cache_lookup_fn is not None:
        cached = cache_lookup_fn()
        if cached is not None:
            decision = _decide(
                feature=feature,
                tier=Tier.CACHE,
                outcome=RouteOutcome.CACHE_HIT,
                confidence=1.0,
                frontier_called=False,
            )
            return RouteResult(value=cached, decision=decision)

    best_value: Optional[T] = None
    best_confidence = 0.0

    # Tier 0 — deterministic.
    if deterministic_fn is not None and resolved_policy.deterministic_first:
        det = deterministic_fn()
        if det is not None:
            best_value, best_confidence = det.value, det.confidence
            if det.confidence >= threshold:
                decision = _decide(
                    feature=feature,
                    tier=Tier.DETERMINISTIC,
                    outcome=RouteOutcome.DETERMINISTIC_HIT,
                    confidence=det.confidence,
                    frontier_called=False,
                )
                return RouteResult(value=det.value, decision=decision)

    # Tier 1 — local semantic.
    if local_fn is not None:
        local = local_fn()
        if local is not None:
            if local.confidence > best_confidence:
                best_value, best_confidence = local.value, local.confidence
            if local.confidence >= threshold:
                decision = _decide(
                    feature=feature,
                    tier=Tier.LOCAL,
                    outcome=RouteOutcome.LOCAL_HIT,
                    confidence=local.confidence,
                    frontier_called=False,
                )
                return RouteResult(value=local.value, decision=decision)

    # Tier 2 — frontier. Gated by policy eligibility and execution mode.
    if frontier_fn is None or not resolved_policy.frontier_eligible:
        outcome = (
            RouteOutcome.FRONTIER_BLOCKED
            if frontier_fn is not None
            else RouteOutcome.SKIPPED
        )
        decision = _decide(
            feature=feature,
            tier=Tier.DETERMINISTIC if best_value is not None else Tier.NONE,
            outcome=outcome,
            confidence=best_confidence,
            frontier_called=False,
        )
        return RouteResult(value=best_value, decision=decision)

    mode = get_ai_mode()
    if mode in (AIMode.DISABLED, AIMode.OBSERVE_ONLY):
        decision = _decide(
            feature=feature,
            tier=Tier.DETERMINISTIC if best_value is not None else Tier.NONE,
            outcome=RouteOutcome.FRONTIER_DISABLED,
            confidence=best_confidence,
            frontier_called=False,
        )
        return RouteResult(value=best_value, decision=decision)

    value = frontier_fn()
    if cache_store_fn is not None:
        cache_store_fn(value)
    decision = _decide(
        feature=feature,
        tier=Tier.FRONTIER,
        outcome=RouteOutcome.FRONTIER_CALL,
        confidence=1.0,
        frontier_called=True,
    )
    return RouteResult(value=value, decision=decision)


def cache_hit_stats(decisions: Optional[tuple[TierDecision, ...]] = None) -> dict[str, int]:
    """ADF-W5.2 acceptance evidence ("avoided-call metrics"): counts cache
    hits and frontier calls avoided by them, from the recorded decision log
    (or an explicitly supplied snapshot, e.g. from `flush_tier_decisions_to_jsonl`'s
    source). A cache hit is definitionally an avoided frontier call -- the
    two counts are always equal by construction, reported together so a
    caller doesn't have to recompute the equivalence."""
    source = decisions if decisions is not None else recorded_decisions()
    cache_hits = sum(1 for decision in source if decision.outcome == RouteOutcome.CACHE_HIT)
    frontier_calls = sum(1 for decision in source if decision.outcome == RouteOutcome.FRONTIER_CALL)
    return {
        "cache_hits": cache_hits,
        "frontier_calls_avoided_by_cache": cache_hits,
        "actual_frontier_calls": frontier_calls,
    }


def flush_tier_decisions_to_jsonl(output_path: Path) -> int:
    """Serialize all recorded tier decisions to a JSONL sidecar (WI-4.3).

    Appends one JSON line per decision to *output_path*.  Returns the number
    of decisions written.  The in-memory buffer is NOT cleared — callers may
    call ``reset_recorded_decisions()`` if they want to flush once-only.
    """
    from src.core.jsonl_utils import append_jsonl_line

    decisions = list(recorded_decisions())
    if not decisions:
        return 0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    for d in decisions:
        append_jsonl_line(
            output_path,
            json.dumps(
                {
                    "feature": d.feature,
                    "tier": d.tier.value,
                    "outcome": d.outcome.value,
                    "confidence": d.confidence,
                    "frontier_called": d.frontier_called,
                    "trace_id": d.trace_id,
                    "recorded_at": d.recorded_at,
                }
            ),
        )
    return len(decisions)
