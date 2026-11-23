"""RELO-W5 items H/S: effect identity, durable effect states, marker-only
reconciliation of uncertain effects, and the create-task-only dispatch allowlist."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.actuation_outbox import (
    DispatchResult,
    actuation_idempotency_key,
    create_task_idempotency_key,
    dispatch_leased_create_task,
    effect_id_v1,
    effect_state,
    enqueue_create_task_intent,
    outbox_db_path,
    reconcile_uncertain_create_task,
    require_dispatchable_operation,
)
from src.core.ledger.durable_outbox_store import (
    OutboxError,
    load_entry,
    load_reconciliation_receipt,
    load_success_receipt,
)
from tests.support.actuation_reconciliation import (
    synthetic_authorized_effect_document,
    synthetic_completed_reconciliation,
)

_BASE = {
    "operation": "create_task.v1",
    "target": "org/project:new",
    "payload": {"title": "Ship", "area": "A\\B"},
    "approval_id": "approval-1",
    "audience": "ado:org/project",
}


def _enqueue(root: Path, *, intent: str = "intent-1", effect_id: str | None = None):
    key = create_task_idempotency_key(program_id="fixture", org="org", project="project", operation_intent_id=intent)
    payload = json.dumps({
        "program_id": "fixture",
        "operation": "create_task.v1",
        "operation_intent_id": intent,
        "proposal_id": "p",
        "org": "org",
        "project": "project",
        "authorized_effect": synthetic_authorized_effect_document(intent_id=intent),
    })
    entry = enqueue_create_task_intent(
        program_id="fixture", idempotency_key=key, operation_intent_id=intent,
        proposal_id="p", payload_json=payload, programs_root=root, effect_id=effect_id,
    )
    return key, entry


def _dispatch(root: Path, key: str, fn, owner: str = "worker"):
    return dispatch_leased_create_task(
        program_id="fixture", idempotency_key=key, owner=owner, dispatch_fn=fn, programs_root=root,
    )


def _timeout(entry):
    raise TimeoutError("read timed out after send")


def _successful_dispatch(entry, remote_id: int) -> DispatchResult:
    authorized = json.loads(entry.payload_json)["authorized_effect"]
    return DispatchResult(
        succeeded=True,
        remote_id=remote_id,
        remote_rev=2,
        reconciliation=synthetic_completed_reconciliation(
            authorized,
            remote_id=remote_id,
            remote_rev=2,
        ),
    )


def test_effect_id_is_deterministic_and_ignores_payload_key_order() -> None:
    reordered = dict(_BASE, payload={"area": "A\\B", "title": "Ship"})
    first = effect_id_v1(**_BASE)
    assert first == effect_id_v1(**reordered)
    assert len(first) == 64 and int(first, 16) >= 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operation", "add_comment.v1"),
        ("target", "org/project:42"),
        ("payload", {"title": "Ship!", "area": "A\\B"}),
    ],
)
def test_effect_id_changes_with_each_identity_field(field: str, value: object) -> None:
    assert effect_id_v1(**_BASE) != effect_id_v1(**dict(_BASE, **{field: value}))


def test_only_create_task_is_dispatchable() -> None:
    require_dispatchable_operation("create_task.v1")
    for operation in (
        "add_comment.v1",
        "add_tag.v1",
        "set_field.v1",
        "remove_tag.v1",
        "set_tags.v1",
    ):
        with pytest.raises(OutboxError, match="draft/manual"):
            require_dispatchable_operation(operation)
    for operation in ("teams_message.v1", "email.v1", "update_field.v1", "collaboration_write.v1"):
        with pytest.raises(OutboxError, match="draft/manual"):
            require_dispatchable_operation(operation)


def test_unqualified_operation_cannot_leave_outbox_even_when_payload_says_dispatch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "programs"
    key = actuation_idempotency_key(
        program_id="fixture",
        org="org",
        project="project",
        operation="add_comment.v1",
        target_identity="42",
        operation_intent_id="intent-1",
    )
    enqueue_create_task_intent(
        program_id="fixture",
        idempotency_key=key,
        operation_intent_id="intent-1",
        proposal_id="proposal-1",
        payload_json=json.dumps({"operation": "add_comment.v1", "operation_intent_id": "intent-1"}),
        programs_root=root,
        operation="add_comment.v1",
        target_identity="42",
    )
    calls: list[str] = []
    with pytest.raises(OutboxError, match="draft/manual only"):
        dispatch_leased_create_task(
            program_id="fixture",
            idempotency_key=key,
            owner="worker",
            dispatch_fn=lambda entry: calls.append(entry.outbox_id)
            or DispatchResult(succeeded=True, remote_id=42),
            programs_root=root,
        )
    assert calls == []
    retained = load_entry(outbox_db_path("fixture", programs_root=root), key)
    assert retained is not None and retained.status == "pending"


def test_create_without_authorized_effect_cannot_call_provider(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    key = create_task_idempotency_key(
        program_id="fixture",
        org="org",
        project="project",
        operation_intent_id="intent-unbound",
    )
    enqueue_create_task_intent(
        program_id="fixture",
        idempotency_key=key,
        operation_intent_id="intent-unbound",
        proposal_id="proposal-1",
        payload_json=json.dumps({
            "operation": "create_task.v1",
            "operation_intent_id": "intent-unbound",
            "org": "org",
            "project": "project",
        }),
        programs_root=root,
    )
    calls: list[str] = []
    outcome = dispatch_leased_create_task(
        program_id="fixture",
        idempotency_key=key,
        owner="worker",
        dispatch_fn=lambda entry: calls.append(entry.outbox_id)
        or _successful_dispatch(entry, 42),
        programs_root=root,
    )
    assert outcome.status == "failed_terminal"
    assert calls == []


@pytest.mark.parametrize("payload", ["[]", "{not-json"])
def test_dispatch_refuses_non_object_payload_before_provider(
    tmp_path: Path, payload: str,
) -> None:
    root = tmp_path / "programs"
    key = create_task_idempotency_key(
        program_id="fixture",
        org="org",
        project="project",
        operation_intent_id="intent-malformed",
    )
    enqueue_create_task_intent(
        program_id="fixture",
        idempotency_key=key,
        operation_intent_id="intent-malformed",
        proposal_id="proposal-1",
        payload_json=payload,
        programs_root=root,
    )
    calls: list[str] = []
    outcome = dispatch_leased_create_task(
        program_id="fixture",
        idempotency_key=key,
        owner="worker",
        dispatch_fn=lambda entry: calls.append(entry.outbox_id)
        or _successful_dispatch(entry, 42),
        programs_root=root,
    )
    assert outcome.status == "failed_terminal"
    assert calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("program_id", "foreign"),
        ("operation", "add_comment.v1"),
        ("intent_id", "foreign"),
        ("tenant_id", "foreign"),
        ("project", "foreign"),
        ("preflight_revision", 0),
    ],
)
def test_dispatch_refuses_changed_authorization_binding(
    tmp_path: Path, field: str, value: object,
) -> None:
    root = tmp_path / "programs"
    intent = "intent-binding"
    document = synthetic_authorized_effect_document(intent_id=intent)
    document[field] = value
    key = create_task_idempotency_key(
        program_id="fixture",
        org="org",
        project="project",
        operation_intent_id=intent,
    )
    enqueue_create_task_intent(
        program_id="fixture",
        idempotency_key=key,
        operation_intent_id=intent,
        proposal_id="proposal-1",
        payload_json=json.dumps({
            "operation": "create_task.v1",
            "operation_intent_id": intent,
            "org": "org",
            "project": "project",
            "authorized_effect": document,
        }),
        programs_root=root,
    )
    calls: list[str] = []
    outcome = dispatch_leased_create_task(
        program_id="fixture",
        idempotency_key=key,
        owner="worker",
        dispatch_fn=lambda entry: calls.append(entry.outbox_id)
        or _successful_dispatch(entry, 42),
        programs_root=root,
    )
    assert outcome.status == "failed_terminal"
    assert calls == []


def test_partial_reconciliation_is_terminal_uncertain_and_retains_receipt(tmp_path: Path) -> None:
    from src.core.actuation_reconciliation import EffectReconciliation

    root = tmp_path / "programs"
    key, _ = _enqueue(root)
    reconciliation = EffectReconciliation(
        status="partial",
        receipts=(),
        mismatches=("recipient bob: provider evidence missing",),
    )
    outcome = _dispatch(
        root,
        key,
        lambda entry: DispatchResult(
            succeeded=True,
            remote_id=7,
            reconciliation=reconciliation,
        ),
    )
    assert outcome.status == "uncertain_remote_state"
    retained = load_reconciliation_receipt(outbox_db_path("fixture", programs_root=root), key)
    assert retained is not None
    assert json.loads(retained)["status"] == "partial"
    assert json.loads(retained)["mismatches"]
    assert load_success_receipt(outbox_db_path("fixture", programs_root=root), key) is None


def test_reconciliation_receipt_is_replaced_after_verified_absent_redispatch(
    tmp_path: Path,
) -> None:
    from src.core.actuation_reconciliation import EffectReconciliation

    root = tmp_path / "programs"
    key, _ = _enqueue(root)
    first = EffectReconciliation(
        status="partial",
        receipts=(),
        mismatches=("first attempt",),
    )
    _dispatch(
        root,
        key,
        lambda entry: DispatchResult(
            succeeded=True,
            remote_id=7,
            remote_rev=2,
            reconciliation=first,
        ),
    )
    reconcile_uncertain_create_task(
        program_id="fixture",
        idempotency_key=key,
        marker_lookup=lambda entry: None,
        reconciled_by="operator",
        programs_root=root,
    )
    second = EffectReconciliation(
        status="partial",
        receipts=(),
        mismatches=("second attempt",),
    )
    outcome = _dispatch(
        root,
        key,
        lambda entry: DispatchResult(
            succeeded=True,
            remote_id=8,
            remote_rev=3,
            reconciliation=second,
        ),
    )
    assert outcome.status == "uncertain_remote_state"
    retained = load_reconciliation_receipt(
        outbox_db_path("fixture", programs_root=root),
        key,
    )
    assert retained is not None
    assert json.loads(retained)["mismatches"] == ["second attempt"]


def test_verified_absent_clears_prior_receipt_before_uncertain_redispatch(
    tmp_path: Path,
) -> None:
    from src.core.actuation_reconciliation import EffectReconciliation

    root = tmp_path / "programs"
    key, _ = _enqueue(root)
    _dispatch(
        root,
        key,
        lambda entry: DispatchResult(
            succeeded=True,
            remote_id=7,
            remote_rev=2,
            reconciliation=EffectReconciliation(
                status="partial",
                receipts=(),
                mismatches=("first attempt",),
            ),
        ),
    )
    reconcile_uncertain_create_task(
        program_id="fixture",
        idempotency_key=key,
        marker_lookup=lambda entry: None,
        reconciled_by="operator",
        programs_root=root,
    )
    db = outbox_db_path("fixture", programs_root=root)
    assert load_reconciliation_receipt(db, key) is None
    outcome = _dispatch(root, key, _timeout)
    assert outcome.status == "uncertain_remote_state"
    assert load_reconciliation_receipt(db, key) is None


def test_duplicate_effect_id_does_not_create_second_effect(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    effect = effect_id_v1(**_BASE)
    key, first = _enqueue(root, intent="intent-1", effect_id=effect)
    _, duplicate = _enqueue(root, intent="intent-2", effect_id=effect)
    assert duplicate.outbox_id == first.outbox_id == key
    other_key = create_task_idempotency_key(program_id="fixture", org="org", project="project", operation_intent_id="intent-2")
    assert load_entry(outbox_db_path("fixture", programs_root=root), other_key) is None
    calls: list[str] = []
    outcome = _dispatch(root, key, lambda entry: calls.append(entry.outbox_id) or _successful_dispatch(entry, 7))
    assert outcome.status == "completed"
    with pytest.raises(OutboxError):
        _dispatch(root, key, lambda entry: calls.append(entry.outbox_id) or _successful_dispatch(entry, 8))
    assert calls == [key]


def test_effect_states_intent_dispatched_confirmed(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    key, entry = _enqueue(root)
    assert effect_state(entry) == "intent"
    seen: list[str] = []

    def fn(dispatched):
        seen.append(effect_state(load_entry(outbox_db_path("fixture", programs_root=root), key)))
        return _successful_dispatch(dispatched, 11)

    outcome = _dispatch(root, key, fn)
    assert seen == ["dispatched"]
    assert effect_state(outcome.entry) == "confirmed"


def test_ambiguous_timeout_is_uncertain_and_never_blindly_resent(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    key, _ = _enqueue(root)
    assert _dispatch(root, key, _timeout).status == "uncertain_remote_state"
    calls: list[str] = []
    again = _dispatch(root, key, lambda entry: calls.append("sent") or DispatchResult(succeeded=True, remote_id=1))
    assert effect_state(again.entry) == "uncertain"
    assert calls == []


def test_reconcile_marker_found_confirms_without_dispatch(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    key, _ = _enqueue(root)
    _dispatch(root, key, _timeout)
    looked_up: list[str] = []
    outcome = reconcile_uncertain_create_task(
        program_id="fixture", idempotency_key=key, reconciled_by="op",
        marker_lookup=lambda entry: looked_up.append(entry.outbox_id) or 99,
        reconciliation=synthetic_completed_reconciliation(
            synthetic_authorized_effect_document(),
            remote_id=99,
        ),
        programs_root=root,
    )
    assert looked_up == [key]
    assert outcome.status == "completed" and outcome.remote_id == 99
    assert effect_state(outcome.entry) == "confirmed"
    receipt = json.loads(load_success_receipt(outbox_db_path("fixture", programs_root=root), key))
    assert receipt["remote_id"] == 99 and receipt["reconciled_by_marker"] is True


def test_authorized_marker_found_without_effect_reconciliation_stays_uncertain(
    tmp_path: Path,
) -> None:
    root = tmp_path / "programs"
    key = create_task_idempotency_key(
        program_id="fixture",
        org="org",
        project="project",
        operation_intent_id="intent-authorized",
    )
    payload = {
        "operation": "create_task.v1",
        "operation_intent_id": "intent-authorized",
        "proposal_id": "p",
        "org": "org",
        "project": "project",
        "authorized_effect": synthetic_authorized_effect_document(
            intent_id="intent-authorized",
        ),
    }
    enqueue_create_task_intent(
        program_id="fixture",
        idempotency_key=key,
        operation_intent_id="intent-authorized",
        proposal_id="p",
        payload_json=json.dumps(payload),
        programs_root=root,
    )
    _dispatch(root, key, _timeout)
    outcome = reconcile_uncertain_create_task(
        program_id="fixture",
        idempotency_key=key,
        reconciled_by="op",
        marker_lookup=lambda entry: 99,
        programs_root=root,
    )
    assert outcome.status == "uncertain_remote_state"
    assert effect_state(load_entry(outbox_db_path("fixture", programs_root=root), key)) == "uncertain"
    assert load_success_receipt(outbox_db_path("fixture", programs_root=root), key) is None


def test_reconcile_verified_absent_allows_one_redispatch_under_same_key(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    key, _ = _enqueue(root)
    _dispatch(root, key, _timeout)
    outcome = reconcile_uncertain_create_task(
        program_id="fixture", idempotency_key=key, reconciled_by="op",
        marker_lookup=lambda entry: None, programs_root=root,
    )
    assert effect_state(outcome.entry) == "intent"
    calls: list[str] = []
    final = _dispatch(root, key, lambda entry: calls.append(entry.outbox_id) or _successful_dispatch(entry, 5))
    assert calls == [key]
    assert final.status == "completed" and final.remote_id == 5


@pytest.mark.parametrize("lookup", [lambda entry: (_ for _ in ()).throw(ConnectionError("down")), lambda entry: 0, lambda entry: "7"])
def test_reconcile_failed_or_invalid_lookup_stays_uncertain(tmp_path: Path, lookup) -> None:
    root = tmp_path / "programs"
    key, _ = _enqueue(root)
    _dispatch(root, key, _timeout)
    outcome = reconcile_uncertain_create_task(
        program_id="fixture", idempotency_key=key, reconciled_by="op", marker_lookup=lookup, programs_root=root,
    )
    assert outcome.status == "uncertain_remote_state"
    assert effect_state(load_entry(outbox_db_path("fixture", programs_root=root), key)) == "uncertain"


def test_reconcile_refuses_non_uncertain_rows(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    key, _ = _enqueue(root)
    with pytest.raises(OutboxError, match="uncertain"):
        reconcile_uncertain_create_task(
            program_id="fixture", idempotency_key=key, reconciled_by="op",
            marker_lookup=lambda entry: 1, programs_root=root,
        )
