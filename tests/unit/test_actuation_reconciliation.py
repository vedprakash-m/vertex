"""RELO-W5.9 synthetic post-dispatch reconciliation contracts."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.core.actuation_reconciliation import (
    AuthorizedEffect,
    EffectReconciliation,
    ProviderEffectEvidence,
    desired_effect_hash,
    reconcile_effect,
)
from src.core.audience_manifest import AudienceManifest, AudienceManifestEntry, DISPOSITION_INCLUDED


_NOW = datetime(2026, 11, 19, 18, 0, tzinfo=timezone.utc)


def _manifest(*recipients: str) -> AudienceManifest:
    return AudienceManifest(
        generated_at=_NOW,
        program_id="fixture",
        entries=tuple(
            AudienceManifestEntry(
                person_entity_id=recipient,
                scope_id="leaders",
                disposition=DISPOSITION_INCLUDED,
                reason="synthetic fixture",
            )
            for recipient in recipients
        ),
    )


def _authorized(*recipients: str) -> AuthorizedEffect:
    return AuthorizedEffect(
        program_id="fixture",
        intent_id="intent-1",
        approval_id="approval-1",
        provider="ado",
        tenant_id="tenant-1",
        project="project-1",
        operation="create_task.v1",
        target_identity="project-1:new",
        desired_value={"System.Title": "Synthetic task", "System.Tags": ("vertex-intent-intent-1",)},
        execution_principal="operator:fixture",
        audience_manifest=_manifest(*recipients),
        intended_recipients=tuple(recipients),
        preflight_revision=7,
        precondition_observed_at=_NOW,
        evidence_version="fixture-evidence-v1",
        policy_version="fixture-policy-v1",
        rollback_mechanism="none",
    )


def _authorized_document(*recipients: str) -> dict[str, object]:
    return {
        "program_id": "fixture",
        "intent_id": "intent-1",
        "approval_id": "approval-1",
        "provider": "ado",
        "tenant_id": "tenant-1",
        "project": "project-1",
        "operation": "create_task.v1",
        "target_identity": "project-1:new",
        "desired_value": {
            "System.Title": "Synthetic task",
            "System.Tags": ["vertex-intent-intent-1"],
        },
        "execution_principal": "operator:fixture",
        "audience_manifest": {
            "generated_at": _NOW.isoformat(),
            "program_id": "fixture",
            "entries": [
                {
                    "person_entity_id": recipient,
                    "scope_id": "leaders",
                    "disposition": DISPOSITION_INCLUDED,
                    "reason": "synthetic fixture",
                }
                for recipient in recipients
            ],
        },
        "intended_recipients": list(recipients),
        "preflight_revision": 7,
        "precondition_observed_at": _NOW.isoformat(),
        "evidence_version": "fixture-evidence-v1",
        "policy_version": "fixture-policy-v1",
        "rollback_mechanism": "none",
    }


def _evidence(
    *,
    recipient: str | None = None,
    intent_id: str = "intent-1",
    approval_id: str = "approval-1",
    target_identity: str = "project-1:new",
    observed_value: object | None = None,
    principal: str = "operator:fixture",
) -> ProviderEffectEvidence:
    return ProviderEffectEvidence(
        intent_id=intent_id,
        approval_id=approval_id,
        provider="ado",
        tenant_id="tenant-1",
        project="project-1",
        operation="create_task.v1",
        recipient=recipient,
        target_identity=target_identity,
        remote_effect_id="work-item-42",
        resulting_revision=8,
        preflight_revision=7,
        observed_value=(
            {"System.Title": "Synthetic task", "System.Tags": ("vertex-intent-intent-1",)}
            if observed_value is None
            else observed_value
        ),
        execution_principal=principal,
        provider_request_id="request-1",
        provider_correlation_id="correlation-1",
        response_hash="sha256:response-1",
        attempt_count=1,
        dispatched_at=_NOW,
        observed_at=_NOW,
        evidence_version="fixture-evidence-v1",
        policy_version="fixture-policy-v1",
    )


def test_exact_effect_and_each_authorized_recipient_are_certified() -> None:
    result = reconcile_effect(
        _authorized("alice", "bob"),
        (_evidence(recipient="alice"), _evidence(recipient="bob")),
    )
    assert result.status == "completed"
    assert result.completed is True
    assert [receipt.recipient for receipt in result.receipts] == ["alice", "bob"]
    assert all(receipt.terminal_status == "completed" for receipt in result.receipts)
    assert not result.mismatches


def test_missing_recipient_remains_partial_and_never_completes() -> None:
    result = reconcile_effect(_authorized("alice", "bob"), (_evidence(recipient="alice"),))
    assert result.status == "partial"
    assert result.completed is False
    bob = result.receipt_for("bob")
    assert bob is not None and bob.terminal_status == "uncertain_remote_state"
    assert any("recipient" in mismatch for mismatch in result.mismatches)


@pytest.mark.parametrize(
    ("change", "mismatch_label"),
    [
        ({"recipient": "mallory"}, "recipient"),
        ({"recipient": "alice", "target_identity": "wrong-target"}, "target"),
        ({"recipient": "alice", "observed_value": {"System.Title": "Wrong"}}, "value"),
        ({"recipient": "alice", "principal": "operator:someone-else"}, "principal"),
    ],
)
def test_wrong_provider_result_is_not_certifiable(change: dict[str, object], mismatch_label: str) -> None:
    result = reconcile_effect(_authorized("alice"), (_evidence(**change),))
    assert result.status != "completed"
    assert result.completed is False
    assert any(mismatch_label in mismatch for mismatch in result.mismatches)


def test_intended_recipient_outside_authorized_manifest_blocks() -> None:
    expected = replace(_authorized("alice"), audience_manifest=_manifest("bob"))
    result = reconcile_effect(expected, (_evidence(recipient="alice"),))
    assert result.status == "failed_terminal"
    assert result.completed is False
    assert result.receipts == ()
    assert any("audience" in mismatch for mismatch in result.mismatches)


def test_receiver_receipt_exposes_required_terminal_evidence() -> None:
    result = reconcile_effect(_authorized("alice"), (_evidence(recipient="alice"),))
    receipt = result.receipt_for("alice")
    assert receipt is not None
    document = receipt.to_dict()
    for key in (
        "intent_id",
        "approval_id",
        "provider",
        "tenant_id",
        "project",
        "operation",
        "desired_effect_hash",
        "preflight_revision",
        "provider_request_id",
        "provider_correlation_id",
        "remote_effect_id",
        "resulting_revision",
        "response_hash",
        "attempt_count",
        "terminal_status",
        "dispatched_at",
        "observed_at",
        "rollback_mechanism",
        "evidence_version",
        "policy_version",
        "execution_principal",
        "audience_manifest_hash",
        "authorized_audience_recipients",
        "recipient",
    ):
        assert key in document


def test_completed_label_without_child_receipts_is_not_certified() -> None:
    result = EffectReconciliation(status="completed", receipts=(), mismatches=())
    assert result.completed is False


def test_completed_label_with_target_mismatch_is_not_certified() -> None:
    completed = reconcile_effect(_authorized("alice"), (_evidence(recipient="alice"),))
    mismatched = replace(completed, mismatches=("target mismatch",))
    assert mismatched.completed is False


def test_authorized_effect_document_round_trips_exact_binding() -> None:
    expected = AuthorizedEffect.from_dict(_authorized_document("alice"))
    original = _authorized("alice")
    assert expected.intent_id == original.intent_id
    assert expected.approval_id == original.approval_id
    assert desired_effect_hash(expected.desired_value) == desired_effect_hash(original.desired_value)
    assert expected.audience_manifest.included_person_entity_ids == ("alice",)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("program_id", 1),
        ("desired_value", []),
        ("audience_manifest", []),
        ("intended_recipients", "alice"),
        ("preflight_revision", True),
        ("precondition_observed_at", 1),
        ("precondition_observed_at", "invalid"),
    ],
)
def test_authorized_effect_document_refuses_invalid_binding(
    field: str, value: object,
) -> None:
    document = _authorized_document("alice")
    document[field] = value
    with pytest.raises(ValueError):
        AuthorizedEffect.from_dict(document)


def test_authorized_effect_document_refuses_missing_binding() -> None:
    document = _authorized_document()
    document.pop("approval_id")
    with pytest.raises(ValueError, match="missing"):
        AuthorizedEffect.from_dict(document)


@pytest.mark.parametrize(
    "manifest",
    [
        {},
        {"generated_at": "invalid", "program_id": "fixture", "entries": []},
        {"generated_at": _NOW.isoformat(), "program_id": "fixture", "entries": ["alice"]},
        {
            "generated_at": _NOW.isoformat(),
            "program_id": "fixture",
            "entries": [{"person_entity_id": 1, "scope_id": "leaders", "disposition": "included", "reason": "x"}],
        },
    ],
)
def test_authorized_effect_document_refuses_invalid_audience_manifest(
    manifest: object,
) -> None:
    document = _authorized_document()
    document["audience_manifest"] = manifest
    with pytest.raises(ValueError):
        AuthorizedEffect.from_dict(document)


def test_preflight_refuses_all_unbound_authorization_dimensions() -> None:
    manifest = AudienceManifest(
        generated_at=_NOW,
        program_id="foreign",
        entries=(
            AudienceManifestEntry(
                person_entity_id="alice",
                scope_id="leaders",
                disposition=DISPOSITION_INCLUDED,
                reason="synthetic",
            ),
        ),
    )
    expected = replace(
        _authorized("alice"),
        program_id="",
        intent_id="",
        approval_id="",
        provider="",
        tenant_id="",
        project="",
        operation="",
        target_identity="",
        execution_principal="",
        audience_manifest=manifest,
        intended_recipients=("bob", "bob", ""),
        preflight_revision=0,
        precondition_observed_at=datetime(2026, 11, 18, 18, tzinfo=timezone.utc),
        evidence_version="",
        policy_version="",
        rollback_mechanism="",
        desired_value={"score": float("nan")},
    )
    result = reconcile_effect(expected, ())
    assert result.status == "failed_terminal"
    assert len(result.mismatches) >= 15


def test_duplicate_and_unexpected_provider_evidence_cannot_complete() -> None:
    expected = _authorized("alice")
    result = reconcile_effect(
        expected,
        (
            _evidence(recipient="alice"),
            _evidence(recipient="alice"),
            _evidence(recipient="mallory"),
        ),
    )
    assert result.status == "uncertain_remote_state"
    assert result.completed is False
    assert any("duplicate" in mismatch for mismatch in result.mismatches)
    assert any("unexpected" in mismatch for mismatch in result.mismatches)
    unexpected_receipt = result.receipt_for("mallory")
    assert unexpected_receipt is not None
    assert unexpected_receipt.terminal_status == "uncertain_remote_state"


def test_non_recipient_effect_requires_one_parent_evidence() -> None:
    result = reconcile_effect(_authorized(), (_evidence(),))
    assert result.completed is True
    assert result.receipt_for(None) is not None
    assert result.receipt_for("alice") is None


def test_provider_evidence_rejects_invalid_receipt_dimensions() -> None:
    evidence = replace(
        _evidence(recipient="alice"),
        resulting_revision=0,
        preflight_revision=1,
        remote_effect_id="",
        provider_request_id="",
        provider_correlation_id="",
        response_hash="",
        attempt_count=0,
        dispatched_at=datetime(2026, 11, 19, 18, 0),
        observed_at=datetime(2026, 11, 18, 18, tzinfo=timezone.utc),
        observed_value={"score": float("nan")},
    )
    result = reconcile_effect(_authorized("alice"), (evidence,))
    assert result.status == "uncertain_remote_state"
    assert result.receipts[0].terminal_status == "uncertain_remote_state"
    assert len(result.mismatches) >= 8
