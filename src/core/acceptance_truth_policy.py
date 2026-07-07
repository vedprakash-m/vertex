from __future__ import annotations

from dataclasses import dataclass

from src.core.ledger.event_log import ConfidenceTier
from src.core.program_fact_store import FactPrecedence, FactReviewState
from src.core.truth_levels import TruthLevel


@dataclass(frozen=True, slots=True)
class AcceptanceTruthDecision:
    """Executable recommendation for S-0d/PS-J truth semantics."""

    confidence_tier: ConfidenceTier
    precedence: FactPrecedence
    review_state: FactReviewState
    write_authority: str
    accepted_by_required: bool
    derived_truth_expectation: TruthLevel | str
    rationale: str


def recommended_acceptance_truth_decisions() -> tuple[AcceptanceTruthDecision, ...]:
    return (
        AcceptanceTruthDecision(
            confidence_tier=ConfidenceTier.OPERATOR_CONFIRMED,
            precedence=FactPrecedence.ACTIVE_PM_JUDGMENT,
            review_state=FactReviewState.ACCEPTED,
            write_authority="human",
            accepted_by_required=True,
            derived_truth_expectation=TruthLevel.HUMAN_CONFIRMED,
            rationale="An operator-confirmed event is the confirm-loop transition on the fact.",
        ),
        AcceptanceTruthDecision(
            confidence_tier=ConfidenceTier.SOURCE_AUTHORITATIVE,
            precedence=FactPrecedence.VERIFIED_SYSTEM_SIGNAL,
            review_state=FactReviewState.ACCEPTED,
            write_authority="bridge",
            accepted_by_required=False,
            derived_truth_expectation="source_validated_when_primary_authority_else_raw_observed",
            rationale="A source-authoritative event is accepted as a system signal, not as human judgment.",
        ),
        AcceptanceTruthDecision(
            confidence_tier=ConfidenceTier.AI_EXTRACTED,
            precedence=FactPrecedence.RAW_TELEMETRY,
            review_state=FactReviewState.PROPOSED,
            write_authority="bridge",
            accepted_by_required=False,
            derived_truth_expectation=TruthLevel.RAW_OBSERVED,
            rationale="AI extraction creates proposed telemetry until accepted or corroborated.",
        ),
        AcceptanceTruthDecision(
            confidence_tier=ConfidenceTier.INFERRED,
            precedence=FactPrecedence.RAW_TELEMETRY,
            review_state=FactReviewState.PROPOSED,
            write_authority="bridge",
            accepted_by_required=False,
            derived_truth_expectation=TruthLevel.RAW_OBSERVED,
            rationale="Inference is proposed telemetry and must not become accepted fact by construction.",
        ),
    )


def recommended_acceptance_truth_decision_by_tier() -> dict[ConfidenceTier, AcceptanceTruthDecision]:
    return {
        decision.confidence_tier: decision
        for decision in recommended_acceptance_truth_decisions()
    }
