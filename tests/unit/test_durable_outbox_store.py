from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.ledger.durable_outbox_store import (
    _SCHEMA_SQL,
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
from src.core._db import open_program_db_with_retry


def _expired_dispatch_fixture(tmp_path: Path):
    from src.core.workspace_lease import ACTUATION_DISPATCH_DOMAIN, acquire_lease

    root = tmp_path / "programs"
    db = root / "fixture" / "runtime" / "outbox.db"
    enqueue(db, "key", domain=ACTUATION_DISPATCH_DOMAIN, correlation_id="key", payload_json="{}")
    leased = lease_next(db, ACTUATION_DISPATCH_DOMAIN, owner="old", ttl_seconds=0)
    assert leased is not None
    observed = mark_dispatched(db, "key", owner="old", fencing_token=leased.lease_fencing_token, remote_request_hash="request")
    handle = acquire_lease("fixture", "new", mutation_domain=ACTUATION_DISPATCH_DOMAIN, programs_root=root)
    return root, db, observed, handle


def test_classification_preserves_data_and_fences_old_worker(tmp_path: Path) -> None:
    from dataclasses import replace
    from src.core.ledger import durable_outbox_store as store

    root, db, observed, handle = _expired_dispatch_fixture(tmp_path)
    result = store.classify_expired_dispatched(db, observed, workspace_lease=handle, programs_root=root)
    assert result.status == "uncertain_remote_state"
    assert result.lease_owner == "new"
    assert result.lease_fencing_token == observed.lease_fencing_token + 1
    assert result.failure_reason == "Expired dispatched attempt; remote outcome unknown; manual reconciliation required."
    assert result.updated_at >= observed.updated_at
    assert replace(
        result, status=observed.status, lease_owner=observed.lease_owner,
        lease_fencing_token=observed.lease_fencing_token, failure_reason=observed.failure_reason,
        updated_at=observed.updated_at,
    ) == observed
    with pytest.raises(LeaseNoLongerCurrent):
        mark_succeeded(db, "key", owner="old", fencing_token=observed.lease_fencing_token)
    assert lease_next(db, observed.domain, owner="third") is None
    resolved = mark_manually_resolved(db, "key", resolved_by="operator", reason="explicit offline reconciliation")
    assert resolved.status == "manually_resolved"


@pytest.mark.parametrize("change", [
    "row_token", "row_owner", "payload", "domain", "correlation", "status",
    "missing_expiry", "changed_expiry", "active", "workspace_token", "workspace_owner", "workspace_expiry", "same_owner",
])
def test_classification_refuses_changed_or_active_fences(tmp_path: Path, change: str) -> None:
    from dataclasses import replace
    from src.core.ledger import durable_outbox_store as store
    from src.core.workspace_lease import get_workspace_lease_db_path

    root, db, observed, handle = _expired_dispatch_fixture(tmp_path)
    alterations = {
        "row_token": ("lease_fencing_token", observed.lease_fencing_token + 1),
        "row_owner": ("lease_owner", "third"),
        "payload": ("payload_json", '{"changed":true}'),
        "domain": ("domain", "other"),
        "correlation": ("correlation_id", "other"),
        "status": ("status", "completed"),
        "missing_expiry": ("lease_expires_at", None),
        "changed_expiry": ("lease_expires_at", "2000-01-01T00:00:00Z"),
        "active": ("lease_expires_at", (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")),
    }
    if change in alterations:
        column, value = alterations[change]
        with open_program_db_with_retry(db, durability="strict") as connection:
            connection.execute(f"UPDATE outbox SET {column} = ?", (value,))
    elif change == "same_owner":
        handle = replace(handle, owner="old")
        with open_program_db_with_retry(get_workspace_lease_db_path("fixture", programs_root=root), durability="strict") as connection:
            connection.execute("UPDATE workspace_lease SET owner = 'old'")
    else:
        column, value = {
            "workspace_token": ("fencing_token", handle.fencing_token + 1),
            "workspace_owner": ("owner", "third"),
            "workspace_expiry": ("expires_at", "2000-01-01T00:00:00Z"),
        }[change]
        with open_program_db_with_retry(get_workspace_lease_db_path("fixture", programs_root=root), durability="strict") as connection:
            connection.execute(f"UPDATE workspace_lease SET {column} = ?", (value,))
    before = load_entry(db, "key")
    with pytest.raises(LeaseNoLongerCurrent):
        store.classify_expired_dispatched(db, observed, workspace_lease=handle, programs_root=root)
    assert load_entry(db, "key") == before


def test_classification_clock_is_sampled_after_transaction_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import contextmanager
    from src.core.ledger import durable_outbox_store as store
    from src.core.unit_of_work import open_unit_of_work

    root, db, observed, handle = _expired_dispatch_fixture(tmp_path)
    locked = False

    @contextmanager
    def locking(databases):
        nonlocal locked
        with open_unit_of_work(databases) as transaction:
            locked = True
            yield transaction

    def clock():
        assert locked
        return handle.expires_at + timedelta(seconds=1)

    monkeypatch.setattr(store, "open_unit_of_work", locking, raising=False)
    monkeypatch.setattr(store, "_now", clock)
    before = load_entry(db, "key")
    with pytest.raises(LeaseNoLongerCurrent):
        store.classify_expired_dispatched(db, observed, workspace_lease=handle, programs_root=root)
    assert load_entry(db, "key") == before


def test_workspace_takeover_waits_until_classification_commits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from src.core.ledger import durable_outbox_store as store
    from src.core.workspace_lease import acquire_lease

    root, db, observed, handle = _expired_dispatch_fixture(tmp_path)
    started, finished = Event(), Event()
    original_clock = store._now

    def takeover():
        started.set()
        try:
            acquire_lease("fixture", "third", mutation_domain=handle.mutation_domain, programs_root=root)
        finally:
            finished.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = None

        def clock():
            nonlocal future
            future = pool.submit(takeover)
            assert started.wait(2)
            assert not finished.wait(0.1)
            return original_clock()

        monkeypatch.setattr(store, "_now", clock)
        result = store.classify_expired_dispatched(db, observed, workspace_lease=handle, programs_root=root)
        assert result.status == "uncertain_remote_state"
        assert future is not None
        from src.core.workspace_lease import LeaseHeldByAnotherOwner
        with pytest.raises(LeaseHeldByAnotherOwner):
            future.result(timeout=10)


def test_workspace_takeover_before_classification_rejects_old_handle(tmp_path: Path) -> None:
    from src.core.ledger import durable_outbox_store as store
    from src.core.workspace_lease import acquire_lease, release_lease, read_lease_state

    root, db, observed, old_handle = _expired_dispatch_fixture(tmp_path)
    release_lease(old_handle, programs_root=root)
    current = acquire_lease(
        "fixture", "third", mutation_domain=old_handle.mutation_domain, programs_root=root,
    )
    before = load_entry(db, "key")
    with pytest.raises(LeaseNoLongerCurrent):
        store.classify_expired_dispatched(db, observed, workspace_lease=old_handle, programs_root=root)
    assert load_entry(db, "key") == before
    assert read_lease_state("fixture", mutation_domain=current.mutation_domain, programs_root=root) == current


def test_old_worker_completion_waits_then_is_fenced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from src.core.ledger import durable_outbox_store as store

    root, db, observed, handle = _expired_dispatch_fixture(tmp_path)
    started, finished = Event(), Event()
    original_clock = store._now

    def complete():
        started.set()
        try:
            return mark_succeeded(db, "key", owner="old", fencing_token=observed.lease_fencing_token)
        finally:
            finished.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = None

        def clock():
            nonlocal future
            if not started.is_set():
                future = pool.submit(complete)
                assert started.wait(2)
                assert not finished.wait(0.1)
            return original_clock()

        monkeypatch.setattr(store, "_now", clock)
        result = store.classify_expired_dispatched(db, observed, workspace_lease=handle, programs_root=root)
        assert result.status == "uncertain_remote_state"
        assert future is not None
        with pytest.raises(LeaseNoLongerCurrent):
            future.result(timeout=10)


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


# ---------------------------------------------------------------------------
# RELO-W0.13b: "migration-with-outstanding-intents" for this table.
#
# The outbox schema is CREATE-TABLE-IF-NOT-EXISTS / additive-only — there is
# no ALTER-based schema migration mechanism today. The only thing every
# caller already does on every connect (open_program_db* re-runs the schema
# SQL) is therefore the closest thing to a "migration" this table has, and
# it must never disturb an outstanding (leased/dispatched) intent. If a real
# ALTER-based migration is ever introduced, this locked assumption — and the
# ratified `relo-w013b-single-host-durability-profile` budget it backs —
# must be revisited.
# ---------------------------------------------------------------------------


def test_schema_is_additive_only_no_alter_statements() -> None:
    assert "ALTER" not in _SCHEMA_SQL.upper()
    assert "CREATE TABLE IF NOT EXISTS" in _SCHEMA_SQL.upper()


def test_reestablishing_schema_does_not_disturb_an_outstanding_intent(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    enqueue(db_path, "op-1", domain="ado_actuation", correlation_id="intent-1", payload_json="{}")
    leased = lease_next(db_path, "ado_actuation", owner="worker-1")
    assert leased is not None and leased.status == "leased"

    # Re-run schema setup against the same file the way every real caller
    # does on every connect (open_program_db_with_retry -> open_program_db
    # -> the module's _ensure_schema). This is the only "migration" this
    # table has today.
    with open_program_db_with_retry(db_path) as connection:
        connection.executescript(_SCHEMA_SQL)
        connection.commit()

    reloaded = load_entry(db_path, "op-1")
    assert reloaded is not None
    assert reloaded.status == leased.status
    assert reloaded.lease_owner == leased.lease_owner
    assert reloaded.lease_fencing_token == leased.lease_fencing_token
    assert reloaded.attempt_count == leased.attempt_count
