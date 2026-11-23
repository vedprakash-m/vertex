from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.actuation_outbox import (
    ActuationAdmissionRequest,
    ActuationPreflightObservation,
    DispatchResult,
    OutboxError,
    create_actuation_approval,
    dispatch_approved_actuation_intent,
    enqueue_approved_actuation_intent,
    outbox_db_path,
    require_dispatchable_operation,
    revoke_actuation_approval,
)
from src.core.ledger.durable_outbox_store import load_entry
from src.core.operation_capabilities import (
    approved_remote_marker,
    create_target_identity,
    desired_effect_hash,
    semantic_effect_key,
)
from src.core.operator_identity import OperatorIdentity
from tests.support.actuation_reconciliation import (
    synthetic_authorized_effect_document,
    synthetic_completed_reconciliation,
)

_PROGRAM = "fixture"
_NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def _identity(*, principal: str = "CONTOSO\\reviewer") -> OperatorIdentity:
    return OperatorIdentity(
        actor="reviewer@example.com",
        principal=principal,
        machine="reviewer-host",
        session="session-1",
    )


def _request(
    *,
    intent_id: str = "intent-1",
    causal_occurrence_id: str = "meeting:action-1",
    operation: str = "add_comment.v1",
    target_identity: str = "42",
    desired_effect: dict[str, object] | None = None,
) -> ActuationAdmissionRequest:
    desired = desired_effect or {"text": "review this item"}
    resolved_target = target_identity
    if operation == "create_task.v1" and not target_identity.startswith("origin1:"):
        resolved_target = create_target_identity(
            provider="ado",
            tenant="contoso",
            project="One",
            causal_occurrence_id=causal_occurrence_id,
            payload=desired,
        )
    full_payload = {
        "program_id": _PROGRAM,
        "operation": operation,
        "operation_intent_id": intent_id,
        "proposal_id": "proposal-1",
        "org": "contoso",
        "project": "One",
        "work_item_id": int(resolved_target) if resolved_target.isdigit() else None,
        "desired_effect": desired,
    }
    authorized_effect = synthetic_authorized_effect_document(
        program_id=_PROGRAM,
        intent_id=intent_id,
        operation=operation,
        tenant_id="contoso",
        project="One",
    )
    authorized_effect.update({
        "approval_id": f"approval:{intent_id}",
        "target_identity": resolved_target,
        "desired_value": desired,
        "execution_principal": "CONTOSO\\reviewer",
        "evidence_version": "snapshot-17",
        "policy_version": "policy-7",
    })
    full_payload["authorized_effect"] = authorized_effect
    semantic = semantic_effect_key(
        program_id=_PROGRAM,
        provider="ado",
        tenant="contoso",
        project="One",
        operation=operation,
        target_identity=resolved_target,
        causal_occurrence_id=causal_occurrence_id,
        payload=desired,
    )
    full_payload["approved_remote_marker"] = approved_remote_marker(
        operation=operation,
        semantic_effect_key=semantic,
    )
    return ActuationAdmissionRequest(
        program_id=_PROGRAM,
        intent_id=intent_id,
        proposal_id="proposal-1",
        provider="ado",
        tenant="contoso",
        project="One",
        operation=operation,
        target_identity=resolved_target,
        causal_occurrence_id=causal_occurrence_id,
        desired_effect=desired,
        payload_json=json.dumps(full_payload, sort_keys=True),
        approval_policy_version="policy-7",
        rate_scope="proposal-1",
        rate_limit=2,
        blast_radius=1,
        scope_enabled=True,
        rule_enabled=True,
        expected_revision=8 if resolved_target.isdigit() else None,
        expected_state="Active" if resolved_target.isdigit() else None,
        evidence_snapshot_id="snapshot-17",
        evidence_expires_at=_NOW + timedelta(minutes=30),
    )


def _approve(root: Path, request: ActuationAdmissionRequest, *, now: datetime = _NOW):
    return create_actuation_approval(
        root,
        request=request,
        approver=_identity(),
        approved_at=now,
        expires_at=now + timedelta(hours=1),
        approval_id=f"approval:{request.intent_id}",
    )


def _enqueue(root: Path, request: ActuationAdmissionRequest, approval=None):
    resolved = approval or _approve(root, request)
    return enqueue_approved_actuation_intent(root, request=request, approval=resolved, now=_NOW), resolved


def _preflight(*, request: ActuationAdmissionRequest) -> ActuationPreflightObservation:
    return ActuationPreflightObservation(
        evidence_snapshot_id=request.evidence_snapshot_id,
        target_count=0 if request.operation == "create_task.v1" else 1,
        current_revision=request.expected_revision,
        current_state=request.expected_state,
        marker_found=False,
        observed_at=_NOW,
    )


def _success(entry, remote_id: int = 42, *, remote_rev: int = 2) -> DispatchResult:
    authorized = json.loads(entry.payload_json)["authorized_effect"]
    return DispatchResult(
        succeeded=True,
        remote_id=remote_id,
        remote_rev=remote_rev,
        reconciliation=synthetic_completed_reconciliation(
            authorized,
            remote_id=remote_id,
            remote_rev=remote_rev,
        ),
    )


def test_qualified_operation_matrix_preserves_unqualified_manual_semantics() -> None:
    require_dispatchable_operation("create_task.v1")
    for operation in (
        "add_comment.v1",
        "add_tag.v1",
        "set_field.v1",
        "remove_tag.v1",
        "set_tags.v1",
        "teams_message.v1",
        "unknown.v1",
    ):
        with pytest.raises(OutboxError, match="draft/manual"):
            require_dispatchable_operation(operation)


def test_semantic_effect_identity_excludes_approval_but_binds_causal_scope() -> None:
    base = {
        "program_id": "p1",
        "provider": "ado",
        "tenant": "contoso",
        "project": "One",
        "operation": "set_field.v1",
        "target_identity": "42",
        "causal_occurrence_id": "meeting:action-1",
        "payload": {"b": 2, "a": 1},
    }
    first = semantic_effect_key(**base)
    assert first == semantic_effect_key(**dict(base, payload={"a": 1, "b": 2}))
    assert first != semantic_effect_key(**dict(base, causal_occurrence_id="meeting:action-2"))
    assert first != semantic_effect_key(**dict(base, payload={"a": 2, "b": 2}))
    assert desired_effect_hash({"b": 2, "a": 1}) == desired_effect_hash({"a": 1, "b": 2})


def test_policy_revision_change_cannot_create_a_second_effect(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    first_request = _request()
    first, _ = _enqueue(root, first_request)
    second_payload = json.loads(first_request.payload_json)
    second_payload["operation_intent_id"] = "intent-2"
    second_payload["proposal_id"] = "proposal-2"
    second_payload["authorized_effect"]["intent_id"] = "intent-2"
    second_payload["authorized_effect"]["approval_id"] = "approval:intent-2"
    second_payload["authorized_effect"]["policy_version"] = "policy-8"
    second_request = replace(
        first_request,
        intent_id="intent-2",
        proposal_id="proposal-2",
        rate_scope="proposal-2",
        approval_policy_version="policy-8",
        payload_json=json.dumps(second_payload, sort_keys=True),
    )
    second_approval = _approve(root, second_request)
    duplicate = enqueue_approved_actuation_intent(
        root, request=second_request, approval=second_approval, now=_NOW,
    )
    assert duplicate.outbox_id == first.outbox_id


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("program_id", ""), ("provider", ""), ("tenant", ""), ("project", ""),
        ("intent_id", ""), ("proposal_id", ""), ("causal_occurrence_id", ""),
        ("approval_policy_version", ""), ("rate_scope", ""),
        ("evidence_snapshot_id", ""), ("evidence_snapshot_id", "unknown"),
        ("rate_limit", True), ("rate_limit", 0), ("blast_radius", True),
        ("blast_radius", 0), ("blast_radius", 2), ("operation", "unknown.v1"),
        ("expected_revision", True), ("expected_revision", 0), ("expected_revision", None),
        ("scope_enabled", 1), ("scope_enabled", False),
        ("rule_enabled", 1), ("rule_enabled", False),
        ("payload_json", "not-json"), ("payload_json", "[]"),
    ],
)
def test_invalid_admission_inputs_fail_before_approval_persists(
    tmp_path: Path, field: str, value: object,
) -> None:
    request = replace(_request(), **{field: value})
    with pytest.raises(OutboxError):
        _approve(tmp_path / "programs", request)


def test_invalid_create_target_and_nonserializable_effect_refuse(tmp_path: Path) -> None:
    request = replace(
        _request(), operation="create_task.v1", target_identity="new",
        expected_revision=None, expected_state=None,
        payload_json=json.dumps({
            "operation": "create_task.v1", "operation_intent_id": "intent-1",
            "proposal_id": "proposal-1", "org": "contoso", "project": "One",
            "desired_effect": {"text": "review this item"},
        }),
    )
    with pytest.raises(OutboxError, match="stable origin"):
        _approve(tmp_path / "programs", request)
    with pytest.raises(OutboxError):
        _approve(
            tmp_path / "programs",
            replace(_request(), desired_effect={"value": object()}),
        )


def test_approval_requires_operator_identity_object(tmp_path: Path) -> None:
    with pytest.raises(OutboxError, match="identity"):
        create_actuation_approval(
            tmp_path / "programs",
            request=_request(),
            approver=object(),
            approved_at=_NOW,
            expires_at=_NOW + timedelta(hours=1),
        )


@pytest.mark.parametrize("changed_time", ["naive", "equal", "before_evidence"])
def test_approval_timing_is_strict(tmp_path: Path, changed_time: str) -> None:
    request = _request()
    approved_at = _NOW
    expires_at = _NOW + timedelta(hours=1)
    if changed_time == "naive":
        approved_at = approved_at.replace(tzinfo=None)
    elif changed_time == "equal":
        expires_at = approved_at
    else:
        request = replace(request, evidence_expires_at=approved_at)
    with pytest.raises(OutboxError):
        create_actuation_approval(
            tmp_path / "programs",
            request=request,
            approver=_identity(),
            approved_at=approved_at,
            expires_at=expires_at,
        )


def test_corrupt_stored_approval_time_refuses_at_enqueue(tmp_path: Path) -> None:
    from src.core._db import open_program_db_with_retry

    root = tmp_path / "programs"
    request = _request()
    approval = _approve(root, request)
    db = outbox_db_path(_PROGRAM, programs_root=root)
    with open_program_db_with_retry(db, durability="strict") as connection:
        connection.execute(
            "UPDATE outbox_approvals SET approved_at = 'invalid' WHERE approval_id = ?",
            (approval.approval_id,),
        )
    with pytest.raises(OutboxError, match="stored time"):
        enqueue_approved_actuation_intent(root, request=request, approval=approval, now=_NOW)


def test_distinct_causal_occurrences_remain_distinct(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    first, _ = _enqueue(root, _request())
    second_request = _request(
        intent_id="intent-2", causal_occurrence_id="meeting:action-2", target_identity="43",
    )
    second_approval = _approve(root, second_request)
    second = enqueue_approved_actuation_intent(
        root, request=second_request, approval=second_approval, now=_NOW,
    )
    assert second.outbox_id != first.outbox_id


def test_conflicting_active_intent_refuses_at_enrollment(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    _enqueue(root, _request())
    conflicting = _request(intent_id="intent-2", causal_occurrence_id="meeting:action-2")
    approval = _approve(root, conflicting)
    with pytest.raises(OutboxError, match="conflicting"):
        enqueue_approved_actuation_intent(root, request=conflicting, approval=approval, now=_NOW)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("principal", None),
        ("principal", ""),
        ("principal", "unknown"),
        ("machine", None),
        ("session", None),
    ],
)
def test_approval_requires_real_snapshot_and_operator_identity(
    tmp_path: Path, field: str, value: str | None,
) -> None:
    request = _request()
    values = _identity().as_dict()
    values[field] = value
    identity = OperatorIdentity(
        actor=str(values["actor"]), principal=values["principal"],
        machine=values["machine"], session=values["session"],
    )
    with pytest.raises(OutboxError, match="identity"):
        create_actuation_approval(
            tmp_path / "programs",
            request=request,
            approver=identity,
            approved_at=_NOW,
            expires_at=_NOW + timedelta(hours=1),
        )
    blank_snapshot = replace(request, evidence_snapshot_id="")
    with pytest.raises(OutboxError, match="snapshot"):
        create_actuation_approval(
            tmp_path / "programs",
            request=blank_snapshot,
            approver=_identity(),
            approved_at=_NOW,
            expires_at=_NOW + timedelta(hours=1),
        )


def test_payload_edit_after_approval_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    request = _request()
    approval = _approve(root, request)
    changed = replace(
        request, payload_json=request.payload_json.replace("review this item", "send this item"),
    )
    with pytest.raises(OutboxError, match="payload"):
        enqueue_approved_actuation_intent(root, request=changed, approval=approval, now=_NOW)
    assert load_entry(
        outbox_db_path(_PROGRAM, programs_root=root),
        "fixture:contoso/One:add_comment.v1:42:intent-1",
    ) is None


def test_approved_payload_refuses_rebound_retained_semantic_effect(
    tmp_path: Path,
) -> None:
    root = tmp_path / "programs"
    request = _request()
    payload = json.loads(request.payload_json)
    payload["semantic_effect_id"] = "sek1:rebound"
    request = replace(
        request,
        payload_json=json.dumps(payload, sort_keys=True),
    )
    approval = _approve(root, request)

    with pytest.raises(OutboxError, match="semantic effect does not match"):
        enqueue_approved_actuation_intent(
            root,
            request=request,
            approval=approval,
            now=_NOW,
        )


def test_payload_identity_fields_must_match_the_request(tmp_path: Path) -> None:
    request = replace(
        _request(),
        payload_json=_request().payload_json.replace('"operation": "add_comment.v1"', '"operation": "set_field.v1"'),
    )
    with pytest.raises(OutboxError, match="payload identity"):
        _approve(tmp_path / "programs", request)


@pytest.mark.parametrize("field", ["intent_id", "semantic_effect_key", "payload_sha256", "revision"])
def test_tampered_approval_object_is_not_an_approval(
    tmp_path: Path, field: str,
) -> None:
    request = _request()
    approval = _approve(tmp_path / "programs", request)
    value = {
        "intent_id": "other",
        "semantic_effect_key": "sek1:other",
        "payload_sha256": "0" * 64,
        "revision": True,
    }[field]
    with pytest.raises(OutboxError):
        enqueue_approved_actuation_intent(
            tmp_path / "programs",
            request=request,
            approval=replace(approval, **{field: value}),
            now=_NOW,
        )


@pytest.mark.parametrize("expired_at", ["approval", "evidence"])
def test_enqueue_refuses_expired_approval_evidence(tmp_path: Path, expired_at: str) -> None:
    root = tmp_path / "programs"
    request = _request()
    approval = _approve(root, request)
    now = approval.expires_at if expired_at == "approval" else approval.evidence_expires_at
    with pytest.raises(OutboxError, match="expired"):
        enqueue_approved_actuation_intent(root, request=request, approval=approval, now=now)


def test_semantic_effect_bound_to_missing_row_refuses_reenrollment(tmp_path: Path) -> None:
    from src.core._db import open_program_db_with_retry

    root = tmp_path / "programs"
    request = _request()
    entry, approval = _enqueue(root, request)
    db = outbox_db_path(_PROGRAM, programs_root=root)
    with open_program_db_with_retry(db, durability="strict") as connection:
        connection.execute("DELETE FROM outbox WHERE outbox_id = ?", (entry.outbox_id,))
    with pytest.raises(OutboxError, match="missing outbox"):
        enqueue_approved_actuation_intent(root, request=request, approval=approval, now=_NOW)


def test_existing_provider_request_reuse_requires_exact_approval_admission(tmp_path: Path) -> None:
    from src.core._db import open_program_db_with_retry

    root = tmp_path / "programs"
    request = _request()
    entry, approval = _enqueue(root, request)
    db = outbox_db_path(_PROGRAM, programs_root=root)
    with open_program_db_with_retry(db, durability="strict") as connection:
        connection.execute("DELETE FROM outbox_effects WHERE effect_id = ?", (approval.semantic_effect_key,))
    assert enqueue_approved_actuation_intent(
        root, request=request, approval=approval, now=_NOW,
    ).outbox_id == entry.outbox_id

    second = create_actuation_approval(
        root,
        request=request,
        approver=_identity(principal="CONTOSO\\second-reviewer"),
        approved_at=_NOW + timedelta(seconds=1),
        expires_at=_NOW + timedelta(hours=1),
        approval_id="approval:intent-1:r2",
    )
    with pytest.raises(OutboxError, match="different approval"):
        enqueue_approved_actuation_intent(
            root, request=request, approval=second, now=_NOW + timedelta(seconds=2),
        )


def test_reenrollment_requires_the_current_persisted_approval(tmp_path: Path) -> None:
    from src.core._db import open_program_db_with_retry

    root = tmp_path / "programs"
    request = _request()
    _, first = _enqueue(root, request)
    create_actuation_approval(
        root,
        request=request,
        approver=_identity(principal="CONTOSO\\second-reviewer"),
        approved_at=_NOW + timedelta(seconds=1),
        expires_at=_NOW + timedelta(hours=1),
        approval_id="approval:intent-1:r2",
    )
    db = outbox_db_path(_PROGRAM, programs_root=root)
    with open_program_db_with_retry(db, durability="strict") as connection:
        connection.execute("DELETE FROM outbox_effects")
    with pytest.raises(OutboxError, match="current revision"):
        enqueue_approved_actuation_intent(
            root, request=request, approval=first, now=_NOW + timedelta(seconds=2),
        )


def test_approval_revision_defaults_and_expiry_are_persisted(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    request = _request()
    approval = create_actuation_approval(
        root,
        request=request,
        approver=_identity(),
        approved_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
    )
    assert approval.approval_id.startswith("approval-")
    assert approval.revision == 1


def test_newer_approval_revision_is_required_at_claim_and_dispatch(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    request = _request()
    first = _approve(root, request)
    entry = enqueue_approved_actuation_intent(root, request=request, approval=first, now=_NOW)
    second = create_actuation_approval(
        root,
        request=request,
        approver=_identity(principal="CONTOSO\\second-reviewer"),
        approved_at=_NOW + timedelta(seconds=1),
        expires_at=_NOW + timedelta(hours=1),
        approval_id="approval:intent-1:r2",
    )
    calls: list[str] = []
    outcome = dispatch_approved_actuation_intent(
        program_id=_PROGRAM,
        outbox_id=entry.outbox_id,
        owner="worker",
        programs_root=root,
        preflight_fn=lambda retained: _preflight(request=request),
        dispatch_fn=lambda retained: calls.append(retained.outbox_id) or DispatchResult(succeeded=True, remote_id=42),
        approval_policy_version="policy-7",
        scope_enabled=True,
        rule_enabled=True,
        now=_NOW + timedelta(seconds=2),
    )
    assert second.revision == first.revision + 1
    assert outcome.entry.status == "cancelled"
    assert "revision" in outcome.entry.failure_reason.lower()
    assert calls == []


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("scope", "scope"),
        ("rule", "rule"),
        ("revision", "revision"),
        ("state", "state"),
        ("target", "target"),
        ("marker", "marker"),
        ("snapshot", "snapshot"),
    ],
)
def test_preflight_failures_never_reach_synthetic_provider(
    tmp_path: Path, change: str, message: str,
) -> None:
    root = tmp_path / "programs"
    request = _request()
    entry, _ = _enqueue(root, request)
    observation = _preflight(request=request)
    scope = request.scope_enabled
    rule = request.rule_enabled
    if change == "scope":
        scope = False
    elif change == "rule":
        rule = False
    elif change == "revision":
        observation = replace(observation, current_revision=9)
    elif change == "state":
        observation = replace(observation, current_state="Closed")
    elif change == "target":
        observation = replace(observation, target_count=0)
    elif change == "marker":
        observation = replace(observation, marker_found=True)
    elif change == "snapshot":
        observation = replace(observation, evidence_snapshot_id="snapshot-18")
    calls: list[str] = []
    outcome = dispatch_approved_actuation_intent(
        program_id=_PROGRAM,
        outbox_id=entry.outbox_id,
        owner="worker",
        programs_root=root,
        preflight_fn=lambda retained: observation,
        dispatch_fn=lambda retained: calls.append(retained.outbox_id) or DispatchResult(succeeded=True, remote_id=42),
        approval_policy_version=request.approval_policy_version,
        scope_enabled=scope,
        rule_enabled=rule,
        now=_NOW,
    )
    assert calls == []
    assert message in (outcome.entry.failure_reason or "").lower()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("evidence_snapshot_id", ""), ("target_count", -1), ("target_count", True),
        ("marker_found", 1), ("current_revision", True), ("current_revision", 0),
        ("current_state", 1), ("observed_at", _NOW.replace(tzinfo=None)),
        ("observed_at", _NOW + timedelta(seconds=1)),
    ],
)
def test_malformed_or_future_preflight_observation_fails_closed(
    tmp_path: Path, field: str, value: object,
) -> None:
    root = tmp_path / "programs"
    request = _request()
    entry, _ = _enqueue(root, request)
    observation = replace(_preflight(request=request), **{field: value})
    calls: list[str] = []
    outcome = dispatch_approved_actuation_intent(
        program_id=_PROGRAM,
        outbox_id=entry.outbox_id,
        owner="worker",
        programs_root=root,
        preflight_fn=lambda retained: observation,
        dispatch_fn=lambda retained: calls.append(retained.outbox_id) or DispatchResult(
            succeeded=True, remote_id=42,
        ),
        approval_policy_version=request.approval_policy_version,
        scope_enabled=True,
        rule_enabled=True,
        now=_NOW,
    )
    assert calls == []
    assert outcome.entry.status in {"cancelled", "failed_terminal"}


@pytest.mark.parametrize("tamper", ["policy", "revoked"])
def test_persisted_policy_or_revocation_change_blocks_before_lease(
    tmp_path: Path, tamper: str,
) -> None:
    from src.core._db import open_program_db_with_retry

    root = tmp_path / "programs"
    request = _request()
    entry, approval = _enqueue(root, request)
    db = outbox_db_path(_PROGRAM, programs_root=root)
    with open_program_db_with_retry(db, durability="strict") as connection:
        if tamper == "policy":
            connection.execute(
                "UPDATE outbox_approvals SET approval_policy_version = 'policy-8' "
                "WHERE approval_id = ?",
                (approval.approval_id,),
            )
        else:
            connection.execute(
                "UPDATE outbox_approvals SET revoked_at = ? WHERE approval_id = ?",
                (_NOW.isoformat(), approval.approval_id),
            )
    calls: list[str] = []
    outcome = dispatch_approved_actuation_intent(
        program_id=_PROGRAM,
        outbox_id=entry.outbox_id,
        owner="worker",
        programs_root=root,
        preflight_fn=lambda retained: _preflight(request=request),
        dispatch_fn=lambda retained: calls.append(retained.outbox_id) or DispatchResult(
            succeeded=True, remote_id=42,
        ),
        approval_policy_version=request.approval_policy_version,
        scope_enabled=True,
        rule_enabled=True,
        now=_NOW,
    )
    assert outcome.entry.status == "cancelled"
    assert calls == []


def test_expired_evidence_and_approval_cancel_atomically(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    approval_request = _request()
    entry, _ = _enqueue(root, approval_request)
    calls: list[str] = []
    outcome = dispatch_approved_actuation_intent(
        program_id=_PROGRAM,
        outbox_id=entry.outbox_id,
        owner="worker",
        programs_root=root,
        preflight_fn=lambda retained: _preflight(request=approval_request),
        dispatch_fn=lambda retained: calls.append(retained.outbox_id) or DispatchResult(succeeded=True, remote_id=42),
        approval_policy_version=approval_request.approval_policy_version,
        scope_enabled=True,
        rule_enabled=True,
        now=approval_request.evidence_expires_at,
    )
    assert outcome.entry.status == "cancelled"
    assert "evidence" in outcome.entry.failure_reason.lower()
    assert calls == []

    second_request = replace(
        _request(intent_id="intent-2", causal_occurrence_id="meeting:action-2"),
        evidence_expires_at=_NOW + timedelta(hours=2),
    )
    second_entry, _ = _enqueue(root, second_request)
    approval_outcome = dispatch_approved_actuation_intent(
        program_id=_PROGRAM,
        outbox_id=second_entry.outbox_id,
        owner="worker",
        programs_root=root,
        preflight_fn=lambda retained: _preflight(request=second_request),
        dispatch_fn=lambda retained: calls.append(retained.outbox_id) or DispatchResult(succeeded=True, remote_id=42),
        approval_policy_version=second_request.approval_policy_version,
        scope_enabled=True,
        rule_enabled=True,
        now=_NOW + timedelta(hours=1),
    )
    assert approval_outcome.entry.status == "cancelled"
    assert "approval" in approval_outcome.entry.failure_reason.lower()
    assert calls == []


def test_rate_capacity_reserves_atomically_and_does_not_oversubscribe(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    first_request = _request()
    first, _ = _enqueue(root, first_request)
    second_request = _request(
        intent_id="intent-2", causal_occurrence_id="meeting:action-2", target_identity="43",
    )
    _enqueue(root, second_request)
    third_request = _request(
        intent_id="intent-3", causal_occurrence_id="meeting:action-3", target_identity="44",
    )
    third_approval = _approve(root, third_request)
    with pytest.raises(OutboxError, match="rate"):
        enqueue_approved_actuation_intent(
            root, request=third_request, approval=third_approval, now=_NOW,
        )
    assert load_entry(outbox_db_path(_PROGRAM, programs_root=root), first.outbox_id) is not None


def test_admission_rolls_back_effect_and_outbox_when_rate_reservation_fails(tmp_path: Path) -> None:
    import sqlite3
    from src.core._db import open_program_db_with_retry

    root = tmp_path / "programs"
    request = _request()
    approval = _approve(root, request)
    db = outbox_db_path(_PROGRAM, programs_root=root)
    with open_program_db_with_retry(db, durability="strict") as connection:
        connection.execute(
            "CREATE TRIGGER admission_rate_fault BEFORE INSERT ON outbox_rate_reservations "
            "BEGIN SELECT RAISE(ABORT, 'rate fault'); END",
        )
    with pytest.raises(sqlite3.IntegrityError):
        enqueue_approved_actuation_intent(root, request=request, approval=approval, now=_NOW)
    with open_program_db_with_retry(db, read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM outbox_admissions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM outbox_effects").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM outbox_rate_reservations").fetchone()[0] == 0


@pytest.mark.parametrize("reservation", ["rate", "effect"])
def test_missing_current_reservation_never_reaches_provider(
    tmp_path: Path, reservation: str,
) -> None:
    from src.core._db import open_program_db_with_retry

    root = tmp_path / "programs"
    request = _request()
    entry, _ = _enqueue(root, request)
    db = outbox_db_path(_PROGRAM, programs_root=root)
    with open_program_db_with_retry(db, durability="strict") as connection:
        if reservation == "rate":
            connection.execute("DELETE FROM outbox_rate_reservations WHERE outbox_id = ?", (entry.outbox_id,))
        else:
            connection.execute("DELETE FROM outbox_effects WHERE outbox_id = ?", (entry.outbox_id,))
    calls: list[str] = []
    outcome = dispatch_approved_actuation_intent(
        program_id=_PROGRAM,
        outbox_id=entry.outbox_id,
        owner="worker",
        programs_root=root,
        preflight_fn=lambda retained: _preflight(request=request),
        dispatch_fn=lambda retained: calls.append(retained.outbox_id) or DispatchResult(
            succeeded=True, remote_id=42,
        ),
        approval_policy_version=request.approval_policy_version,
        scope_enabled=True,
        rule_enabled=True,
        now=_NOW,
    )
    assert outcome.entry.status == "failed_terminal"
    assert reservation in outcome.entry.failure_reason.lower()
    assert calls == []


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("delete_admission", "admission"),
        ("delete_approval", "approval"),
        ("payload", "payload"),
        ("approval_payload", "payload"),
        ("approval_effect", "effect"),
        ("operation", "capability"),
        ("blast", "blast"),
        ("rate_cap", "capacity"),
    ],
)
def test_retained_admission_corruption_is_fail_closed(
    tmp_path: Path, change: str, message: str,
) -> None:
    from src.core._db import open_program_db_with_retry

    root = tmp_path / "programs"
    request = _request()
    entry, approval = _enqueue(root, request)
    db = outbox_db_path(_PROGRAM, programs_root=root)
    with open_program_db_with_retry(db, durability="strict") as connection:
        if change == "delete_admission":
            connection.execute("DELETE FROM outbox_admissions WHERE outbox_id = ?", (entry.outbox_id,))
        elif change == "delete_approval":
            connection.execute("DELETE FROM outbox_approvals WHERE approval_id = ?", (approval.approval_id,))
        elif change == "payload":
            connection.execute(
                "UPDATE outbox SET payload_json = ? WHERE outbox_id = ?",
                (request.payload_json.replace("review this item", "changed"), entry.outbox_id),
            )
        elif change == "approval_payload":
            connection.execute(
                "UPDATE outbox_approvals SET payload_sha256 = ? WHERE approval_id = ?",
                ("0" * 64, approval.approval_id),
            )
        elif change == "approval_effect":
            connection.execute(
                "UPDATE outbox_approvals SET semantic_effect_key = ? WHERE approval_id = ?",
                ("sek1:changed", approval.approval_id),
            )
        elif change == "operation":
            connection.execute(
                "UPDATE outbox_admissions SET operation = 'unknown.v1' WHERE outbox_id = ?",
                (entry.outbox_id,),
            )
        elif change == "blast":
            connection.execute(
                "UPDATE outbox_admissions SET blast_radius = 2 WHERE outbox_id = ?",
                (entry.outbox_id,),
            )
        else:
            connection.execute(
                "UPDATE outbox_admissions SET rate_limit = 0 WHERE outbox_id = ?",
                (entry.outbox_id,),
            )
    calls: list[str] = []
    outcome = dispatch_approved_actuation_intent(
        program_id=_PROGRAM,
        outbox_id=entry.outbox_id,
        owner="worker",
        programs_root=root,
        preflight_fn=lambda retained: _preflight(request=request),
        dispatch_fn=lambda retained: calls.append(retained.outbox_id) or DispatchResult(
            succeeded=True, remote_id=42,
        ),
        approval_policy_version=request.approval_policy_version,
        scope_enabled=True,
        rule_enabled=True,
        now=_NOW,
    )
    assert calls == []
    assert message in (outcome.entry.failure_reason or "").lower()


def test_conflicting_active_intent_added_after_admission_blocks_dispatch(tmp_path: Path) -> None:
    from src.core._db import open_program_db_with_retry

    root = tmp_path / "programs"
    request = _request()
    first, _ = _enqueue(root, request)
    second_request = _request(
        intent_id="intent-2", causal_occurrence_id="meeting:action-2", target_identity="43",
    )
    second, _ = _enqueue(root, second_request)
    db = outbox_db_path(_PROGRAM, programs_root=root)
    with open_program_db_with_retry(db, durability="strict") as connection:
        connection.execute(
            "UPDATE outbox_admissions SET target_identity = ? WHERE outbox_id = ?",
            (request.target_identity, second.outbox_id),
        )
    calls: list[str] = []
    outcome = dispatch_approved_actuation_intent(
        program_id=_PROGRAM,
        outbox_id=first.outbox_id,
        owner="worker",
        programs_root=root,
        preflight_fn=lambda retained: _preflight(request=request),
        dispatch_fn=lambda retained: calls.append(retained.outbox_id) or DispatchResult(
            succeeded=True, remote_id=42,
        ),
        approval_policy_version=request.approval_policy_version,
        scope_enabled=True,
        rule_enabled=True,
        now=_NOW,
    )
    assert "conflicting" in outcome.entry.failure_reason.lower()
    assert calls == []


def test_unknown_or_already_cancelled_outbox_refuses_qualified_dispatch(tmp_path: Path) -> None:
    from src.core._db import open_program_db_with_retry

    root = tmp_path / "programs"
    request = _request()
    entry, _ = _enqueue(root, request)
    with pytest.raises(OutboxError, match="does not exist"):
        dispatch_approved_actuation_intent(
            program_id=_PROGRAM, outbox_id="missing", owner="worker", programs_root=root,
            preflight_fn=lambda retained: _preflight(request=request),
            dispatch_fn=lambda retained: DispatchResult(succeeded=True, remote_id=42),
            approval_policy_version=request.approval_policy_version,
            scope_enabled=True, rule_enabled=True, now=_NOW,
        )
    db = outbox_db_path(_PROGRAM, programs_root=root)
    with open_program_db_with_retry(db, durability="strict") as connection:
        connection.execute(
            "UPDATE outbox SET status = 'cancelled', failure_reason = 'retained cancellation' "
            "WHERE outbox_id = ?",
            (entry.outbox_id,),
        )
    outcome = dispatch_approved_actuation_intent(
        program_id=_PROGRAM, outbox_id=entry.outbox_id, owner="worker", programs_root=root,
        preflight_fn=lambda retained: _preflight(request=request),
        dispatch_fn=lambda retained: pytest.fail("provider reached"),
        approval_policy_version=request.approval_policy_version,
        scope_enabled=True, rule_enabled=True, now=_NOW,
    )
    assert outcome.status == "cancelled"


def test_verification_refuses_unknown_row_directly(tmp_path: Path) -> None:
    from src.core.actuation_approval_store import verify_qualified_admission

    root = tmp_path / "programs"
    request = _request()
    _approve(root, request)
    with pytest.raises(OutboxError, match="unknown outbox"):
        verify_qualified_admission(
            outbox_db_path(_PROGRAM, programs_root=root),
            outbox_id="missing",
            observation=_preflight(request=request),
            approval_policy_version=request.approval_policy_version,
            scope_enabled=True,
            rule_enabled=True,
            now=_NOW,
        )


def test_revocation_cancels_pending_atomically_and_dispatch_never_runs(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    request = _request()
    entry, approval = _enqueue(root, request)
    result = revoke_actuation_approval(
        root,
        program_id=_PROGRAM,
        approval_id=approval.approval_id,
        intent_id=request.intent_id,
        revision=approval.revision,
        revoked_by=_identity(),
        reason="owner withdrew approval",
        now=_NOW,
    )
    assert result.cancelled_outbox_ids == (entry.outbox_id,)
    assert result.reconciliation_required_outbox_ids == ()
    assert load_entry(outbox_db_path(_PROGRAM, programs_root=root), entry.outbox_id).status == "cancelled"


def test_revocation_refuses_missing_or_stale_current_approval(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    request = _request()
    _, approval = _enqueue(root, request)
    with pytest.raises(OutboxError, match="current revision"):
        revoke_actuation_approval(
            root, program_id=_PROGRAM, approval_id="missing", intent_id=request.intent_id,
            revision=approval.revision, revoked_by=_identity(), reason="withdrawn", now=_NOW,
        )


@pytest.mark.parametrize(
    ("remote_id", "remote_rev"),
    [(0, 2), (42, 0)],
)
def test_provider_success_requires_valid_remote_identity(
    tmp_path: Path, remote_id: int, remote_rev: int
) -> None:
    root = tmp_path / "programs"
    request = _request(operation="create_task.v1")
    entry, _ = _enqueue(root, request)

    outcome = dispatch_approved_actuation_intent(
        program_id=_PROGRAM,
        outbox_id=entry.outbox_id,
        owner="worker",
        programs_root=root,
        preflight_fn=lambda retained: _preflight(request=request),
        dispatch_fn=lambda retained: _success(
            retained, remote_id=remote_id, remote_rev=remote_rev
        ),
        approval_policy_version=request.approval_policy_version,
        scope_enabled=True,
        rule_enabled=True,
        now=_NOW,
    )

    assert outcome.status == "uncertain_remote_state"
    assert "valid remote" in str(outcome.entry.failure_reason).lower()


def test_provider_compensation_request_without_policy_becomes_uncertain(
    tmp_path: Path,
) -> None:
    root = tmp_path / "programs"
    request = _request(operation="create_task.v1")
    entry, _ = _enqueue(root, request)

    outcome = dispatch_approved_actuation_intent(
        program_id=_PROGRAM,
        outbox_id=entry.outbox_id,
        owner="worker",
        programs_root=root,
        preflight_fn=lambda retained: _preflight(request=request),
        dispatch_fn=lambda retained: DispatchResult(
            succeeded=True,
            remote_id=42,
            remote_rev=2,
            compensation_required=True,
            failure_reason="compensation requested",
            reconciliation=synthetic_completed_reconciliation(
                json.loads(retained.payload_json)["authorized_effect"],
                remote_id=42,
                remote_rev=2,
            ),
        ),
        approval_policy_version=request.approval_policy_version,
        scope_enabled=True,
        rule_enabled=True,
        now=_NOW,
    )

    assert outcome.status == "uncertain_remote_state"
    assert "compensation" in str(outcome.entry.failure_reason).lower()


def test_provider_compensation_request_uses_approved_offline_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "programs"
    request = _request(operation="create_task.v1")
    entry, _ = _enqueue(root, request)
    monkeypatch.setattr(
        "src.core.actuation_outbox.require_offline_compensation_operation",
        lambda operation: "offline_compensation.v1",
    )

    outcome = dispatch_approved_actuation_intent(
        program_id=_PROGRAM,
        outbox_id=entry.outbox_id,
        owner="worker",
        programs_root=root,
        preflight_fn=lambda retained: _preflight(request=request),
        dispatch_fn=lambda retained: DispatchResult(
            succeeded=True,
            remote_id=42,
            remote_rev=2,
            compensation_required=True,
            failure_reason="compensation requested",
            reconciliation=synthetic_completed_reconciliation(
                json.loads(retained.payload_json)["authorized_effect"],
                remote_id=42,
                remote_rev=2,
            ),
        ),
        approval_policy_version=request.approval_policy_version,
        scope_enabled=True,
        rule_enabled=True,
        now=_NOW,
    )

    assert outcome.status == "compensation_required"


def test_revocation_of_completed_effect_does_not_fabricate_reconciliation(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    request = _request(operation="create_task.v1")
    entry, approval = _enqueue(root, request)
    outcome = dispatch_approved_actuation_intent(
        program_id=_PROGRAM,
        outbox_id=entry.outbox_id,
        owner="worker",
        programs_root=root,
        preflight_fn=lambda retained: _preflight(request=request),
        dispatch_fn=lambda retained: _success(retained),
        approval_policy_version=request.approval_policy_version,
        scope_enabled=True,
        rule_enabled=True,
        now=_NOW,
    )
    assert outcome.status == "completed"
    result = revoke_actuation_approval(
        root, program_id=_PROGRAM, approval_id=approval.approval_id,
        intent_id=request.intent_id, revision=approval.revision,
        revoked_by=_identity(), reason="post-completion withdrawal", now=_NOW,
    )
    assert result.cancelled_outbox_ids == ()
    assert result.reconciliation_required_outbox_ids == ()


def test_revocation_after_provider_dispatch_requires_reconciliation(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    request = _request(operation="create_task.v1")
    entry, approval = _enqueue(root, request)

    def dispatch(retained):
        result = revoke_actuation_approval(
            root,
            program_id=_PROGRAM,
            approval_id=approval.approval_id,
            intent_id=request.intent_id,
            revision=approval.revision,
            revoked_by=_identity(),
            reason="owner withdrew approval after dispatch",
            now=_NOW,
        )
        assert result.reconciliation_required_outbox_ids == (entry.outbox_id,)
        return _success(retained)

    outcome = dispatch_approved_actuation_intent(
        program_id=_PROGRAM,
        outbox_id=entry.outbox_id,
        owner="worker",
        programs_root=root,
        preflight_fn=lambda retained: _preflight(request=request),
        dispatch_fn=dispatch,
        approval_policy_version=request.approval_policy_version,
        scope_enabled=True,
        rule_enabled=True,
        now=_NOW,
    )
    assert outcome.status == "completed"
    assert load_entry(outbox_db_path(_PROGRAM, programs_root=root), entry.outbox_id).status == "completed"


def test_revoked_dispatched_entry_is_reconciliation_not_cancelled(tmp_path: Path) -> None:
    from src.core._db import open_program_db_with_retry

    root = tmp_path / "programs"
    request = _request()
    entry, approval = _enqueue(root, request)
    db = outbox_db_path(_PROGRAM, programs_root=root)
    with open_program_db_with_retry(db, durability="strict") as connection:
        connection.execute(
            "UPDATE outbox SET status = 'dispatched', lease_owner = 'old', "
            "lease_fencing_token = 1, lease_expires_at = ? WHERE outbox_id = ?",
            ((_NOW + timedelta(minutes=1)).isoformat(), entry.outbox_id),
        )
    revoke_actuation_approval(
        root,
        program_id=_PROGRAM,
        approval_id=approval.approval_id,
        intent_id=request.intent_id,
        revision=approval.revision,
        revoked_by=_identity(),
        reason="withdrawn after dispatch",
        now=_NOW,
    )
    outcome = dispatch_approved_actuation_intent(
        program_id=_PROGRAM,
        outbox_id=entry.outbox_id,
        owner="replacement",
        programs_root=root,
        preflight_fn=lambda retained: _preflight(request=request),
        dispatch_fn=lambda retained: pytest.fail("provider reached"),
        approval_policy_version=request.approval_policy_version,
        scope_enabled=True,
        rule_enabled=True,
        now=_NOW,
    )
    assert outcome.status == "uncertain_remote_state"


def test_second_preflight_refusal_after_lease_never_calls_provider(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    request = _request(operation="create_task.v1")
    entry, _ = _enqueue(root, request)
    observations = [_preflight(request=request), replace(
        _preflight(request=request), marker_found=True,
    )]

    def preflight(retained):
        return observations.pop(0)

    outcome = dispatch_approved_actuation_intent(
        program_id=_PROGRAM,
        outbox_id=entry.outbox_id,
        owner="worker",
        programs_root=root,
        preflight_fn=preflight,
        dispatch_fn=lambda retained: pytest.fail("provider reached"),
        approval_policy_version=request.approval_policy_version,
        scope_enabled=True,
        rule_enabled=True,
        now=_NOW,
    )
    assert outcome.status == "uncertain_remote_state"


@pytest.mark.parametrize("failure_phase", ["before_lease", "after_lease"])
def test_preflight_failure_refuses_without_calling_provider(
    tmp_path: Path, failure_phase: str,
) -> None:
    root = tmp_path / "programs"
    request = _request(operation="create_task.v1")
    entry, _ = _enqueue(root, request)
    calls = 0

    def preflight(retained):
        nonlocal calls
        calls += 1
        if failure_phase == "before_lease" or calls == 2:
            raise RuntimeError("preflight source unavailable")
        return _preflight(request=request)

    with pytest.raises(OutboxError, match="preflight"):
        dispatch_approved_actuation_intent(
            program_id=_PROGRAM,
            outbox_id=entry.outbox_id,
            owner="worker",
            programs_root=root,
            preflight_fn=preflight,
            dispatch_fn=lambda retained: pytest.fail("provider reached"),
            approval_policy_version=request.approval_policy_version,
            scope_enabled=True,
            rule_enabled=True,
            now=_NOW,
        )
    assert load_entry(
        outbox_db_path(_PROGRAM, programs_root=root), entry.outbox_id,
    ).status in {"pending", "leased"}


@pytest.mark.parametrize("tamper", ["workspace", "outbox"])
def test_qualified_dispatch_refuses_lost_fence_after_lease(
    tmp_path: Path, tamper: str,
) -> None:
    from src.core._db import open_program_db_with_retry
    from src.core.ledger.durable_outbox_store import LeaseNoLongerCurrent
    from src.core.workspace_lease import get_workspace_lease_db_path

    root = tmp_path / "programs"
    request = _request(operation="create_task.v1")
    entry, _ = _enqueue(root, request)
    calls = 0

    def preflight(retained):
        nonlocal calls
        calls += 1
        if calls == 2:
            if tamper == "workspace":
                lease_db = get_workspace_lease_db_path(_PROGRAM, programs_root=root)
                with open_program_db_with_retry(lease_db, durability="strict") as connection:
                    connection.execute("DELETE FROM workspace_lease")
            else:
                db = outbox_db_path(_PROGRAM, programs_root=root)
                with open_program_db_with_retry(db, durability="strict") as connection:
                    connection.execute(
                        "UPDATE outbox SET lease_owner = 'replacement' WHERE outbox_id = ?",
                        (entry.outbox_id,),
                    )
        return _preflight(request=request)

    with pytest.raises(LeaseNoLongerCurrent):
        dispatch_approved_actuation_intent(
            program_id=_PROGRAM,
            outbox_id=entry.outbox_id,
            owner="worker",
            programs_root=root,
            preflight_fn=preflight,
            dispatch_fn=lambda retained: pytest.fail("provider reached"),
            approval_policy_version=request.approval_policy_version,
            scope_enabled=True,
            rule_enabled=True,
            now=_NOW,
        )


@pytest.mark.parametrize("operation", ["create_task.v1", "add_comment.v1", "add_tag.v1", "set_field.v1"])
def test_all_qualified_operations_dispatch_only_through_synthetic_provider(
    tmp_path: Path, operation: str,
) -> None:
    root = tmp_path / "programs"
    target = "meeting:action-1" if operation == "create_task.v1" else "42"
    request = _request(
        intent_id=f"intent:{operation}",
        operation=operation,
        target_identity=target,
        causal_occurrence_id=f"occurrence:{operation}",
    )
    entry, _ = _enqueue(root, request)
    calls: list[str] = []
    if operation == "create_task.v1":
        outcome = dispatch_approved_actuation_intent(
            program_id=_PROGRAM,
            outbox_id=entry.outbox_id,
            owner="worker",
            programs_root=root,
            preflight_fn=lambda retained: _preflight(request=request),
            dispatch_fn=lambda retained: calls.append(retained.outbox_id)
            or _success(retained, remote_id=43),
            approval_policy_version=request.approval_policy_version,
            scope_enabled=True,
            rule_enabled=True,
            now=_NOW,
        )
        assert outcome.status == "completed"
        assert calls == [entry.outbox_id]
    else:
        with pytest.raises(OutboxError, match="draft/manual"):
            dispatch_approved_actuation_intent(
                program_id=_PROGRAM,
                outbox_id=entry.outbox_id,
                owner="worker",
                programs_root=root,
                preflight_fn=lambda retained: _preflight(request=request),
                dispatch_fn=lambda retained: calls.append(retained.outbox_id)
                or _success(retained, remote_id=43),
                approval_policy_version=request.approval_policy_version,
                scope_enabled=True,
                rule_enabled=True,
                now=_NOW,
            )
        assert calls == []
