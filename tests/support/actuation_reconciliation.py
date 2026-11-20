from __future__ import annotations

from datetime import datetime, timezone

from src.core.actuation_reconciliation import (
    AuthorizedEffect,
    EffectReconciliation,
    ProviderEffectEvidence,
    reconcile_effect,
)


_NOW = datetime(2026, 11, 19, 18, 0, tzinfo=timezone.utc)


def synthetic_authorized_effect_document(
    *,
    program_id: str = "fixture",
    intent_id: str = "intent-1",
    operation: str = "create_task.v1",
    tenant_id: str = "org",
    project: str = "project",
) -> dict[str, object]:
    return {
        "program_id": program_id,
        "intent_id": intent_id,
        "approval_id": "approval-1",
        "provider": "ado",
        "tenant_id": tenant_id,
        "project": project,
        "operation": operation,
        "target_identity": f"{project}:new",
        "desired_value": {"value": "synthetic"},
        "execution_principal": "operator:synthetic",
        "audience_manifest": {
            "generated_at": _NOW.isoformat(),
            "program_id": program_id,
            "entries": [],
        },
        "intended_recipients": [],
        "preflight_revision": 1,
        "precondition_observed_at": _NOW.isoformat(),
        "evidence_version": "synthetic-evidence-v1",
        "policy_version": "synthetic-policy-v1",
        "rollback_mechanism": "none",
    }


def synthetic_completed_reconciliation(
    authorized_document: dict[str, object],
    *,
    remote_id: int,
    remote_rev: int = 2,
) -> EffectReconciliation:
    authorized = AuthorizedEffect.from_dict(authorized_document)
    evidence = ProviderEffectEvidence(
        intent_id=authorized.intent_id,
        approval_id=authorized.approval_id,
        provider=authorized.provider,
        tenant_id=authorized.tenant_id,
        project=authorized.project,
        operation=authorized.operation,
        recipient=None,
        target_identity=authorized.target_identity,
        remote_effect_id=str(remote_id),
        resulting_revision=remote_rev,
        preflight_revision=authorized.preflight_revision,
        observed_value=authorized.desired_value,
        execution_principal=authorized.execution_principal,
        provider_request_id=f"request-{remote_id}",
        provider_correlation_id=authorized.intent_id,
        response_hash=f"sha256:response-{remote_id}",
        attempt_count=1,
        dispatched_at=_NOW,
        observed_at=_NOW,
        evidence_version=authorized.evidence_version,
        policy_version=authorized.policy_version,
    )
    return reconcile_effect(authorized, (evidence,))
