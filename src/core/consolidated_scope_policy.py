from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ConsolidatedScopeDecision:
    """Executable recommendation for S-0c.

    This is diagnostic policy, not approval.  Human Product/Governance must
    accept the recommendation before scope language is made normative.
    """

    security_profile: str
    automation_scope: str
    unsupported_v1_authority_domains: tuple[str, ...]
    detected_but_not_authoritative_claim_types: tuple[str, ...]
    rationale: str


def recommended_s0c_scope_decision() -> ConsolidatedScopeDecision:
    return ConsolidatedScopeDecision(
        security_profile="pilot-local",
        automation_scope="automatic_after_deposit",
        unsupported_v1_authority_domains=("deliverable", "incident"),
        detected_but_not_authoritative_claim_types=(
            "deployment.rollback",
            "deployment.started",
            "incident.severity_changed",
        ),
        rationale=(
            "Preserve the local-first security envelope, keep source delivery operator-driven, "
            "and avoid claiming v1 authority for domains without complete source-policy and "
            "projection support."
        ),
    )
