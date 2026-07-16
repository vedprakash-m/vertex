from __future__ import annotations

import pytest

from src.ai.ai_mode import AIMode, set_ai_mode
from src.core.policy_loader import AIFeaturePolicy
from src.ai.tiered_router import (
    RouteOutcome,
    Tier,
    TierResult,
    cache_hit_stats,
    recorded_decisions,
    register_decision_sink,
    reset_recorded_decisions,
    route_through_tiers,
    unregister_decision_sink,
)


def _policy(**overrides: object) -> AIFeaturePolicy:
    base = dict(
        max_tokens=500,
        temperature=0.0,
        model_tier="standard",
        frontier_eligible=True,
        deterministic_first=True,
        tier0_confidence_threshold=0.9,
    )
    base.update(overrides)
    return AIFeaturePolicy(**base)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _isolate_router_state():
    reset_recorded_decisions()
    set_ai_mode(AIMode.ACTIVE)
    yield
    reset_recorded_decisions()
    set_ai_mode(AIMode.ACTIVE)


def test_confident_deterministic_hit_skips_frontier():
    frontier_called = {"n": 0}

    def frontier() -> str:
        frontier_called["n"] += 1
        return "frontier"

    result = route_through_tiers(
        "claim_extractor",
        deterministic_fn=lambda: TierResult(value="det", confidence=1.0),
        frontier_fn=frontier,
        policy=_policy(),
    )

    assert result.value == "det"
    assert result.frontier_called is False
    assert frontier_called["n"] == 0
    decisions = recorded_decisions()
    assert decisions[-1].outcome is RouteOutcome.DETERMINISTIC_HIT
    assert decisions[-1].tier is Tier.DETERMINISTIC


def test_low_confidence_deterministic_falls_through_to_frontier():
    result = route_through_tiers(
        "claim_extractor",
        deterministic_fn=lambda: TierResult(value="det", confidence=0.5),
        frontier_fn=lambda: "frontier",
        policy=_policy(),
    )
    assert result.value == "frontier"
    assert result.frontier_called is True
    assert recorded_decisions()[-1].outcome is RouteOutcome.FRONTIER_CALL


def test_local_tier_hits_before_frontier():
    frontier_called = {"n": 0}

    def frontier() -> str:
        frontier_called["n"] += 1
        return "frontier"

    result = route_through_tiers(
        "claim_extractor",
        deterministic_fn=lambda: None,
        local_fn=lambda: TierResult(value="local", confidence=0.95),
        frontier_fn=frontier,
        policy=_policy(),
    )
    assert result.value == "local"
    assert frontier_called["n"] == 0
    assert recorded_decisions()[-1].outcome is RouteOutcome.LOCAL_HIT
    assert recorded_decisions()[-1].tier is Tier.LOCAL


def test_frontier_blocked_when_not_eligible_returns_best_lower_tier():
    result = route_through_tiers(
        "claim_extractor",
        deterministic_fn=lambda: TierResult(value="det-low", confidence=0.4),
        frontier_fn=lambda: "frontier",
        policy=_policy(frontier_eligible=False),
    )
    assert result.value == "det-low"
    assert result.frontier_called is False
    assert recorded_decisions()[-1].outcome is RouteOutcome.FRONTIER_BLOCKED


def test_disabled_mode_skips_frontier_and_never_raises():
    set_ai_mode(AIMode.DISABLED)

    def frontier() -> str:  # pragma: no cover - must never be called
        raise AssertionError("frontier must not run under AIMode.DISABLED")

    result = route_through_tiers(
        "claim_extractor",
        deterministic_fn=lambda: None,
        frontier_fn=frontier,
        policy=_policy(),
    )
    assert result.value is None
    assert result.frontier_called is False
    assert recorded_decisions()[-1].outcome is RouteOutcome.FRONTIER_DISABLED


def test_observe_only_mode_skips_frontier():
    set_ai_mode(AIMode.OBSERVE_ONLY)
    result = route_through_tiers(
        "claim_extractor",
        deterministic_fn=lambda: TierResult(value="det-low", confidence=0.2),
        frontier_fn=lambda: "frontier",
        policy=_policy(),
    )
    assert result.value == "det-low"
    assert result.frontier_called is False
    assert recorded_decisions()[-1].outcome is RouteOutcome.FRONTIER_DISABLED


def test_no_fn_produces_value_records_skipped():
    result = route_through_tiers(
        "claim_extractor",
        deterministic_fn=lambda: None,
        frontier_fn=None,
        policy=_policy(),
    )
    assert result.value is None
    assert recorded_decisions()[-1].outcome is RouteOutcome.SKIPPED
    assert recorded_decisions()[-1].tier is Tier.NONE


def test_deterministic_first_false_skips_tier0():
    frontier_called = {"n": 0}

    def frontier() -> str:
        frontier_called["n"] += 1
        return "frontier"

    det_called = {"n": 0}

    def det() -> TierResult[str]:
        det_called["n"] += 1
        return TierResult(value="det", confidence=1.0)

    result = route_through_tiers(
        "claim_extractor",
        deterministic_fn=det,
        frontier_fn=frontier,
        policy=_policy(deterministic_first=False),
    )
    assert det_called["n"] == 0
    assert result.value == "frontier"
    assert frontier_called["n"] == 1


def test_frontier_fn_exceptions_propagate():
    class Boom(Exception):
        pass

    def frontier() -> str:
        raise Boom("explode")

    with pytest.raises(Boom):
        route_through_tiers(
            "claim_extractor",
            deterministic_fn=lambda: None,
            frontier_fn=frontier,
            policy=_policy(),
        )


def test_registered_sink_receives_decisions():
    captured: list[str] = []

    def sink(decision) -> None:
        captured.append(decision.outcome.value)

    register_decision_sink(sink)
    try:
        route_through_tiers(
            "claim_extractor",
            deterministic_fn=lambda: TierResult(value="det", confidence=1.0),
            frontier_fn=lambda: "frontier",
            policy=_policy(),
        )
    finally:
        unregister_decision_sink(sink)
    assert captured == ["deterministic_hit"]


def test_decision_carries_trace_run_id():
    from src.ai.llm_trace import AITraceContext, use_trace_context

    ctx = AITraceContext(edition="acme_weekly", run_id="run-123", caller="test")
    with use_trace_context(ctx):
        route_through_tiers(
            "claim_extractor",
            deterministic_fn=lambda: TierResult(value="det", confidence=1.0),
            frontier_fn=lambda: "frontier",
            policy=_policy(),
        )
    assert recorded_decisions()[-1].trace_id == "run-123"


# --- ADF-W5.2: shared result cache + CACHE_HIT (Section 8.8.3) ---


def test_cache_hit_short_circuits_everything_else():
    calls = {"det": 0, "local": 0, "frontier": 0}

    def det():
        calls["det"] += 1
        return TierResult(value="det", confidence=1.0)

    def local():
        calls["local"] += 1
        return TierResult(value="local", confidence=1.0)

    def frontier():
        calls["frontier"] += 1
        return "frontier"

    result = route_through_tiers(
        "claim_extractor",
        deterministic_fn=det,
        local_fn=local,
        frontier_fn=frontier,
        policy=_policy(),
        cache_lookup_fn=lambda: "cached-value",
    )

    assert result.value == "cached-value"
    assert calls == {"det": 0, "local": 0, "frontier": 0}
    decision = recorded_decisions()[-1]
    assert decision.outcome is RouteOutcome.CACHE_HIT
    assert decision.tier is Tier.CACHE


def test_cache_miss_falls_through_to_normal_ordering():
    result = route_through_tiers(
        "claim_extractor",
        deterministic_fn=lambda: TierResult(value="det", confidence=1.0),
        frontier_fn=lambda: "frontier",
        policy=_policy(),
        cache_lookup_fn=lambda: None,
    )
    assert result.value == "det"
    assert recorded_decisions()[-1].outcome is RouteOutcome.DETERMINISTIC_HIT


def test_cache_store_called_only_after_a_real_frontier_call():
    stored = []
    result = route_through_tiers(
        "claim_extractor",
        frontier_fn=lambda: "fresh-frontier-value",
        policy=_policy(deterministic_first=False),
        cache_lookup_fn=lambda: None,
        cache_store_fn=lambda value: stored.append(value),
    )
    assert result.value == "fresh-frontier-value"
    assert stored == ["fresh-frontier-value"]


def test_cache_store_not_called_on_deterministic_hit():
    stored = []
    route_through_tiers(
        "claim_extractor",
        deterministic_fn=lambda: TierResult(value="det", confidence=1.0),
        frontier_fn=lambda: "frontier",
        policy=_policy(),
        cache_lookup_fn=lambda: None,
        cache_store_fn=lambda value: stored.append(value),
    )
    assert stored == []


def test_cache_store_not_called_on_cache_hit_itself():
    stored = []
    route_through_tiers(
        "claim_extractor",
        frontier_fn=lambda: "frontier",
        policy=_policy(),
        cache_lookup_fn=lambda: "cached",
        cache_store_fn=lambda value: stored.append(value),
    )
    assert stored == []


def test_cache_hit_stats_counts_hits_and_avoided_calls():
    route_through_tiers(
        "feature_a", frontier_fn=lambda: "x", policy=_policy(), cache_lookup_fn=lambda: "cached"
    )
    route_through_tiers(
        "feature_b", frontier_fn=lambda: "y", policy=_policy(), cache_lookup_fn=lambda: None
    )
    stats = cache_hit_stats()
    assert stats["cache_hits"] == 1
    assert stats["frontier_calls_avoided_by_cache"] == 1
    assert stats["actual_frontier_calls"] == 1


def test_cache_hit_stats_accepts_explicit_decisions_snapshot():
    route_through_tiers(
        "feature_a", frontier_fn=lambda: "x", policy=_policy(), cache_lookup_fn=lambda: "cached"
    )
    snapshot = recorded_decisions()
    reset_recorded_decisions()
    # The live log is now empty, but the explicit snapshot still counts.
    assert cache_hit_stats()["cache_hits"] == 0
    assert cache_hit_stats(snapshot)["cache_hits"] == 1
