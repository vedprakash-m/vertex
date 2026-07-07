from __future__ import annotations

from src.ai.rev.extractor import MATERIAL_EVENT_TYPES
from src.core.rev.authority_scope import (
    assess_rev_authority_scope,
    recommended_v1_authoritative_claim_types,
)
from src.core.truth_model import load_source_authority_policy


def test_authority_scope_covers_every_material_rev_event_type() -> None:
    policy = load_source_authority_policy()
    assessments = assess_rev_authority_scope(policy)

    assert {assessment.claim_event_type for assessment in assessments} == set(MATERIAL_EVENT_TYPES)


def test_recommended_s0g_scope_final_v1_authoritative_count_is_four() -> None:
    policy = load_source_authority_policy()

    assert recommended_v1_authoritative_claim_types(policy) == frozenset({
        "commitment.date_set",
        "deployment.completed",
        "milestone.completed",
        "ownership.changed",
    })


def test_risk_blocking_milestone_is_detected_but_not_authoritative_without_judgment_human_comms() -> None:
    policy = load_source_authority_policy()
    assessments = {
        assessment.claim_event_type: assessment
        for assessment in assess_rev_authority_scope(policy)
    }

    risk = assessments["risk.blocking_milestone"]
    assert risk.authority_family == "judgment"
    assert risk.status == "recommended_unsupported_v1"
    assert "judgment" in risk.reason
    assert "human_comms" in risk.reason


def test_q9_events_are_detected_but_not_authoritative() -> None:
    policy = load_source_authority_policy()
    assessments = {
        assessment.claim_event_type: assessment
        for assessment in assess_rev_authority_scope(policy)
    }

    for claim_type in ("deployment.rollback", "deployment.started", "incident.severity_changed"):
        assert assessments[claim_type].status == "recommended_unsupported_v1"
        assert assessments[claim_type].accessor is None
