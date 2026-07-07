from __future__ import annotations

from datetime import datetime, timezone

from src.core.acceptance_truth_policy import (
    recommended_acceptance_truth_decision_by_tier,
    recommended_acceptance_truth_decisions,
)
from src.core.ledger.event_log import ConfidenceTier, EventEnvelope, TemporalConfidence
from src.core.ledger.fact_bridge import bridge_fact_controls_for_event, build_bridge_fact_input
from src.core.ledger.source_refs import OperatorAssertionRef
from src.core.truth_levels import TruthLevel
from src.core.truth_model import TruthContext, derive_truth_level, load_source_authority_policy


def _event(confidence: ConfidenceTier, *, actor: str = "operator") -> EventEnvelope:
    now = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)
    return EventEnvelope(
        event_id=f"evt-{confidence.value}",
        program_id="nova",
        event_type="risk.raised.v1",
        occurred_at=now,
        recorded_at=now,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=confidence,
        actor=actor,
        payload={"risk_id": "risk:r1", "title": "Risk one"},
        source_ref=OperatorAssertionRef(asserted_by=actor, asserted_at=now),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )


def _empty_context() -> TruthContext:
    return TruthContext(
        baseline_locked_keys=frozenset(),
        suspended_sources=frozenset(),
        corroborated_keys=frozenset(),
    )


def test_acceptance_truth_policy_decisions_every_confidence_tier() -> None:
    decisions = recommended_acceptance_truth_decision_by_tier()

    assert set(decisions) == set(ConfidenceTier)


def test_acceptance_truth_policy_matches_live_bridge_controls() -> None:
    decisions = recommended_acceptance_truth_decision_by_tier()

    for confidence in ConfidenceTier:
        event = _event(confidence, actor="alex")
        controls = bridge_fact_controls_for_event(event)
        decision = decisions[confidence]

        assert controls.precedence == decision.precedence
        assert controls.review_state == decision.review_state
        assert controls.write_authority == decision.write_authority
        assert (controls.accepted_by is not None) is decision.accepted_by_required


def test_acceptance_truth_policy_matches_derived_truth_for_representative_source_fixtures() -> None:
    policy = load_source_authority_policy()
    fixture_by_tier = {
        ConfidenceTier.OPERATOR_CONFIRMED: ("ado", TruthLevel.HUMAN_CONFIRMED),
        ConfidenceTier.SOURCE_AUTHORITATIVE: ("ado", TruthLevel.SOURCE_VALIDATED),
        ConfidenceTier.AI_EXTRACTED: ("workiq", TruthLevel.RAW_OBSERVED),
        ConfidenceTier.INFERRED: ("workiq", TruthLevel.RAW_OBSERVED),
    }

    for confidence, (source, expected) in fixture_by_tier.items():
        fact = build_bridge_fact_input(
            _event(confidence, actor="alex"),
            fact_type="action.item",
            entity_refs=("WI:123",),
            payload={"source": source, "title": "Action one"},
        )

        assert derive_truth_level(fact, _empty_context(), policy=policy) == expected


def test_acceptance_truth_policy_keeps_acceptance_as_fact_state_not_second_fact() -> None:
    for decision in recommended_acceptance_truth_decisions():
        assert decision.precedence.value
        assert decision.review_state.value
        assert decision.write_authority in {"bridge", "human"}
