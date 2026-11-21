from __future__ import annotations

import json
from pathlib import Path

import pytest

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
from src.core.ledger.durable_outbox_store import (
    _STATE_RECEIPTS_SQL,
    OutboxError,
    enqueue,
    lease_next,
    load_entry,
    load_latest_receipt,
    mark_dispatched,
    mark_failed_terminal,
    mark_succeeded,
    mark_uncertain_remote_state,
)
from src.core.workspace_lease import ACTUATION_DISPATCH_DOMAIN
from tests.support.actuation_reconciliation import synthetic_authorized_effect_document


def _uncertain_create(programs_root: Path) -> tuple[str, Path]:
    key = create_task_idempotency_key(
        program_id="fixture",
        org="org",
        project="project",
        operation_intent_id="intent-1",
    )
    payload = {
        "operation": "create_task.v1",
        "operation_intent_id": "intent-1",
        "proposal_id": "proposal-1",
        "org": "org",
        "project": "project",
        "title": "Synthetic item",
        "authorized_effect": synthetic_authorized_effect_document(),
    }
    enqueue_create_task_intent(
        program_id="fixture",
        idempotency_key=key,
        operation_intent_id="intent-1",
        proposal_id="proposal-1",
        payload_json=json.dumps(payload),
        programs_root=programs_root,
    )
    outcome = dispatch_leased_create_task(
        program_id="fixture",
        idempotency_key=key,
        owner="worker",
        dispatch_fn=lambda entry: DispatchResult(
            succeeded=False,
            uncertain=True,
            failure_reason="synthetic response loss",
        ),
        programs_root=programs_root,
    )
    assert outcome.status == "uncertain_remote_state"
    return key, outbox_db_path("fixture", programs_root=programs_root)


@pytest.mark.parametrize(
    ("observation", "expected_status", "expected_remote_id"),
    [
        (
            ReconciliationObservation(
                disposition=ReconciliationDisposition.CONFIRMED,
                remote_id=41,
                reason="one exact marker match",
            ),
            "completed",
            41,
        ),
        (
            ReconciliationObservation(
                disposition=ReconciliationDisposition.ABSENT,
                reason="complete lookup found no marker",
            ),
            "pending",
            None,
        ),
    ],
)
def test_reconcile_uncertain_effect_records_authoritative_terminal_outcome(
    tmp_path: Path,
    observation: ReconciliationObservation,
    expected_status: str,
    expected_remote_id: int | None,
) -> None:
    key, db_path = _uncertain_create(tmp_path)

    outcome = reconcile_uncertain_effect(
        db_path,
        key,
        reconciled_by="synthetic-double",
        provider_lookup=lambda entry: observation,
    )

    assert outcome.entry.status == expected_status
    assert outcome.remote_id == expected_remote_id
    receipt = json.loads(load_latest_receipt(db_path, key) or "{}")
    assert receipt["terminal_status"] == expected_status
    assert receipt["reconciled_by"] == "synthetic-double"
    assert receipt["disposition"] == observation.disposition.value


@pytest.mark.parametrize(
    "disposition",
    [
        ReconciliationDisposition.INCONCLUSIVE,
        ReconciliationDisposition.CONFLICT,
    ],
)
def test_reconcile_uncertain_effect_blocks_incomplete_or_conflicting_lookup(
    tmp_path: Path,
    disposition: ReconciliationDisposition,
) -> None:
    key, db_path = _uncertain_create(tmp_path)

    outcome = reconcile_uncertain_effect(
        db_path,
        key,
        reconciled_by="synthetic-double",
        provider_lookup=lambda entry: ReconciliationObservation(
            disposition=disposition,
            reason="provider double cannot prove a unique exact effect",
        ),
    )

    assert outcome.entry.status == "uncertain_remote_state"
    assert outcome.remote_id is None
    receipt = json.loads(load_latest_receipt(db_path, key) or "{}")
    assert receipt["terminal_status"] == "uncertain_remote_state"
    assert receipt["disposition"] == disposition.value


def test_reconcile_uncertain_effect_contains_lookup_exception_type_only(
    tmp_path: Path,
) -> None:
    key, db_path = _uncertain_create(tmp_path)

    def fail_lookup(entry):
        raise ConnectionError("provider-double private body")

    outcome = reconcile_uncertain_effect(
        db_path,
        key,
        reconciled_by="synthetic-double",
        provider_lookup=fail_lookup,
    )

    assert outcome.entry.status == "uncertain_remote_state"
    receipt = json.loads(load_latest_receipt(db_path, key) or "{}")
    assert receipt["disposition"] == "inconclusive"
    assert receipt["reason"] == "provider lookup raised ConnectionError"
    assert "private body" not in receipt["reason"]


def test_reconcile_uncertain_effect_refuses_unqualified_operation_before_lookup(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "outbox.db"
    entry = enqueue(
        db_path,
        "comment-1",
        domain=ACTUATION_DISPATCH_DOMAIN,
        correlation_id="comment-1",
        payload_json=json.dumps({"operation": "add_comment.v1"}),
    )
    leased = lease_next(db_path, ACTUATION_DISPATCH_DOMAIN, owner="worker")
    assert leased is not None
    dispatched = mark_dispatched(
        db_path,
        entry.outbox_id,
        owner="worker",
        fencing_token=leased.lease_fencing_token,
    )
    mark_uncertain_remote_state(
        db_path,
        entry.outbox_id,
        owner="worker",
        fencing_token=dispatched.lease_fencing_token,
        reason="synthetic uncertainty",
    )
    calls: list[str] = []

    with pytest.raises(OutboxError, match="marker and reconciliation"):
        reconcile_uncertain_effect(
            db_path,
            entry.outbox_id,
            reconciled_by="synthetic-double",
            provider_lookup=lambda current: calls.append(current.outbox_id)
            or ReconciliationObservation(
                disposition=ReconciliationDisposition.ABSENT,
                reason="not reached",
            ),
        )

    assert calls == []
    retained = load_entry(db_path, entry.outbox_id)
    assert retained is not None and retained.status == "uncertain_remote_state"


def test_failure_transition_persists_terminal_receipt(tmp_path: Path) -> None:
    db_path = tmp_path / "outbox.db"
    entry = enqueue(
        db_path,
        "failed-1",
        domain="synthetic",
        correlation_id="failed-1",
        payload_json=json.dumps({"operation": "synthetic.v1"}),
    )
    leased = lease_next(db_path, "synthetic", owner="worker")
    assert leased is not None
    mark_dispatched(
        db_path,
        entry.outbox_id,
        owner="worker",
        fencing_token=leased.lease_fencing_token,
    )

    transition = mark_failed_terminal(
        db_path,
        entry.outbox_id,
        owner="worker",
        fencing_token=leased.lease_fencing_token,
        reason="definite synthetic rejection",
    )

    assert transition.status == "failed_terminal"
    receipt = json.loads(load_latest_receipt(db_path, entry.outbox_id) or "{}")
    assert receipt["terminal_status"] == "failed_terminal"
    assert receipt["reason"] == "definite synthetic rejection"
    assert receipt["attempt_count"] == 1


def test_refused_terminal_transition_does_not_create_receipt_table(
    tmp_path: Path,
) -> None:
    from src.core._db import open_program_db_with_retry

    db_path = tmp_path / "outbox.db"
    entry = enqueue(
        db_path,
        "failed-1",
        domain="synthetic",
        correlation_id="failed-1",
        payload_json=json.dumps({"operation": "synthetic.v1"}),
    )
    leased = lease_next(db_path, "synthetic", owner="worker")
    assert leased is not None

    with pytest.raises(OutboxError, match="lease"):
        mark_failed_terminal(
            db_path,
            entry.outbox_id,
            owner="stale-worker",
            fencing_token=leased.lease_fencing_token,
            reason="stale owner must not receive a receipt",
        )

    with open_program_db_with_retry(db_path, read_only=True) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='outbox_state_receipts'"
        ).fetchone() is None


def test_failure_receipt_insert_fault_rolls_back_terminal_transition(
    tmp_path: Path,
) -> None:
    import sqlite3

    from src.core._db import open_program_db_with_retry

    db_path = tmp_path / "outbox.db"
    entry = enqueue(
        db_path,
        "failed-1",
        domain="synthetic",
        correlation_id="failed-1",
        payload_json=json.dumps({"operation": "synthetic.v1"}),
    )
    leased = lease_next(db_path, "synthetic", owner="worker")
    assert leased is not None
    dispatched = mark_dispatched(
        db_path,
        entry.outbox_id,
        owner="worker",
        fencing_token=leased.lease_fencing_token,
    )
    with open_program_db_with_retry(db_path) as connection:
        connection.executescript(_STATE_RECEIPTS_SQL)
        connection.execute(
            "CREATE TRIGGER receipt_fault BEFORE INSERT ON outbox_state_receipts "
            "BEGIN SELECT RAISE(ABORT, 'receipt fault'); END"
        )

    with pytest.raises(sqlite3.IntegrityError):
        mark_failed_terminal(
            db_path,
            entry.outbox_id,
            owner="worker",
            fencing_token=leased.lease_fencing_token,
            reason="definite synthetic rejection",
        )

    retained = load_entry(db_path, entry.outbox_id)
    assert retained == dispatched
    assert load_latest_receipt(db_path, entry.outbox_id) is None


def test_request_compensation_is_capability_gated_and_persisted(tmp_path: Path) -> None:
    db_path = tmp_path / "outbox.db"
    entry = enqueue(
        db_path,
        "comment-1",
        domain="synthetic",
        correlation_id="comment-1",
        payload_json=json.dumps({"operation": "add_comment.v1"}),
    )
    leased = lease_next(db_path, "synthetic", owner="worker")
    assert leased is not None
    mark_dispatched(
        db_path,
        entry.outbox_id,
        owner="worker",
        fencing_token=leased.lease_fencing_token,
    )
    succeeded = mark_succeeded(
        db_path,
        entry.outbox_id,
        owner="worker",
        fencing_token=leased.lease_fencing_token,
    )

    compensated = request_compensation(
        db_path,
        entry.outbox_id,
        owner="worker",
        fencing_token=succeeded.lease_fencing_token,
        reason="synthetic recipient/effect mismatch",
    )

    assert compensated.status == "compensation_required"
    receipt = json.loads(load_latest_receipt(db_path, entry.outbox_id) or "{}")
    assert receipt["terminal_status"] == "compensation_required"
    assert receipt["operation"] == "add_comment.v1"


def test_request_compensation_refuses_noncompensable_create(tmp_path: Path) -> None:
    db_path = tmp_path / "outbox.db"
    entry = enqueue(
        db_path,
        "create-1",
        domain="synthetic",
        correlation_id="create-1",
        payload_json=json.dumps({"operation": "create_task.v1"}),
    )
    leased = lease_next(db_path, "synthetic", owner="worker")
    assert leased is not None
    mark_dispatched(
        db_path,
        entry.outbox_id,
        owner="worker",
        fencing_token=leased.lease_fencing_token,
    )
    succeeded = mark_succeeded(
        db_path,
        entry.outbox_id,
        owner="worker",
        fencing_token=leased.lease_fencing_token,
    )

    with pytest.raises(OutboxError, match="no approved compensation"):
        request_compensation(
            db_path,
            entry.outbox_id,
            owner="worker",
            fencing_token=succeeded.lease_fencing_token,
            reason="must remain manual",
        )
    assert load_entry(db_path, entry.outbox_id) == succeeded


def test_reconcile_and_compensation_fail_closed_on_invalid_rows_and_capabilities(tmp_path: Path) -> None:
    db_path = tmp_path / "outbox_invalid.db"
    bad_json = enqueue(
        db_path, "bad-json", domain="synthetic", correlation_id="bad-json", payload_json="{not-json",
    )
    leased_bad = lease_next(db_path, "synthetic", owner="worker")
    assert leased_bad is not None
    mark_dispatched(db_path, bad_json.outbox_id, owner="worker", fencing_token=leased_bad.lease_fencing_token)
    mark_uncertain_remote_state(db_path, bad_json.outbox_id, owner="worker", fencing_token=leased_bad.lease_fencing_token, reason="lost")
    with pytest.raises(OutboxError, match="not valid JSON"):
        reconcile_uncertain_effect(
            db_path, bad_json.outbox_id, reconciled_by="op", provider_lookup=lambda _e: ReconciliationObservation(ReconciliationDisposition.ABSENT, "x"),
        )

    bad_op = enqueue(
        db_path, "bad-op", domain="synthetic", correlation_id="bad-op", payload_json=json.dumps({"operation": "unknown_op.v1"}),
    )
    leased_op = lease_next(db_path, "synthetic", owner="worker")
    assert leased_op is not None
    mark_dispatched(db_path, bad_op.outbox_id, owner="worker", fencing_token=leased_op.lease_fencing_token)
    mark_uncertain_remote_state(db_path, bad_op.outbox_id, owner="worker", fencing_token=leased_op.lease_fencing_token, reason="lost")
    with pytest.raises(OutboxError, match="no registered capability"):
        reconcile_uncertain_effect(
            db_path, bad_op.outbox_id, reconciled_by="op", provider_lookup=lambda _e: ReconciliationObservation(ReconciliationDisposition.ABSENT, "x"),
        )
    with pytest.raises(OutboxError, match="unknown outbox row"):
        request_compensation(db_path, "nonexistent", owner="worker", fencing_token=1, reason="nope")
    with pytest.raises(OutboxError, match="no registered capability"):
        request_compensation(db_path, bad_op.outbox_id, owner="worker", fencing_token=leased_op.lease_fencing_token, reason="nope")

    # Non-dict JSON payload & empty operation guards + provider lookup exceptions/invalid returns
    list_row = enqueue(db_path, "list-json", domain="synthetic", correlation_id="list-json", payload_json="[1, 2]")
    leased_list = lease_next(db_path, "synthetic", owner="worker")
    assert leased_list is not None
    mark_dispatched(db_path, list_row.outbox_id, owner="worker", fencing_token=leased_list.lease_fencing_token)
    mark_uncertain_remote_state(db_path, list_row.outbox_id, owner="worker", fencing_token=leased_list.lease_fencing_token, reason="lost")
    with pytest.raises(OutboxError, match="not an object"):
        reconcile_uncertain_effect(db_path, list_row.outbox_id, reconciled_by="op", provider_lookup=lambda _e: ReconciliationObservation(ReconciliationDisposition.ABSENT, "x"))

    empty_op = enqueue(db_path, "empty-op", domain="synthetic", correlation_id="empty-op", payload_json=json.dumps({"operation": ""}))
    leased_empty = lease_next(db_path, "synthetic", owner="worker")
    assert leased_empty is not None
    mark_dispatched(db_path, empty_op.outbox_id, owner="worker", fencing_token=leased_empty.lease_fencing_token)
    mark_uncertain_remote_state(db_path, empty_op.outbox_id, owner="worker", fencing_token=leased_empty.lease_fencing_token, reason="lost")
    with pytest.raises(OutboxError, match="operation is invalid"):
        reconcile_uncertain_effect(db_path, empty_op.outbox_id, reconciled_by="op", provider_lookup=lambda _e: ReconciliationObservation(ReconciliationDisposition.ABSENT, "x"))

    valid_row = enqueue(
        db_path, "valid-create", domain="synthetic", correlation_id="valid-create",
        payload_json=json.dumps({"operation": "create_task.v1", "org": "o", "project": "p", "operation_intent_id": "i"}),
    )
    leased_valid = lease_next(db_path, "synthetic", owner="worker")
    assert leased_valid is not None
    mark_dispatched(db_path, valid_row.outbox_id, owner="worker", fencing_token=leased_valid.lease_fencing_token)
    with pytest.raises(OutboxError, match="applies only to an uncertain_remote_state"):
        reconcile_uncertain_effect(db_path, valid_row.outbox_id, reconciled_by="op", provider_lookup=lambda _e: ReconciliationObservation(ReconciliationDisposition.ABSENT, "x"))
    mark_uncertain_remote_state(db_path, valid_row.outbox_id, owner="worker", fencing_token=leased_valid.lease_fencing_token, reason="lost")

    raising_outcome = reconcile_uncertain_effect(
        db_path, valid_row.outbox_id, reconciled_by="op", provider_lookup=lambda _e: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert raising_outcome.status == "uncertain_remote_state"
    invalid_obs_outcome = reconcile_uncertain_effect(
        db_path, valid_row.outbox_id, reconciled_by="op", provider_lookup=lambda _e: "not-an-obs",  # type: ignore[return-value]
    )
    assert invalid_obs_outcome.status == "uncertain_remote_state"
