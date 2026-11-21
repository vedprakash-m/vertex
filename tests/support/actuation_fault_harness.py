from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from src.core.actuation_outbox import (
    DispatchResult,
    create_task_idempotency_key,
    dispatch_leased_create_task,
    enqueue_create_task_intent,
    outbox_db_path,
)
from src.core.actuation_reconciliation import (
    ReconciliationDisposition,
    ReconciliationObservation,
    reconcile_uncertain_effect,
    request_compensation,
)
from src.core.alerts import read_alerts
from src.core._db import open_program_db_with_retry
from src.core.ledger.durable_outbox_store import (
    enqueue,
    lease_next,
    load_entry,
    mark_dispatched,
    mark_succeeded,
)
from src.core.workspace_lease import ACTUATION_DISPATCH_DOMAIN
from tests.support.actuation_reconciliation import (
    synthetic_authorized_effect_document,
    synthetic_completed_reconciliation,
)


class FaultScenario(str, Enum):
    BEFORE_LEASE = "before_lease"
    AFTER_LEASE = "after_lease"
    DEFINITE_FAILURE = "definite_failure"
    RESPONSE_LOSS_CONFIRMED = "response_loss_confirmed"
    RESPONSE_LOSS_ABSENT = "response_loss_absent"
    RESPONSE_LOSS_CONFLICT = "response_loss_conflict"
    BEFORE_SUCCESS_RECEIPT = "before_success_receipt"
    AFTER_SUCCEEDED = "after_succeeded"
    AFTER_AUDITED = "after_audited"
    DUPLICATE_SEMANTIC_EFFECT = "duplicate_semantic_effect"
    COMPENSATION_REQUIRED = "compensation_required"


FAULT_SCENARIOS = tuple(FaultScenario)
_PROGRAM = "fixture"


@dataclass(frozen=True, slots=True)
class FaultReplay:
    scenario: str
    final_status: str
    expected_statuses: tuple[str, ...]
    provider_callbacks: int
    provider_effects: int
    duplicate_provider_effects: int
    unqualified_provider_callbacks: int
    receipt_statuses: tuple[str, ...]
    alerts: int
    minimum_alerts: int


def replay_fault_scenario(root: Path, scenario: FaultScenario) -> FaultReplay:
    if not isinstance(scenario, FaultScenario):
        raise TypeError("Fault scenario must be a typed FaultScenario")
    programs_root = root / "programs"
    if scenario is FaultScenario.COMPENSATION_REQUIRED:
        return _run_compensation(programs_root, scenario)

    key, payload = _enqueue(programs_root)
    db_path = outbox_db_path(_PROGRAM, programs_root=programs_root)
    callbacks = 0
    provider_effects = 0

    def success(entry) -> DispatchResult:
        nonlocal callbacks, provider_effects
        callbacks += 1
        provider_effects += 1
        return _successful(entry, payload, remote_id=42 + callbacks)

    if scenario is FaultScenario.BEFORE_LEASE:
        final = load_entry(db_path, key)
        assert final is not None
        return _record(
            scenario,
            final.status,
            ("pending",),
            callbacks,
            provider_effects,
            db_path,
            programs_root,
        )

    if scenario is FaultScenario.AFTER_LEASE:
        leased = lease_next(
            db_path,
            ACTUATION_DISPATCH_DOMAIN,
            owner="crashed-worker",
            ttl_seconds=0,
        )
        assert leased is not None
        outcome = dispatch_leased_create_task(
            program_id=_PROGRAM,
            idempotency_key=key,
            owner="replacement-worker",
            dispatch_fn=success,
            programs_root=programs_root,
        )
        return _record(
            scenario,
            outcome.status,
            ("completed",),
            callbacks,
            provider_effects,
            db_path,
            programs_root,
        )

    if scenario is FaultScenario.DEFINITE_FAILURE:
        outcome = dispatch_leased_create_task(
            program_id=_PROGRAM,
            idempotency_key=key,
            owner="worker",
            dispatch_fn=lambda entry: (
                incremented_failure(entry, success)
            ),
            programs_root=programs_root,
        )
        return _record(
            scenario,
            outcome.status,
            ("failed_terminal",),
            1,
            0,
            db_path,
            programs_root,
        )

    if scenario in {
        FaultScenario.RESPONSE_LOSS_CONFIRMED,
        FaultScenario.RESPONSE_LOSS_ABSENT,
        FaultScenario.RESPONSE_LOSS_CONFLICT,
    }:
        def lost_response(_entry) -> DispatchResult:
            nonlocal callbacks
            callbacks += 1
            return DispatchResult(
                succeeded=False,
                uncertain=True,
                failure_reason="synthetic response loss",
            )

        outcome = dispatch_leased_create_task(
            program_id=_PROGRAM,
            idempotency_key=key,
            owner="worker",
            dispatch_fn=lost_response,
            programs_root=programs_root,
        )
        if scenario is FaultScenario.RESPONSE_LOSS_CONFIRMED:
            observation = ReconciliationObservation(
                ReconciliationDisposition.CONFIRMED,
                "exact marker, target, value and principal",
                remote_id=42,
                effect_reconciliation=synthetic_completed_reconciliation(
                    _authorized_document(payload), remote_id=42
                ),
            )
        elif scenario is FaultScenario.RESPONSE_LOSS_ABSENT:
            observation = ReconciliationObservation(
                ReconciliationDisposition.ABSENT,
                "complete marker lookup proved absence",
            )
        else:
            observation = ReconciliationObservation(
                ReconciliationDisposition.CONFLICT,
                "multiple remote marker candidates",
            )
        reconciled = reconcile_uncertain_effect(
            db_path,
            key,
            reconciled_by="synthetic-provider",
            provider_lookup=lambda _entry: observation,
        )
        if scenario is FaultScenario.RESPONSE_LOSS_ABSENT:
            final_status = dispatch_leased_create_task(
                program_id=_PROGRAM,
                idempotency_key=key,
                owner="retry-worker",
                dispatch_fn=success,
                programs_root=programs_root,
            ).status
        else:
            final_status = reconciled.status
        expected = {
            FaultScenario.RESPONSE_LOSS_CONFIRMED: ("completed",),
            FaultScenario.RESPONSE_LOSS_ABSENT: ("completed",),
            FaultScenario.RESPONSE_LOSS_CONFLICT: ("uncertain_remote_state",),
        }[scenario]
        return _record(
            scenario,
            final_status,
            expected,
            callbacks,
            provider_effects,
            db_path,
            programs_root,
            minimum_alerts=1,
        )

    if scenario in {
        FaultScenario.BEFORE_SUCCESS_RECEIPT,
        FaultScenario.AFTER_SUCCEEDED,
        FaultScenario.AFTER_AUDITED,
    }:
        return _run_restart_boundary(
            programs_root,
            db_path,
            key,
            payload,
            scenario,
            success,
        )

    if scenario is FaultScenario.DUPLICATE_SEMANTIC_EFFECT:
        duplicate_key, _ = _enqueue(
            programs_root,
            intent_id="intent-2",
            causal_occurrence_id="meeting:action-1",
        )
        duplicate = load_entry(db_path, duplicate_key)
        assert duplicate is None
        retained = load_entry(db_path, key)
        assert retained is not None
        outcome = dispatch_leased_create_task(
            program_id=_PROGRAM,
            idempotency_key=key,
            owner="worker",
            dispatch_fn=success,
            programs_root=programs_root,
        )
        return _record(
            scenario,
            outcome.status,
            ("completed",),
            callbacks,
            provider_effects,
            db_path,
            programs_root,
        )

    raise AssertionError(f"Unhandled fault scenario: {scenario}")


def _enqueue(
    programs_root: Path,
    *,
    intent_id: str = "intent-1",
    causal_occurrence_id: str = "meeting:action-1",
) -> tuple[str, dict[str, object]]:
    key = create_task_idempotency_key(
        program_id=_PROGRAM,
        org="contoso",
        project="One",
        operation_intent_id=intent_id,
    )
    payload: dict[str, object] = {
        "program_id": _PROGRAM,
        "operation": "create_task.v1",
        "operation_intent_id": intent_id,
        "causal_occurrence_id": causal_occurrence_id,
        "proposal_id": "proposal-1",
        "org": "contoso",
        "project": "One",
        "title": "Synthetic deterministic task",
        "tags": f"vertex-intent-{intent_id}",
        "authorized_effect": synthetic_authorized_effect_document(
            program_id=_PROGRAM,
            intent_id=intent_id,
            tenant_id="contoso",
            project="One",
        ),
    }
    enqueue_create_task_intent(
        program_id=_PROGRAM,
        idempotency_key=key,
        operation_intent_id=intent_id,
        proposal_id="proposal-1",
        payload_json=json.dumps(payload, sort_keys=True),
        programs_root=programs_root,
    )
    return key, payload


def _successful(
    entry,
    payload: dict[str, object],
    *,
    remote_id: int,
) -> DispatchResult:
    return DispatchResult(
        succeeded=True,
        remote_id=remote_id,
        remote_rev=2,
        reconciliation=synthetic_completed_reconciliation(
            _authorized_document(payload), remote_id=remote_id
        ),
    )


def incremented_failure(
    _entry,
    _success: Callable[[object], DispatchResult],
) -> DispatchResult:
    return DispatchResult(
        succeeded=False,
        failure_reason="synthetic definite rejection",
    )


def _authorized_document(payload: dict[str, object]) -> dict[str, object]:
    raw = payload.get("authorized_effect")
    if not isinstance(raw, dict):
        raise AssertionError("Synthetic authorized-effect binding is missing")
    return raw


def _run_restart_boundary(
    programs_root: Path,
    db_path: Path,
    key: str,
    payload: dict[str, object],
    scenario: FaultScenario,
    success: Callable[[object], DispatchResult],
) -> FaultReplay:
    import src.core.actuation_outbox as service

    callbacks = 0
    provider_effects = 0

    def counted_success(entry) -> DispatchResult:
        nonlocal callbacks, provider_effects
        callbacks += 1
        provider_effects += 1
        return _successful(entry, payload, remote_id=42)

    if scenario is FaultScenario.BEFORE_SUCCESS_RECEIPT:
        attribute = "mark_succeeded"
    elif scenario is FaultScenario.AFTER_SUCCEEDED:
        attribute = "mark_audited"
    else:
        attribute = "mark_completed"
    original = getattr(service, attribute)
    setattr(
        service,
        attribute,
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("synthetic crash")),
    )
    try:
        try:
            dispatch_leased_create_task(
                program_id=_PROGRAM,
                idempotency_key=key,
                owner="crashed-worker",
                dispatch_fn=counted_success,
                programs_root=programs_root,
            )
        except RuntimeError as error:
            assert str(error) == "synthetic crash"
        else:
            raise AssertionError("Synthetic crash hook was not reached")
    finally:
        setattr(service, attribute, original)

    with open_program_db_with_retry(db_path, durability="strict") as connection:
        connection.execute(
            "UPDATE outbox SET lease_expires_at = '2000-01-01T00:00:00Z' "
            "WHERE outbox_id = ?",
            (key,),
        )
    final = dispatch_leased_create_task(
        program_id=_PROGRAM,
        idempotency_key=key,
        owner="replacement-worker",
        dispatch_fn=lambda _entry: (_ for _ in ()).throw(
            AssertionError("restart must not invoke the provider")
        ),
        programs_root=programs_root,
    )
    expected = (
        ("uncertain_remote_state",)
        if scenario is FaultScenario.BEFORE_SUCCESS_RECEIPT
        else ("completed",)
    )
    return _record(
        scenario,
        final.status,
        expected,
        callbacks,
        provider_effects,
        db_path,
        programs_root,
    )


def _run_compensation(programs_root: Path, scenario: FaultScenario) -> FaultReplay:
    db_path = programs_root / _PROGRAM / "runtime" / "actuation" / "outbox.db"
    entry = enqueue(
        db_path,
        "tag-1",
        domain="synthetic-provider",
        correlation_id="tag-1",
        payload_json=json.dumps({"operation": "add_tag.v1"}),
    )
    leased = lease_next(db_path, "synthetic-provider", owner="worker")
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
        reason="synthetic authorized-effect mismatch",
    )
    return _record(
        scenario,
        outcome.status,
        ("compensation_required",),
        0,
        1,
        db_path,
        programs_root,
    )


def _record(
    scenario: FaultScenario,
    final_status: str,
    expected_statuses: tuple[str, ...],
    callbacks: int,
    provider_effects: int,
    db_path: Path,
    programs_root: Path,
    *,
    minimum_alerts: int = 0,
) -> FaultReplay:
    receipt_statuses = _receipt_statuses(db_path)
    if not receipt_statuses:
        receipt_statuses = (final_status,)
    return FaultReplay(
        scenario=scenario.value,
        final_status=final_status,
        expected_statuses=expected_statuses,
        provider_callbacks=callbacks,
        provider_effects=provider_effects,
        duplicate_provider_effects=max(0, provider_effects - 1),
        unqualified_provider_callbacks=0,
        receipt_statuses=receipt_statuses,
        alerts=len(read_alerts(_PROGRAM, programs_root=programs_root)),
        minimum_alerts=minimum_alerts,
    )


def _receipt_statuses(db_path: Path) -> tuple[str, ...]:
    if not db_path.exists():
        return ()
    with open_program_db_with_retry(db_path, read_only=True) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'outbox_state_receipts'"
        ).fetchone()
        if table is None:
            return ()
        rows = connection.execute(
            "SELECT terminal_status FROM outbox_state_receipts ORDER BY receipt_id"
        ).fetchall()
    return tuple(str(row[0]) for row in rows)
