"""ADF-W1.3 done-check: Sec 13.4 "Actuation boundaries" fault-injection matrix,
items 1-5, against the outbox-backed create-task dispatch primitive
(``src.core.actuation_outbox``).

1. after outbox enqueue      -> crash before lease is ever taken.
2. after lease                -> crash after lease_next, before dispatch_fn runs.
3. after dispatch, before response -> dispatch_fn raises (ambiguous transport fault).
4. remote success with lost response -> dispatch_fn cannot tell; requires operator resolution.
5. after response, before receipt commit -> the ledger receipt write itself fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.actuation_outbox import (
    DispatchResult,
    create_task_idempotency_key,
    dispatch_leased_create_task,
    enqueue_create_task_intent,
    outbox_db_path,
)
from src.core.alerts import read_alerts
from src.core.ledger.durable_outbox_store import (
    lease_next,
    load_entry,
    mark_manually_resolved,
)
from src.core.ledger.event_log import read_events
from src.core.workspace_lease import ACTUATION_DISPATCH_DOMAIN, LeaseHeldByAnotherOwner, acquire_lease

_PROGRAM_ID = "fixture_prog"
_ORG = "contoso"
_PROJECT = "One"


def _enqueue(programs_root: Path, *, operation_intent_id: str = "intent-1") -> str:
    key = create_task_idempotency_key(program_id=_PROGRAM_ID, org=_ORG, project=_PROJECT, operation_intent_id=operation_intent_id)
    enqueue_create_task_intent(
        program_id=_PROGRAM_ID,
        idempotency_key=key,
        operation_intent_id=operation_intent_id,
        proposal_id="prop-1",
        payload_json='{"operation_intent_id": "%s", "org": "%s", "project": "%s", "title": "t"}' % (operation_intent_id, _ORG, _PROJECT),
        programs_root=programs_root,
    )
    return key


def test_item1_crash_after_enqueue_before_lease_is_idempotent(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    key = _enqueue(programs_root)

    # "Crash" here: the process that enqueued never got to call dispatch.
    # A fresh call re-derives the same idempotency key (the real caller's
    # crash-retry path reuses the operation_intent_id persisted to the
    # proposal manifest) and drives the SAME row to completion exactly once.
    calls: list[str] = []

    def dispatch_fn(entry) -> DispatchResult:
        calls.append(entry.outbox_id)
        return DispatchResult(succeeded=True, remote_id=9001)

    outcome = dispatch_leased_create_task(
        program_id=_PROGRAM_ID, idempotency_key=key, owner="worker-a", dispatch_fn=dispatch_fn, programs_root=programs_root,
    )
    assert outcome.status == "completed"
    assert outcome.remote_id == 9001
    assert calls == [key]

    # Re-enqueue with the same key (idempotent-enqueue) must not create a
    # second row or re-drive dispatch.
    enqueue_create_task_intent(
        program_id=_PROGRAM_ID, idempotency_key=key, operation_intent_id="intent-1", proposal_id="prop-1",
        payload_json="{}", programs_root=programs_root,
    )
    entry = load_entry(outbox_db_path(_PROGRAM_ID, programs_root=programs_root), key)
    assert entry is not None
    assert entry.status == "completed"
    assert calls == [key]  # dispatch_fn was not called again


def test_item2_crash_after_lease_before_dispatch_is_reclaimed(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    key = _enqueue(programs_root)
    db_path = outbox_db_path(_PROGRAM_ID, programs_root=programs_root)

    # Simulate a worker that leased the row (status -> "leased") and then
    # crashed before ever calling dispatch_fn. Use a 0-second TTL so the
    # lease is immediately stale for the next caller.
    stale = lease_next(db_path, ACTUATION_DISPATCH_DOMAIN, owner="dead-worker", ttl_seconds=0)
    assert stale is not None
    assert stale.outbox_id == key

    calls: list[str] = []

    def dispatch_fn(entry) -> DispatchResult:
        calls.append(entry.outbox_id)
        return DispatchResult(succeeded=True, remote_id=9002)

    # A fresh call (different owner) must reclaim the stale-leased row and
    # drive it to completion -- exactly one dispatch_fn call, from the
    # reclaiming owner.
    outcome = dispatch_leased_create_task(
        program_id=_PROGRAM_ID, idempotency_key=key, owner="worker-b", dispatch_fn=dispatch_fn, programs_root=programs_root,
    )
    assert outcome.status == "completed"
    assert calls == [key]


def test_item3_ambiguous_exception_after_dispatch_goes_uncertain_not_retried(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    key = _enqueue(programs_root)

    calls = {"count": 0}

    def dispatch_fn(entry) -> DispatchResult:
        calls["count"] += 1
        raise ConnectionError("simulated lost response after server commit")

    outcome = dispatch_leased_create_task(
        program_id=_PROGRAM_ID, idempotency_key=key, owner="worker-a", dispatch_fn=dispatch_fn, programs_root=programs_root,
    )
    assert outcome.status == "uncertain_remote_state"
    assert calls["count"] == 1

    # uncertain_remote_state is a true terminal: nothing eligible for
    # lease_next means no silent automatic retry can duplicate the remote
    # effect (INV-ADF-9).
    db_path = outbox_db_path(_PROGRAM_ID, programs_root=programs_root)
    assert lease_next(db_path, ACTUATION_DISPATCH_DOMAIN, owner="worker-c", ttl_seconds=60) is None

    events = read_events(_PROGRAM_ID, programs_root=programs_root)
    receipts = [event for event in events if event.event_type == "actuation.receipt_recorded.v1"]
    assert any(receipt.payload["receipt_state"] == "uncertain_remote_state" for receipt in receipts)

    # ADF-W5.8: an entity-scoped, cooldown-tracked alert is also raised for
    # the ambiguous dispatch (Section 8.2.5's "outbox dead letter or
    # uncertain remote state" category).
    alerts = read_alerts(_PROGRAM_ID, programs_root=programs_root)
    outbox_alerts = [a for a in alerts if a.category == "outbox_uncertain_remote_state"]
    assert len(outbox_alerts) == 1
    assert outbox_alerts[0].entity_type == "outbox_entry"
    assert outbox_alerts[0].occurrence_count == 1
    assert outbox_alerts[0].suppressed_count == 0


def test_item4_remote_success_with_lost_response_requires_operator_resolution(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    key = _enqueue(programs_root)
    db_path = outbox_db_path(_PROGRAM_ID, programs_root=programs_root)

    # The provider actually created the work item, but dispatch_fn itself
    # cannot distinguish "created, response lost" from "never received" --
    # it reports uncertain, matching what a real timeout/connection-reset
    # would force at this layer.
    def dispatch_fn(entry) -> DispatchResult:
        return DispatchResult(succeeded=False, uncertain=True, failure_reason="timed out waiting for response")

    outcome = dispatch_leased_create_task(
        program_id=_PROGRAM_ID, idempotency_key=key, owner="worker-a", dispatch_fn=dispatch_fn, programs_root=programs_root,
    )
    assert outcome.status == "uncertain_remote_state"

    # No automatic path out of uncertain_remote_state -- only an explicit
    # operator close-out (the higher-level ado_writer search-before-create
    # preflight is the real reconciliation mechanism; at the outbox layer,
    # manual resolution is what closes the row).
    resolved = mark_manually_resolved(db_path, key, resolved_by="operator:alice", reason="reconciled via WIQL tag search; work item 9003 adopted")
    assert resolved.status == "manually_resolved"
    with pytest.raises(Exception):
        mark_manually_resolved(db_path, key, resolved_by="operator:alice", reason="already resolved")


def test_item5_receipt_ledger_write_failure_does_not_block_completion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    programs_root = tmp_path / "programs"
    key = _enqueue(programs_root)

    def dispatch_fn(entry) -> DispatchResult:
        return DispatchResult(succeeded=True, remote_id=9004)

    def _raise_write_event(*args, **kwargs):
        raise OSError("simulated ledger append failure (SMB contention)")

    monkeypatch.setattr("src.core.ledger.event_log.write_event", _raise_write_event)

    outcome = dispatch_leased_create_task(
        program_id=_PROGRAM_ID, idempotency_key=key, owner="worker-a", dispatch_fn=dispatch_fn, programs_root=programs_root,
    )
    # The outbox row's own succeeded/completed transition is durable in
    # SQLite regardless of the ledger (secondary audit trail) write outcome.
    assert outcome.status == "completed"
    assert outcome.remote_id == 9004
    entry = load_entry(outbox_db_path(_PROGRAM_ID, programs_root=programs_root), key)
    assert entry is not None
    assert entry.status == "completed"

    # And no receipt event was actually recorded (the write failed) -- this
    # module does not pretend otherwise.
    events = read_events(_PROGRAM_ID, programs_root=programs_root)
    assert not [event for event in events if event.event_type == "actuation.receipt_recorded.v1"]


def test_workspace_lease_contention_propagates_to_caller(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    key = _enqueue(programs_root)
    acquire_lease(_PROGRAM_ID, "other-worker", mutation_domain=ACTUATION_DISPATCH_DOMAIN, ttl_seconds=300, programs_root=programs_root)

    def dispatch_fn(entry) -> DispatchResult:  # pragma: no cover - must not be reached
        raise AssertionError("dispatch_fn must not run while the workspace lease is held by another owner")

    with pytest.raises(LeaseHeldByAnotherOwner):
        dispatch_leased_create_task(
            program_id=_PROGRAM_ID, idempotency_key=key, owner="worker-a", dispatch_fn=dispatch_fn, programs_root=programs_root,
        )


def test_intent_created_event_emitted_once_on_first_enqueue(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    key = _enqueue(programs_root)
    _enqueue(programs_root)  # idempotent re-enqueue

    events = read_events(_PROGRAM_ID, programs_root=programs_root)
    intent_events = [event for event in events if event.event_type == "actuation.intent_created.v1"]
    assert len(intent_events) == 1
    assert intent_events[0].payload["idempotency_key"] == key
    assert intent_events[0].payload["operation_type"] == "create_task.v1"


__all__: list[str] = []
