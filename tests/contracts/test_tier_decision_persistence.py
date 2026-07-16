"""Contract test: every tier decision reaches a durable sink (ADF-W0.7).

Verifies that one deterministic hit and one frontier call each produce a
durable ``TierDecisionRecord`` row in the program's tier-decision store, and
that the rows survive a simulated restart (fresh read).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.ai.tiered_router import (
    RouteOutcome,
    RouteResult,
    Tier,
    TierDecision,
    register_decision_sink,
    reset_recorded_decisions,
    sink_failure_count,
)
from src.core.measurement_store import (
    TierDecisionSinkConfig,
    make_tier_decision_sink,
    read_measurements,
    tier_decision_store_path,
)
from src.core.policy_loader import AIFeaturePolicy


def _policy(frontier_eligible: bool) -> AIFeaturePolicy:
    return AIFeaturePolicy(
        max_tokens=800,
        temperature=0.2,
        model_tier="frontier",
        tier0_confidence_threshold=0.5,
        deterministic_first=True,
        frontier_eligible=frontier_eligible,
    )


def test_deterministic_hit_and_frontier_call_produce_durable_rows(tmp_path: Path) -> None:
    reset_recorded_decisions()
    config = TierDecisionSinkConfig(
        program_id="acme",
        edition_id="acme_weekly",
        run_id="run-contract",
        execution_mode="enforce",
        policy_version="1",
        programs_root=tmp_path,
    )
    sink = make_tier_decision_sink(config)
    register_decision_sink(sink)
    try:
        # Deterministic hit: lower tier answers with high confidence.
        result: RouteResult[str] = _route_deterministic_hit()
        assert result.decision.tier is Tier.DETERMINISTIC
        assert result.decision.outcome is RouteOutcome.DETERMINISTIC_HIT

        # Frontier call: no lower tier hits, frontier invoked.
        result2: RouteResult[str] = _route_frontier_call()
        assert result2.decision.tier is Tier.FRONTIER
        assert result2.decision.outcome is RouteOutcome.FRONTIER_CALL
    finally:
        from src.ai.tiered_router import unregister_decision_sink

        unregister_decision_sink(sink)

    # Durable rows exist and survive a fresh read (simulated restart).
    store = tier_decision_store_path("acme", programs_root=tmp_path)
    rows = read_measurements(store)
    assert len(rows) == 2
    tiers = [r["chosen_tier"] for r in rows]
    outcomes = [r["outcome"] for r in rows]
    assert "deterministic" in tiers
    assert "frontier" in tiers
    assert "deterministic_hit" in outcomes
    assert "frontier_call" in outcomes
    # Each row carries schema version, execution mode, and a valid checksum.
    from src.core.measurement_store import compute_record_checksum, verify_record_checksum

    for row in rows:
        assert row["schema_version"] == "1"
        assert row["execution_mode"] == "enforce"
        assert row["run_id"] == "run-contract"
        assert row["program_id"] == "acme"
        body = {k: v for k, v in row.items() if k != "record_checksum"}
        assert row["record_checksum"] == compute_record_checksum(body)
        assert verify_record_checksum(row) is True


def test_streaming_beyond_ring_capacity_persists_every_decision(tmp_path: Path) -> None:
    """The diagnostic ring must never be the durable measurement boundary."""
    reset_recorded_decisions()
    sink = make_tier_decision_sink(
        TierDecisionSinkConfig(
            program_id="acme", edition_id=None, run_id="long-run", execution_mode="enforce", programs_root=tmp_path
        )
    )
    register_decision_sink(sink)
    try:
        for _ in range(3_000):
            _route_deterministic_hit()
    finally:
        from src.ai.tiered_router import unregister_decision_sink

        unregister_decision_sink(sink)
    assert len(read_measurements(tier_decision_store_path("acme", programs_root=tmp_path))) == 3_000


def test_sink_failure_is_counted_without_breaking_routing() -> None:
    reset_recorded_decisions()

    def broken_sink(_decision: TierDecision) -> None:
        raise OSError("disk full")

    register_decision_sink(broken_sink)
    try:
        result = _route_deterministic_hit()
    finally:
        from src.ai.tiered_router import unregister_decision_sink

        unregister_decision_sink(broken_sink)
    assert result.value == "det-answer"
    assert sink_failure_count() == 1


def _route_deterministic_hit() -> RouteResult[str]:
    from src.ai.tiered_router import TierResult, route_through_tiers

    return route_through_tiers(
        "test_feature",
        deterministic_fn=lambda: TierResult(value="det-answer", confidence=0.99),
        frontier_fn=lambda: "frontier-answer",
        policy=_policy(frontier_eligible=True),
    )


def _route_frontier_call() -> RouteResult[str]:
    from src.ai.tiered_router import route_through_tiers

    return route_through_tiers(
        "test_feature",
        deterministic_fn=lambda: None,
        frontier_fn=lambda: "frontier-answer",
        policy=_policy(frontier_eligible=True),
    )
