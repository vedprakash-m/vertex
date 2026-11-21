from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.actuation_approval_store import (
    load_marker_outbox_id,
)
from src.core.actuation_outbox import (
    ActuationAdmissionRequest,
    DispatchResult,
    create_actuation_approval,
    create_task_idempotency_key,
    dispatch_approved_actuation_intent,
    dispatch_leased_create_task,
    effect_id_v1,
    enqueue_approved_actuation_intent,
    enqueue_create_task_intent,
    outbox_db_path,
)
from src.core.actuation_reconciliation import (
    ReconciliationDisposition,
    ReconciliationObservation,
    reconcile_uncertain_effect,
    request_compensation,
)
from src.core.ledger.durable_outbox_store import (
    OutboxError,
    load_entry,
    load_latest_receipt,
    load_success_receipt,
)
from src.core.operation_capabilities import (
    approved_remote_marker,
    create_target_identity,
    lookup_capability,
    semantic_effect_key,
)
from src.core.operator_identity import OperatorIdentity
from tests.support.actuation_reconciliation import (
    synthetic_authorized_effect_document,
    synthetic_completed_reconciliation,
)
from src.m365.ado_writer import _authorized_create_effect, _build_create_task_patch


_NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
_PROGRAM = "fixture"


def _request(*, intent_id: str = "intent-1") -> ActuationAdmissionRequest:
    desired = {"text": "review this item"}
    target = "42"
    payload = {
        "program_id": _PROGRAM,
        "operation": "add_comment.v1",
        "operation_intent_id": intent_id,
        "proposal_id": "proposal-1",
        "org": "contoso",
        "project": "One",
        "work_item_id": 42,
        "desired_effect": desired,
        "authorized_effect": synthetic_authorized_effect_document(
            program_id=_PROGRAM,
            intent_id=intent_id,
            operation="add_comment.v1",
            tenant_id="contoso",
            project="One",
        ),
    }
    authorized = payload["authorized_effect"]
    assert isinstance(authorized, dict)
    authorized.update(
        {
            "target_identity": "42",
            "desired_value": desired,
            "execution_principal": r"CONTOSO\reviewer",
            "evidence_version": "snapshot-17",
            "policy_version": "policy-7",
        }
    )
    payload["approved_remote_marker"] = approved_remote_marker(
        operation="add_comment.v1",
        semantic_effect_key=semantic_effect_key(
            program_id=_PROGRAM,
            provider="ado",
            tenant="contoso",
            project="One",
            operation="add_comment.v1",
            target_identity="42",
            causal_occurrence_id="meeting:action-1",
            payload=desired,
        ),
    )
    return ActuationAdmissionRequest(
        program_id=_PROGRAM,
        intent_id=intent_id,
        proposal_id="proposal-1",
        provider="ado",
        tenant="contoso",
        project="One",
        operation="add_comment.v1",
        target_identity=target,
        causal_occurrence_id="meeting:action-1",
        desired_effect=desired,
        payload_json=json.dumps(payload, sort_keys=True),
        approval_policy_version="policy-7",
        rate_scope="proposal-1",
        rate_limit=2,
        blast_radius=1,
        scope_enabled=True,
        rule_enabled=True,
        expected_revision=8,
        expected_state="Active",
        evidence_snapshot_id="snapshot-17",
        evidence_expires_at=_NOW + timedelta(minutes=30),
    )


def _identity() -> OperatorIdentity:
    return OperatorIdentity(
        actor="reviewer@example.com",
        principal=r"CONTOSO\reviewer",
        machine="reviewer-host",
        session="session-1",
    )


def _approve_and_enqueue(root: Path):
    request = _request()
    approval = create_actuation_approval(
        root,
        request=request,
        approver=_identity(),
        approved_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
        approval_id="approval-1",
    )
    entry = enqueue_approved_actuation_intent(
        root,
        request=request,
        approval=approval,
        now=_NOW,
    )
    return request, approval, entry


def _preflight(request: ActuationAdmissionRequest):
    from src.core.actuation_approval_store import ActuationPreflightObservation

    return ActuationPreflightObservation(
        evidence_snapshot_id=request.evidence_snapshot_id,
        target_count=1,
        current_revision=request.expected_revision,
        current_state=request.expected_state,
        marker_found=False,
        observed_at=_NOW,
    )


def _enqueue_create(root: Path, *, intent_id: str = "intent-1"):
    key = create_task_idempotency_key(
        program_id=_PROGRAM,
        org="contoso",
        project="One",
        operation_intent_id=intent_id,
    )
    payload = {
        "program_id": _PROGRAM,
        "operation": "create_task.v1",
        "operation_intent_id": intent_id,
        "proposal_id": "proposal-1",
        "org": "contoso",
        "project": "One",
        "title": "Synthetic",
        "tags": f"vertex-intent-{intent_id}",
        "authorized_effect": synthetic_authorized_effect_document(
            program_id=_PROGRAM,
            intent_id=intent_id,
            tenant_id="contoso",
            project="One",
        ),
    }
    entry = enqueue_create_task_intent(
        program_id=_PROGRAM,
        idempotency_key=key,
        operation_intent_id=intent_id,
        proposal_id="proposal-1",
        payload_json=json.dumps(payload),
        programs_root=root,
    )
    return key, entry, payload


def _success(entry, payload: dict[str, object], *, remote_id: int = 42):
    return DispatchResult(
        succeeded=True,
        remote_id=remote_id,
        remote_rev=2,
        reconciliation=synthetic_completed_reconciliation(
            payload["authorized_effect"],  # type: ignore[arg-type]
            remote_id=remote_id,
            remote_rev=2,
        ),
    )


def test_effect_identity_is_approval_and_audience_independent() -> None:
    def identity(
        *,
        approval_id: str = "approval-1",
        audience: str = "ado:contoso/One",
        payload: dict[str, object] | None = None,
    ) -> str:
        return effect_id_v1(
            operation="create_task.v1",
            target="contoso/One:new",
            payload=payload or {"title": "Ship"},
            approval_id=approval_id,
            audience=audience,
        )

    expected = identity()
    assert expected == identity(approval_id="approval-2")
    assert expected == identity(audience="teams:channel")
    assert expected != identity(payload={"title": "Do not ship"})


def test_enrollment_atomically_reserves_approved_marker(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    request, approval, entry = _approve_and_enqueue(root)
    db_path = outbox_db_path(_PROGRAM, programs_root=root)
    marker = approved_remote_marker(
        operation=request.operation,
        semantic_effect_key=approval.semantic_effect_key,
    )
    assert marker is not None

    assert load_marker_outbox_id(db_path, marker) == entry.outbox_id
    assert entry.status == "pending"


def test_changed_marker_reservation_blocks_dispatch_before_callback(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    request, approval, entry = _approve_and_enqueue(root)
    db_path = outbox_db_path(_PROGRAM, programs_root=root)
    marker = approved_remote_marker(
        operation=request.operation,
        semantic_effect_key=approval.semantic_effect_key,
    )
    assert marker is not None
    from src.core._db import open_program_db_with_retry

    with open_program_db_with_retry(db_path, durability="strict") as connection:
        connection.execute(
            "UPDATE outbox_marker_reservations SET outbox_id = ? WHERE marker_value = ?",
            ("other-row", marker),
        )
    calls: list[str] = []

    def dispatched(retained) -> DispatchResult:
        calls.append(retained.outbox_id)
        return DispatchResult(succeeded=True, remote_id=42)

    outcome = dispatch_approved_actuation_intent(
        program_id=_PROGRAM,
        outbox_id=entry.outbox_id,
        owner="worker",
        dispatch_fn=dispatched,
        preflight_fn=lambda retained: _preflight(request),
        programs_root=root,
        approval_policy_version=request.approval_policy_version,
        scope_enabled=True,
        rule_enabled=True,
        now=_NOW,
    )

    assert outcome.status != "completed"
    assert "marker" in (outcome.entry.failure_reason or "").lower()
    assert calls == []


def test_confirmed_reconciliation_requires_authorized_effect_evidence(
    tmp_path: Path,
) -> None:
    key, _, payload = _enqueue_create(tmp_path / "programs")
    db_path = outbox_db_path(_PROGRAM, programs_root=tmp_path / "programs")
    uncertain = dispatch_leased_create_task(
        program_id=_PROGRAM,
        idempotency_key=key,
        owner="worker",
        dispatch_fn=lambda _entry: DispatchResult(
            succeeded=False,
            uncertain=True,
            failure_reason="synthetic response loss",
        ),
        programs_root=tmp_path / "programs",
    )
    assert uncertain.status == "uncertain_remote_state"

    unresolved = reconcile_uncertain_effect(
        db_path,
        key,
        reconciled_by="synthetic-provider",
        provider_lookup=lambda _entry: ReconciliationObservation(
            ReconciliationDisposition.CONFIRMED,
            "marker found",
            remote_id=42,
        ),
    )
    assert unresolved.status == "uncertain_remote_state"

    completed = reconcile_uncertain_effect(
        db_path,
        key,
        reconciled_by="synthetic-provider",
        provider_lookup=lambda _entry: ReconciliationObservation(
            ReconciliationDisposition.CONFIRMED,
            "marker and value found",
            remote_id=42,
            effect_reconciliation=synthetic_completed_reconciliation(
                payload["authorized_effect"], remote_id=42
            ),
        ),
    )
    assert completed.status == "completed"
    assert load_success_receipt(db_path, key) is not None


def test_confirmed_reconciliation_refuses_incomplete_receipt_dimensions(
    tmp_path: Path,
) -> None:
    key, _, payload = _enqueue_create(tmp_path / "programs")
    db_path = outbox_db_path(_PROGRAM, programs_root=tmp_path / "programs")
    dispatch_leased_create_task(
        program_id=_PROGRAM,
        idempotency_key=key,
        owner="worker",
        dispatch_fn=lambda _entry: DispatchResult(
            succeeded=False,
            uncertain=True,
            failure_reason="synthetic response loss",
        ),
        programs_root=tmp_path / "programs",
    )
    completed = synthetic_completed_reconciliation(
        payload["authorized_effect"],  # type: ignore[arg-type]
        remote_id=42,
    )
    tampered = replace(
        completed,
        receipts=(
            replace(
                completed.receipts[0],
                response_hash="",
                evidence_version="unbound-evidence",
            ),
        ),
    )
    outcome = reconcile_uncertain_effect(
        db_path,
        key,
        reconciled_by="synthetic-provider",
        provider_lookup=lambda _entry: ReconciliationObservation(
            ReconciliationDisposition.CONFIRMED,
            "marker found",
            remote_id=42,
            effect_reconciliation=tampered,
        ),
    )
    assert outcome.status == "uncertain_remote_state"
    naive = replace(
        completed,
        receipts=(
            replace(
                completed.receipts[0],
                dispatched_at=datetime(2026, 11, 19, 18, 0),
            ),
        ),
    )
    outcome = reconcile_uncertain_effect(
        db_path,
        key,
        reconciled_by="synthetic-provider",
        provider_lookup=lambda _entry: ReconciliationObservation(
            ReconciliationDisposition.CONFIRMED,
            "marker found",
            remote_id=42,
            effect_reconciliation=naive,
        ),
    )
    assert outcome.status == "uncertain_remote_state"


def test_approved_create_uses_canonical_origin_target_identity() -> None:
    fields = {
        "program_id": _PROGRAM,
        "operation_intent_id": "intent-1",
        "org": "contoso",
        "project": "One",
        "title": "Synthetic",
        "tags": "vertex-intent-intent-1",
        "causal_occurrence_id": "meeting:action-1",
        "authorized_effect": synthetic_authorized_effect_document(
            program_id=_PROGRAM,
            intent_id="intent-1",
            tenant_id="contoso",
            project="One",
        ),
    }
    desired = {
        item["path"].removeprefix("/fields/"): item["value"]
        for item in _build_create_task_patch(fields)
    }
    fields["authorized_effect"]["target_identity"] = create_target_identity(
        provider="ado",
        tenant="contoso",
        project="One",
        causal_occurrence_id="meeting:action-1",
        payload=desired,
    )
    fields["authorized_effect"]["desired_value"] = desired
    fields["approved_remote_marker"] = "vertex-effect-qualified"

    authorized = _authorized_create_effect(fields)
    assert authorized.target_identity == fields["authorized_effect"]["target_identity"]


def test_audited_restart_completes_from_receipt_without_provider_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "programs"
    key, _, payload = _enqueue_create(root)
    import src.core.actuation_outbox as service
    original_completed = service.mark_completed
    monkeypatch.setattr(
        service,
        "mark_completed",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("crash after audit")),
    )
    with pytest.raises(RuntimeError, match="crash after audit"):
        dispatch_leased_create_task(
            program_id=_PROGRAM,
            idempotency_key=key,
            owner="worker-a",
            dispatch_fn=lambda entry: _success(entry, payload),
            programs_root=root,
        )

    db_path = outbox_db_path(_PROGRAM, programs_root=root)
    retained = load_entry(db_path, key)
    assert retained is not None and retained.status == "audited"
    from src.core._db import open_program_db_with_retry
    with open_program_db_with_retry(db_path, durability="strict") as connection:
        connection.execute(
            "UPDATE outbox SET lease_expires_at = '2000-01-01T00:00:00Z' WHERE outbox_id = ?",
            (key,),
        )
    monkeypatch.setattr(service, "mark_completed", original_completed)
    outcome = dispatch_leased_create_task(
        program_id=_PROGRAM,
        idempotency_key=key,
        owner="worker-b",
        dispatch_fn=lambda _entry: (_ for _ in ()).throw(
            AssertionError("provider callback must not run during audited restart")
        ),
        programs_root=root,
    )
    assert outcome.status == "completed"
    assert outcome.remote_id == 42


def test_compensation_receipt_names_registered_inverse_action(tmp_path: Path) -> None:
    from src.core.ledger.durable_outbox_store import (
        enqueue,
        lease_next,
        mark_dispatched,
        mark_succeeded,
    )

    db_path = tmp_path / "outbox.db"
    entry = enqueue(
        db_path,
        "tag-1",
        domain="synthetic",
        correlation_id="tag-1",
        payload_json=json.dumps({"operation": "add_tag.v1"}),
    )
    leased = lease_next(db_path, "synthetic", owner="worker")
    assert leased is not None
    dispatched = mark_dispatched(
        db_path,
        entry.outbox_id,
        owner="worker",
        fencing_token=leased.lease_fencing_token,
    )
    succeeded = mark_succeeded(
        db_path,
        entry.outbox_id,
        owner="worker",
        fencing_token=dispatched.lease_fencing_token,
    )
    outcome = request_compensation(
        db_path,
        entry.outbox_id,
        owner="worker",
        fencing_token=succeeded.lease_fencing_token,
        reason="authorized effect mismatch",
    )
    receipt = json.loads(load_latest_receipt(db_path, entry.outbox_id) or "{}")
    assert outcome.status == "compensation_required"
    assert receipt["compensation_operation"] == "remove_tag.v1"


def test_operation_registry_leaves_teams_email_marker_decisions_unqualified() -> None:
    for operation in ("teams_message.v1", "email_message.v1"):
        capability = lookup_capability(operation)
        assert capability.supports_remote_marker is False  # type: ignore[union-attr]
        assert capability.reconcilable is False  # type: ignore[union-attr]
        assert capability.dispatchable is False  # type: ignore[union-attr]


def test_unknown_marker_and_unsupported_compensation_refuse(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    request = replace(_request(), operation="unknown.v1")
    marker = approved_remote_marker(operation=request.operation, semantic_effect_key="sek1:x")
    assert marker is None

    with pytest.raises(OutboxError, match="unknown outbox"):
        request_compensation(
            tmp_path / "missing.db",
            "missing",
            owner="worker",
            fencing_token=1,
            reason="not reached",
        )
