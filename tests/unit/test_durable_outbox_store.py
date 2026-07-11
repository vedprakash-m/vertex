from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.ledger.durable_outbox_store import (
    LeaseNoLongerCurrent,
    OutboxError,
    enqueue,
    lease_next,
    list_by_status,
    load_entry,
    mark_audited,
    mark_completed,
    mark_dead_letter,
    mark_dispatched,
    mark_failed_retryable,
    mark_manually_resolved,
    mark_succeeded,
    mark_uncertain_remote_state,
)


def _db(tmp_path: Path) -> Path:
    return tmp_path / "outbox.sqlite3"


def test_enqueue_starts_pending(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    entry = enqueue(db_path, "op-1", domain="ado_actuation", correlation_id="intent-1", payload_json="{}")
    assert entry.status == "pending"
    assert entry.attempt_count == 0
    assert entry.lease_fencing_token == 0


def test_lease_next_returns_none_when_empty(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    assert lease_next(db_path, "ado_actuation", owner="worker-1") is None


def test_lease_next_leases_pending_row_and_bumps_attempt_and_token(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    enqueue(db_path, "op-1", domain="ado_actuation", correlation_id="intent-1", payload_json="{}")
    leased = lease_next(db_path, "ado_actuation", owner="worker-1")
    assert leased is not None
    assert leased.status == "leased"
    assert leased.lease_owner == "worker-1"
    assert leased.lease_fencing_token == 1
    assert leased.attempt_count == 1


def test_lease_next_does_not_return_already_leased_row(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    enqueue(db_path, "op-1", domain="ado_actuation", correlation_id="intent-1", payload_json="{}")
    lease_next(db_path, "ado_actuation", owner="worker-1", ttl_seconds=300)
    assert lease_next(db_path, "ado_actuation", owner="worker-2") is None


def test_stale_lease_is_reclaimed_by_another_worker(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    enqueue(db_path, "op-1", domain="ado_actuation", correlation_id="intent-1", payload_json="{}")
    lease_next(db_path, "ado_actuation", owner="worker-1", ttl_seconds=0)

    reclaimed = lease_next(db_path, "ado_actuation", owner="worker-2")
    assert reclaimed is not None
    assert reclaimed.lease_owner == "worker-2"
    assert reclaimed.lease_fencing_token == 2


def test_happy_path_lifecycle(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    enqueue(db_path, "op-1", domain="ado_actuation", correlation_id="intent-1", payload_json="{}")
    leased = lease_next(db_path, "ado_actuation", owner="worker-1")
    assert leased is not None

    dispatched = mark_dispatched(db_path, "op-1", owner="worker-1", fencing_token=leased.lease_fencing_token, remote_request_hash="sha256:req")
    assert dispatched.status == "dispatched"

    succeeded = mark_succeeded(db_path, "op-1", owner="worker-1", fencing_token=leased.lease_fencing_token, remote_response_hash="sha256:resp")
    assert succeeded.status == "succeeded"

    audited = mark_audited(db_path, "op-1", owner="worker-1", fencing_token=leased.lease_fencing_token)
    assert audited.status == "audited"

    completed = mark_completed(db_path, "op-1", owner="worker-1", fencing_token=leased.lease_fencing_token)
    assert completed.status == "completed"
    assert completed.remote_request_hash == "sha256:req"
    assert completed.remote_response_hash == "sha256:resp"


def test_transition_with_stale_fencing_token_raises(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    enqueue(db_path, "op-1", domain="ado_actuation", correlation_id="intent-1", payload_json="{}")
    leased = lease_next(db_path, "ado_actuation", owner="worker-1", ttl_seconds=0)
    assert leased is not None
    # Another worker reclaims the stale lease, bumping the fencing token.
    lease_next(db_path, "ado_actuation", owner="worker-2")

    with pytest.raises(LeaseNoLongerCurrent):
        mark_dispatched(db_path, "op-1", owner="worker-1", fencing_token=leased.lease_fencing_token)


def test_failed_retryable_is_re_leasable_once_due(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    enqueue(db_path, "op-1", domain="ado_actuation", correlation_id="intent-1", payload_json="{}")
    leased = lease_next(db_path, "ado_actuation", owner="worker-1")
    assert leased is not None

    past_due = datetime.now(timezone.utc) - timedelta(seconds=1)
    failed = mark_failed_retryable(db_path, "op-1", owner="worker-1", fencing_token=leased.lease_fencing_token, reason="timeout", next_attempt_at=past_due)
    assert failed.status == "failed_retryable"

    retried = lease_next(db_path, "ado_actuation", owner="worker-2")
    assert retried is not None
    assert retried.attempt_count == 2


def test_failed_retryable_not_yet_due_is_not_leased(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    enqueue(db_path, "op-1", domain="ado_actuation", correlation_id="intent-1", payload_json="{}")
    leased = lease_next(db_path, "ado_actuation", owner="worker-1")
    assert leased is not None

    future = datetime.now(timezone.utc) + timedelta(hours=1)
    mark_failed_retryable(db_path, "op-1", owner="worker-1", fencing_token=leased.lease_fencing_token, reason="timeout", next_attempt_at=future)

    assert lease_next(db_path, "ado_actuation", owner="worker-2") is None


def test_dead_letter_then_manually_resolved(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    enqueue(db_path, "op-1", domain="ado_actuation", correlation_id="intent-1", payload_json="{}")
    leased = lease_next(db_path, "ado_actuation", owner="worker-1")
    assert leased is not None

    dead = mark_dead_letter(db_path, "op-1", owner="worker-1", fencing_token=leased.lease_fencing_token, reason="max attempts exceeded")
    assert dead.status == "dead_letter"

    resolved = mark_manually_resolved(db_path, "op-1", resolved_by="operator@example.com", reason="verified no duplicate created; closing")
    assert resolved.status == "manually_resolved"
    assert resolved.failure_reason == "verified no duplicate created; closing"


def test_manually_resolved_rejected_from_non_terminal_status(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    enqueue(db_path, "op-1", domain="ado_actuation", correlation_id="intent-1", payload_json="{}")
    with pytest.raises(OutboxError):
        mark_manually_resolved(db_path, "op-1", resolved_by="operator@example.com", reason="nope")


def test_uncertain_remote_state_requires_manual_resolution(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    enqueue(db_path, "op-1", domain="ado_actuation", correlation_id="intent-1", payload_json="{}")
    leased = lease_next(db_path, "ado_actuation", owner="worker-1")
    assert leased is not None
    dispatched = mark_dispatched(db_path, "op-1", owner="worker-1", fencing_token=leased.lease_fencing_token)
    uncertain = mark_uncertain_remote_state(db_path, "op-1", owner="worker-1", fencing_token=dispatched.lease_fencing_token, reason="timeout after send; remote outcome unknown")
    assert uncertain.status == "uncertain_remote_state"

    resolved = mark_manually_resolved(db_path, "op-1", resolved_by="operator@example.com", reason="reconciled: item was created remotely")
    assert resolved.status == "manually_resolved"


def test_load_entry_missing_returns_none(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    assert load_entry(db_path, "does-not-exist") is None


def test_list_by_status(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    enqueue(db_path, "op-1", domain="ado_actuation", correlation_id="intent-1", payload_json="{}")
    enqueue(db_path, "op-2", domain="ado_actuation", correlation_id="intent-2", payload_json="{}")
    enqueue(db_path, "op-3", domain="teams_actuation", correlation_id="intent-3", payload_json="{}")

    ado_pending = list_by_status(db_path, "ado_actuation", "pending")
    assert {e.outbox_id for e in ado_pending} == {"op-1", "op-2"}

    teams_pending = list_by_status(db_path, "teams_actuation", "pending")
    assert {e.outbox_id for e in teams_pending} == {"op-3"}


def test_domains_are_independently_leased(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    enqueue(db_path, "op-1", domain="ado_actuation", correlation_id="intent-1", payload_json="{}")
    enqueue(db_path, "op-2", domain="teams_actuation", correlation_id="intent-2", payload_json="{}")

    ado_leased = lease_next(db_path, "ado_actuation", owner="worker-1")
    teams_leased = lease_next(db_path, "teams_actuation", owner="worker-1")
    assert ado_leased is not None and ado_leased.outbox_id == "op-1"
    assert teams_leased is not None and teams_leased.outbox_id == "op-2"
