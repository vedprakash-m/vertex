from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.actuation_approval_store import (
    _authorized_payload_failure,
    _request_marker,
    load_marker_outbox_id,
)
from src.core.actuation_outbox import (
    ActuationAdmissionRequest,
    DispatchResult,
    _bind_effect_or_raise,
    _load_valid_success_receipt,
    _receipt_reconciliation_binds,
    _resume_success,
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
    AuthorizedEffect,
    EffectReconciliation,
    ReconciliationDisposition,
    ReconciliationObservation,
    reconcile_uncertain_effect,
    request_compensation,
    _authorized_effect_for_entry,
    _reconciliation_binds_authorized_effect,
)
from src.core.ledger.durable_outbox_store import (
    OutboxError,
    load_entry,
    load_latest_receipt,
    load_success_receipt,
    _SUCCESS_RECEIPTS_SQL,
)
from src.core.operation_capabilities import (
    CapabilityRefusal,
    EffectClass,
    OperationCapability,
    approved_remote_marker,
    create_target_identity,
    lookup_capability,
    normalize_semantic_payload,
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


def test_marker_helpers_fail_closed_without_creating_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert load_marker_outbox_id(tmp_path / "missing.db", "marker") is None
    db_path = tmp_path / "plain.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
    assert load_marker_outbox_id(db_path, "marker") is None

    import src.core.actuation_approval_store as approval_store

    monkeypatch.setattr(
        approval_store,
        "approved_remote_marker",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("invalid marker")),
    )
    with pytest.raises(OutboxError, match="invalid marker"):
        _request_marker(_request())


def test_approved_marker_must_be_retained_in_payload(tmp_path: Path) -> None:
    request = _request()
    approval = create_actuation_approval(
        tmp_path / "programs",
        request=request,
        approver=_identity(),
        approved_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
        approval_id="approval-1",
    )
    payload = json.loads(request.payload_json)
    payload.pop("approved_remote_marker")
    changed = replace(request, payload_json=json.dumps(payload))
    with pytest.raises(OutboxError, match="approved remote marker"):
        enqueue_approved_actuation_intent(
            tmp_path / "programs",
            request=changed,
            approval=replace(
                approval,
                payload_sha256=__import__("hashlib").sha256(
                    json.dumps(
                        json.loads(changed.payload_json),
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
            ),
            now=_NOW,
        )


def test_authorized_payload_binding_reports_each_failure_class() -> None:
    request = _request()
    approval = {
        "approval_id": "approval-1",
        "approval_principal": r"CONTOSO\reviewer",
        "approval_policy_version": request.approval_policy_version,
        "evidence_snapshot_id": request.evidence_snapshot_id,
    }

    def failure(payload: str) -> str | None:
        return _authorized_payload_failure(
            request=request,
            payload_json=payload,
            **approval,
        )

    assert "valid JSON" in (failure("not-json") or "")
    document = json.loads(request.payload_json)
    document["authorized_effect"] = []
    assert "authorized-effect binding" in (failure(json.dumps(document)) or "")
    document = json.loads(request.payload_json)
    document["authorized_effect"]["execution_principal"] = "wrong"
    assert "execution_principal" in (failure(json.dumps(document)) or "")
    document = json.loads(request.payload_json)
    document["authorized_effect"]["desired_value"] = []
    assert "desired value is invalid" in (failure(json.dumps(document)) or "")
    document = json.loads(request.payload_json)
    document["authorized_effect"]["desired_value"] = {"text": "changed"}
    assert "does not match" in (failure(json.dumps(document)) or "")
    document = json.loads(request.payload_json)
    document["authorized_effect"]["desired_value"] = {"score": float("nan")}
    assert "canonical JSON" in (failure(json.dumps(document)) or "")


def test_marker_reservation_reenroll_and_collision_are_atomic(
    tmp_path: Path,
) -> None:
    root = tmp_path / "programs"
    request, approval, entry = _approve_and_enqueue(root)
    db_path = outbox_db_path(_PROGRAM, programs_root=root)
    marker = approved_remote_marker(
        operation=request.operation,
        semantic_effect_key=approval.semantic_effect_key,
    )
    assert marker is not None
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE outbox_marker_reservations SET outbox_id = 'other' "
            "WHERE marker_value = ?",
            (marker,),
        )
    with pytest.raises(OutboxError, match="marker reservation"):
        enqueue_approved_actuation_intent(
            root,
            request=request,
            approval=approval,
            now=_NOW,
        )

    other_root = tmp_path / "other-programs"
    other_request = _request()
    other_approval = create_actuation_approval(
        other_root,
        request=other_request,
        approver=_identity(),
        approved_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
        approval_id="approval-1",
    )
    other_db = outbox_db_path(_PROGRAM, programs_root=other_root)
    other_marker = approved_remote_marker(
        operation=other_request.operation,
        semantic_effect_key=other_approval.semantic_effect_key,
    )
    assert other_marker is not None
    with sqlite3.connect(other_db) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO outbox_marker_reservations "
            "(marker_value, outbox_id, semantic_effect_key, reserved_at) "
            "VALUES (?, 'other', ?, ?)",
            (
                other_marker,
                other_approval.semantic_effect_key,
                _NOW.isoformat(),
            ),
        )
    with pytest.raises(OutboxError, match="reserved by a different"):
        enqueue_approved_actuation_intent(
            other_root,
            request=other_request,
            approval=other_approval,
            now=_NOW,
        )


def test_receipt_binder_refuses_partial_or_changed_child_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "programs"
    key, _, payload = _enqueue_create(root)
    dispatch_leased_create_task(
        program_id=_PROGRAM,
        idempotency_key=key,
        owner="worker",
        dispatch_fn=lambda entry: _success(entry, payload),
        programs_root=root,
    )
    db_path = outbox_db_path(_PROGRAM, programs_root=root)
    receipt = json.loads(load_success_receipt(db_path, key) or "{}")
    authorized = AuthorizedEffect.from_dict(payload["authorized_effect"])  # type: ignore[arg-type]
    assert _receipt_reconciliation_binds(
        receipt=receipt,
        authorized=authorized,
        remote_id=42,
        remote_rev=2,
    )
    assert not _receipt_reconciliation_binds(
        receipt={**receipt, "reconciliation": []},
        authorized=authorized,
        remote_id=42,
        remote_rev=2,
    )
    partial = json.loads(json.dumps(receipt))
    partial["reconciliation"]["status"] = "partial"
    assert not _receipt_reconciliation_binds(
        receipt=partial,
        authorized=authorized,
        remote_id=42,
        remote_rev=2,
    )
    changed = json.loads(json.dumps(receipt))
    changed["reconciliation"]["receipts"][0]["response_hash"] = ""
    assert not _receipt_reconciliation_binds(
        receipt=changed,
        authorized=authorized,
        remote_id=42,
        remote_rev=2,
    )


def test_effect_binding_first_owner_and_changed_receipt_are_fail_closed(
    tmp_path: Path,
) -> None:
    from src.core.ledger.durable_outbox_store import bind_effect, enqueue

    db_path = tmp_path / "outbox.db"
    first = enqueue(
        db_path,
        "first",
        domain="synthetic",
        correlation_id="first",
        payload_json="{}",
    )
    second = enqueue(
        db_path,
        "second",
        domain="synthetic",
        correlation_id="second",
        payload_json="{}",
    )
    assert bind_effect(db_path, "effect", first.outbox_id) == first.outbox_id
    with pytest.raises(OutboxError, match="different outbox"):
        _bind_effect_or_raise(db_path, "effect", second.outbox_id)

    root = tmp_path / "programs"
    key, _, payload = _enqueue_create(root)
    dispatch_leased_create_task(
        program_id=_PROGRAM,
        idempotency_key=key,
        owner="worker",
        dispatch_fn=lambda entry: _success(entry, payload),
        programs_root=root,
    )
    completed_db = outbox_db_path(_PROGRAM, programs_root=root)
    completed = load_entry(completed_db, key)
    assert completed is not None
    receipt_json = load_success_receipt(completed_db, key)
    assert receipt_json is not None
    receipt = json.loads(receipt_json)
    with pytest.raises(OutboxError, match="success evidence changed"):
        _resume_success(
            completed_db,
            completed,
            receipt=receipt,
            receipt_json=receipt_json + " ",
        )


def test_resume_refuses_missing_owner_and_noncompensable_retained_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "programs"
    key, _, payload = _enqueue_create(root)
    import src.core.actuation_outbox as service

    original = service.mark_audited
    monkeypatch.setattr(
        service,
        "mark_audited",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("stop")),
    )
    with pytest.raises(RuntimeError, match="stop"):
        dispatch_leased_create_task(
            program_id=_PROGRAM,
            idempotency_key=key,
            owner="worker",
            dispatch_fn=lambda entry: _success(entry, payload),
            programs_root=root,
        )
    monkeypatch.setattr(service, "mark_audited", original)
    db_path = outbox_db_path(_PROGRAM, programs_root=root)
    observed = load_entry(db_path, key)
    receipt_json = load_success_receipt(db_path, key)
    assert observed is not None and receipt_json is not None
    receipt = json.loads(receipt_json)

    receipt["compensation_required"] = True
    compensation_json = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE outbox_success_receipts SET receipt_json = ? WHERE outbox_id = ?",
            (compensation_json, key),
        )
    with pytest.raises(OutboxError, match="approved compensation operation"):
        _resume_success(
            db_path,
            observed,
            receipt=receipt,
            receipt_json=compensation_json,
        )

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE outbox SET lease_owner = NULL WHERE outbox_id = ?",
            (key,),
        )
    ownerless = load_entry(db_path, key)
    assert ownerless is not None
    with pytest.raises(OutboxError, match="lease owner"):
        _resume_success(
            db_path,
            ownerless,
            receipt=receipt,
            receipt_json=compensation_json,
        )


def test_reconciliation_refuses_missing_binding_and_naive_effect_times(
    tmp_path: Path,
) -> None:
    root = tmp_path / "programs"
    key, _, payload = _enqueue_create(root)
    db_path = outbox_db_path(_PROGRAM, programs_root=root)
    dispatch_leased_create_task(
        program_id=_PROGRAM,
        idempotency_key=key,
        owner="worker",
        dispatch_fn=lambda _entry: DispatchResult(
            succeeded=False,
            uncertain=True,
            failure_reason="lost",
        ),
        programs_root=root,
    )
    retained = load_entry(db_path, key)
    assert retained is not None
    damaged = json.loads(retained.payload_json)
    damaged.pop("authorized_effect")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE outbox SET payload_json = ? WHERE outbox_id = ?",
            (json.dumps(damaged), key),
        )
    damaged_entry = load_entry(db_path, key)
    assert damaged_entry is not None
    with pytest.raises(OutboxError, match="authorized-effect evidence"):
        _authorized_effect_for_entry(damaged_entry)

    authorized = AuthorizedEffect.from_dict(payload["authorized_effect"])  # type: ignore[arg-type]
    completed = synthetic_completed_reconciliation(
        payload["authorized_effect"], remote_id=42  # type: ignore[arg-type]
    )
    naive = replace(
        completed,
        receipts=(
            replace(
                completed.receipts[0],
                dispatched_at=datetime(2026, 11, 19, 18, 0),
            ),
        ),
    )
    assert not _reconciliation_binds_authorized_effect(
        authorized,
        naive,
        remote_id=42,
    )
    assert not _reconciliation_binds_authorized_effect(
        authorized,
        EffectReconciliation(status="completed", receipts=(), mismatches=()),
        remote_id=42,
    )


def test_reconciliation_refuses_disjoint_revision_and_invalid_provider_lookup(
    tmp_path: Path,
) -> None:
    document = synthetic_authorized_effect_document()
    authorized_document = json.loads(json.dumps(document))
    authorized_document["intended_recipients"] = ["alice", "bob"]
    authorized_document["audience_manifest"]["entries"] = [
        {
            "person_entity_id": recipient,
            "scope_id": "leaders",
            "disposition": "included",
            "reason": "synthetic",
        }
        for recipient in ("alice", "bob")
    ]
    authorized = AuthorizedEffect.from_dict(authorized_document)
    one = synthetic_completed_reconciliation(
        synthetic_authorized_effect_document(),
        remote_id=42,
        remote_rev=2,
    ).receipts[0]
    split_revisions = EffectReconciliation(
        status="completed",
        receipts=(
            replace(one, recipient="alice"),
            replace(one, recipient="bob", resulting_revision=3),
        ),
        mismatches=(),
    )
    assert not _reconciliation_binds_authorized_effect(
        authorized,
        split_revisions,
        remote_id=42,
    )

    root = tmp_path / "programs"
    key, _, _ = _enqueue_create(root)
    dispatch_leased_create_task(
        program_id=_PROGRAM,
        idempotency_key=key,
        owner="worker",
        dispatch_fn=lambda _entry: DispatchResult(
            succeeded=False,
            uncertain=True,
            failure_reason="lost",
        ),
        programs_root=root,
    )
    outcome = reconcile_uncertain_effect(
        outbox_db_path(_PROGRAM, programs_root=root),
        key,
        reconciled_by="operator",
        provider_lookup=lambda _entry: "invalid",
    )
    assert outcome.status == "uncertain_remote_state"


def test_compensation_policy_refuses_missing_named_or_registered_inverse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.core.actuation_reconciliation as reconciliation_service
    from src.core.ledger.durable_outbox_store import (
        enqueue,
        lease_next,
        mark_dispatched,
        mark_succeeded,
    )

    db_path = tmp_path / "outbox.db"
    entry = enqueue(
        db_path,
        "tag",
        domain="synthetic",
        correlation_id="tag",
        payload_json=json.dumps({"operation": "add_tag.v1"}),
    )
    leased = lease_next(db_path, "synthetic", owner="worker")
    assert leased is not None
    mark_dispatched(
        db_path,
        entry.outbox_id,
        owner="worker",
        fencing_token=leased.lease_fencing_token,
    )
    mark_succeeded(
        db_path,
        entry.outbox_id,
        owner="worker",
        fencing_token=leased.lease_fencing_token,
    )
    capability = OperationCapability(
        operation="add_tag.v1",
        effect_class=EffectClass.MUTATE,
        supports_remote_marker=True,
        reconcilable=True,
        compensable=True,
        max_blast_radius=1,
        compensation_operation=None,
    )
    monkeypatch.setattr(
        reconciliation_service,
        "lookup_capability",
        lambda operation: capability if operation == "add_tag.v1" else CapabilityRefusal(operation),
    )
    with pytest.raises(OutboxError, match="named compensation"):
        request_compensation(
            db_path,
            entry.outbox_id,
            owner="worker",
            fencing_token=leased.lease_fencing_token,
            reason="policy gap",
        )
    named = replace(capability, compensation_operation="remove_tag.v1")
    monkeypatch.setattr(
        reconciliation_service,
        "lookup_capability",
        lambda operation: named
        if operation == "add_tag.v1"
        else CapabilityRefusal(operation=operation),
    )
    with pytest.raises(OutboxError, match="offline compensation"):
        request_compensation(
            db_path,
            entry.outbox_id,
            owner="worker",
            fencing_token=leased.lease_fencing_token,
            reason="policy gap",
        )


def test_operation_marker_and_tag_normalization_edge_contracts() -> None:
    with pytest.raises(ValueError, match="semantic effect key"):
        approved_remote_marker(
            operation="create_task.v1",
            semantic_effect_key="invalid",
        )
    normalized = normalize_semantic_payload(
        "create_task.v1",
        {
            "tags": "B; vertex-intent-123; A",
        },
    )
    assert normalized["tags"] == ("a", "b")


def test_writer_rejects_unbound_and_invalid_approved_markers() -> None:
    from src.m365.ado_writer import ADOWriter

    writer = ADOWriter(
        SimpleNamespace(
            organization="fixture",
            project="One",
            _rest_base_url="https://example.invalid/",
            _vertex_synthetic_provider=True,
        ),
        programs_root=Path("synthetic"),
    )
    payload = {
        "org": "fixture",
        "project": "One",
        "operation": "create_task.v1",
        "operation_intent_id": "intent-1",
        "approved_remote_marker": "vertex-effect-x",
        "tags": "vertex-intent-intent-1",
    }
    result = writer._dispatch_create_task_outbox_entry(
        SimpleNamespace(payload_json=json.dumps(payload), attempt_count=1)
    )
    assert result.succeeded is False
    assert "approved marker" in (result.failure_reason or "")

    malformed = dict(payload, approved_remote_marker="not-a-marker")
    result = writer._dispatch_create_task_outbox_entry(
        SimpleNamespace(payload_json=json.dumps(malformed), attempt_count=1)
    )
    assert result.succeeded is False


def test_writer_rejects_canonical_target_mismatch() -> None:
    fields = {
        "program_id": _PROGRAM,
        "operation_intent_id": "intent-1",
        "org": "contoso",
        "project": "One",
        "title": "Synthetic",
        "tags": "vertex-intent-intent-1; vertex-effect-qualified",
        "causal_occurrence_id": "meeting:action-1",
        "approved_remote_marker": "vertex-effect-qualified",
        "authorized_effect": synthetic_authorized_effect_document(
            program_id=_PROGRAM,
            intent_id="intent-1",
            tenant_id="contoso",
            project="One",
        ),
    }
    fields["authorized_effect"]["desired_value"] = {
        item["path"].removeprefix("/fields/"): item["value"]
        for item in _build_create_task_patch(fields)
    }
    with pytest.raises(ValueError, match="target"):
        _authorized_create_effect(fields)


def test_enrollment_refuses_changed_principal_and_binding_fault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "programs"
    request = _request()
    approval = create_actuation_approval(
        root,
        request=request,
        approver=_identity(),
        approved_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
        approval_id="approval-1",
    )
    invalid = replace(
        approval,
        approver=OperatorIdentity(
            actor="reviewer@example.com",
            principal="",
            machine="reviewer-host",
            session="session-1",
        ),
    )
    with pytest.raises(OutboxError, match="durable approval principal"):
        enqueue_approved_actuation_intent(
            root,
            request=request,
            approval=invalid,
            now=_NOW,
        )

    import src.core.actuation_approval_store as approval_store

    monkeypatch.setattr(
        approval_store,
        "_authorized_payload_failure",
        lambda **_kwargs: "forced binding failure",
    )
    with pytest.raises(OutboxError, match="forced binding failure"):
        enqueue_approved_actuation_intent(
            root,
            request=request,
            approval=approval,
            now=_NOW,
        )


@pytest.mark.parametrize(
    ("missing_field", "message"),
    [("desired_effect", "desired effect"), ("program_id", "program identity")],
)
def test_dispatch_refuses_retained_binding_document_gaps(
    tmp_path: Path,
    missing_field: str,
    message: str,
) -> None:
    root = tmp_path / "programs"
    request, _, entry = _approve_and_enqueue(root)
    db_path = outbox_db_path(_PROGRAM, programs_root=root)
    payload = json.loads(entry.payload_json)
    payload.pop(missing_field)
    payload_json = json.dumps(payload, sort_keys=True)
    import hashlib

    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE outbox SET payload_json = ? WHERE outbox_id = ?",
            (payload_json, entry.outbox_id),
        )
        connection.execute(
            "UPDATE outbox_admissions SET payload_sha256 = ? WHERE outbox_id = ?",
            (payload_hash, entry.outbox_id),
        )
        connection.execute(
            "UPDATE outbox_approvals SET payload_sha256 = ? WHERE approval_id = ?",
            (payload_hash, "approval-1"),
        )
    outcome = dispatch_approved_actuation_intent(
        program_id=_PROGRAM,
        outbox_id=entry.outbox_id,
        owner="worker",
        dispatch_fn=lambda _retained: DispatchResult(succeeded=True, remote_id=42),
        preflight_fn=lambda _retained: _preflight(request),
        programs_root=root,
        approval_policy_version=request.approval_policy_version,
        scope_enabled=True,
        rule_enabled=True,
        now=_NOW,
    )
    assert outcome.status == "failed_terminal"
    assert message in (outcome.entry.failure_reason or "").lower()


def test_dispatch_binding_failure_blocks_callback_before_marker_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "programs"
    request, _, entry = _approve_and_enqueue(root)
    import src.core.actuation_approval_store as approval_store

    monkeypatch.setattr(
        approval_store,
        "_authorized_payload_failure",
        lambda **_kwargs: "forced retained binding failure",
    )
    calls = 0

    def callback(_entry) -> DispatchResult:
        nonlocal calls
        calls += 1
        return DispatchResult(succeeded=True, remote_id=42)

    outcome = dispatch_approved_actuation_intent(
        program_id=_PROGRAM,
        outbox_id=entry.outbox_id,
        owner="worker",
        dispatch_fn=callback,
        preflight_fn=lambda _retained: _preflight(request),
        programs_root=root,
        approval_policy_version=request.approval_policy_version,
        scope_enabled=True,
        rule_enabled=True,
        now=_NOW,
    )
    assert outcome.status == "failed_terminal"
    assert "binding failure" in (outcome.entry.failure_reason or "").lower()
    assert calls == 0


def test_success_receipt_restart_validation_and_resume_compensation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "programs"
    key, _, payload = _enqueue_create(root)
    dispatch_leased_create_task(
        program_id=_PROGRAM,
        idempotency_key=key,
        owner="worker",
        dispatch_fn=lambda entry: _success(entry, payload),
        programs_root=root,
    )
    db_path = outbox_db_path(_PROGRAM, programs_root=root)
    observed = load_entry(db_path, key)
    assert observed is not None
    receipt_json = load_success_receipt(db_path, key)
    assert receipt_json is not None
    receipt = json.loads(receipt_json)
    invalid = json.loads(receipt_json)
    invalid["reconciliation"]["receipts"] = ["invalid"]
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE outbox_success_receipts SET receipt_json = ? WHERE outbox_id = ?",
            (json.dumps(invalid, sort_keys=True), key),
        )
    with pytest.raises(OutboxError, match="authorized effect"):
        _load_valid_success_receipt(
            db_path,
            observed,
            program_id=_PROGRAM,
        )

    comp_db = tmp_path / "comp.db"
    from src.core.ledger.durable_outbox_store import (
        enqueue,
        lease_next,
        mark_dispatched,
        mark_succeeded,
    )

    comp_entry = enqueue(
        comp_db,
        "comment",
        domain="synthetic",
        correlation_id="comment",
        payload_json=json.dumps({"operation": "add_comment.v1"}),
    )
    leased = lease_next(comp_db, "synthetic", owner="worker")
    assert leased is not None
    mark_dispatched(
        comp_db,
        comp_entry.outbox_id,
        owner="worker",
        fencing_token=leased.lease_fencing_token,
    )
    succeeded = mark_succeeded(
        comp_db,
        comp_entry.outbox_id,
        owner="worker",
        fencing_token=leased.lease_fencing_token,
    )
    comp_receipt = {
        "remote_id": 42,
        "remote_rev": 2,
        "compensation_required": True,
    }
    comp_json = json.dumps(comp_receipt, sort_keys=True)
    with sqlite3.connect(comp_db) as connection:
        connection.execute(_SUCCESS_RECEIPTS_SQL)
        connection.execute(
            "INSERT INTO outbox_success_receipts (outbox_id, receipt_json) "
            "VALUES (?, ?)",
            (comp_entry.outbox_id, comp_json),
        )
    resumed = _resume_success(
        comp_db,
        succeeded,
        receipt=comp_receipt,
        receipt_json=comp_json,
    )
    assert resumed.status == "compensation_required"
    assert resumed.entry.failure_reason == "Retained provider effect requires compensation"


def test_receipt_binding_rejects_nonlist_and_nonobject_children(
    tmp_path: Path,
) -> None:
    root = tmp_path / "programs"
    key, _, payload = _enqueue_create(root)
    dispatch_leased_create_task(
        program_id=_PROGRAM,
        idempotency_key=key,
        owner="worker",
        dispatch_fn=lambda entry: _success(entry, payload),
        programs_root=root,
    )
    receipt = json.loads(
        load_success_receipt(
            outbox_db_path(_PROGRAM, programs_root=root),
            key,
        )
        or "{}"
    )
    authorized = AuthorizedEffect.from_dict(payload["authorized_effect"])  # type: ignore[arg-type]
    invalid_shape = json.loads(json.dumps(receipt))
    invalid_shape["reconciliation"]["receipts"] = {}
    assert not _receipt_reconciliation_binds(
        receipt=invalid_shape,
        authorized=authorized,
        remote_id=42,
        remote_rev=2,
    )
    invalid_child = json.loads(json.dumps(receipt))
    invalid_child["reconciliation"]["receipts"] = [1]
    assert not _receipt_reconciliation_binds(
        receipt=invalid_child,
        authorized=authorized,
        remote_id=42,
        remote_rev=2,
    )


def test_reconciliation_confirmed_auth_failures_remain_inconclusive(
    tmp_path: Path,
) -> None:
    root = tmp_path / "programs"
    key, _, payload = _enqueue_create(root)
    db_path = outbox_db_path(_PROGRAM, programs_root=root)
    dispatch_leased_create_task(
        program_id=_PROGRAM,
        idempotency_key=key,
        owner="worker",
        dispatch_fn=lambda _entry: DispatchResult(
            succeeded=False,
            uncertain=True,
            failure_reason="lost",
        ),
        programs_root=root,
    )
    broken = json.loads(load_entry(db_path, key).payload_json)  # type: ignore[union-attr]
    broken["authorized_effect"]["program_id"] = "wrong"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE outbox SET payload_json = ? WHERE outbox_id = ?",
            (json.dumps(broken), key),
        )
    completed = synthetic_completed_reconciliation(
        payload["authorized_effect"], remote_id=42  # type: ignore[arg-type]
    )
    outcome = reconcile_uncertain_effect(
        db_path,
        key,
        reconciled_by="operator",
        provider_lookup=lambda _entry: ReconciliationObservation(
            ReconciliationDisposition.CONFIRMED,
            "marker found",
            remote_id=42,
            effect_reconciliation=completed,
        ),
    )
    assert outcome.status == "uncertain_remote_state"

    broken_entry = replace(load_entry(db_path, key), payload_json=json.dumps({  # type: ignore[arg-type]
        "operation": "create_task.v1",
        "authorized_effect": {},
    }))
    with pytest.raises(OutboxError, match="invalid"):
        _authorized_effect_for_entry(broken_entry)


def test_reconciliation_binder_rejects_recipient_and_non_datetime_shape() -> None:
    document = synthetic_authorized_effect_document()
    authorized = AuthorizedEffect.from_dict(document)
    completed = synthetic_completed_reconciliation(document, remote_id=42)
    recipient_changed = replace(
        completed,
        receipts=(replace(completed.receipts[0], recipient="mallory"),),
    )
    assert not _reconciliation_binds_authorized_effect(
        authorized,
        recipient_changed,
        remote_id=42,
    )
    non_datetime = replace(
        completed,
        receipts=(
            replace(
                completed.receipts[0],
                dispatched_at=object(),  # type: ignore[arg-type]
            ),
        ),
    )
    assert not _reconciliation_binds_authorized_effect(
        authorized,
        non_datetime,
        remote_id=42,
    )
