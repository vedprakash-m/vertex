from __future__ import annotations

from src.core.consolidated_scope_policy import recommended_s0c_scope_decision


def test_recommended_s0c_scope_preserves_pilot_local_and_operator_deposit_boundary() -> None:
    decision = recommended_s0c_scope_decision()

    assert decision.security_profile == "pilot-local"
    assert decision.automation_scope == "automatic_after_deposit"


def test_recommended_s0c_scope_excludes_unimplemented_authority_domains() -> None:
    decision = recommended_s0c_scope_decision()

    assert decision.unsupported_v1_authority_domains == ("deliverable", "incident")
    assert decision.detected_but_not_authoritative_claim_types == (
        "deployment.rollback",
        "deployment.started",
        "incident.severity_changed",
    )
