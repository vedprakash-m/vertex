"""Unit tests for ADF-W1.3 ``src.core.actuation_outbox`` (Sec 8.11 ADF-F11).

Fault-injection scenarios live in
``tests/contracts/test_outbox_create_fault_matrix.py``; this file covers the
module's smaller building blocks in isolation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.actuation_outbox import (
    ActuationDispatchOutcome,
    DispatchResult,
    create_task_idempotency_key,
    dispatch_leased_create_task,
    enqueue_create_task_intent,
    outbox_db_path,
)
from src.core.ledger.durable_outbox_store import load_entry
from src.core.workspace_lease import ACTUATION_DISPATCH_DOMAIN


@pytest.mark.parametrize("changed", ["payload_json", "domain", "correlation_id"])
def test_existing_key_rejects_changed_identity(tmp_path: Path, changed: str) -> None:
    from src.core._db import open_program_db_with_retry
    from src.core.ledger.durable_outbox_store import OutboxError

    root = tmp_path / "programs"
    first = enqueue_create_task_intent(
        program_id="fixture", idempotency_key="key", operation_intent_id="intent",
        proposal_id="proposal", payload_json="{}", programs_root=root,
    )
    db = outbox_db_path("fixture", programs_root=root)
    if changed != "payload_json":
        with open_program_db_with_retry(db, durability="strict") as connection:
            connection.execute(f"UPDATE outbox SET {changed} = ?", ("different",))
    before = load_entry(db, first.outbox_id)
    with pytest.raises(OutboxError):
        enqueue_create_task_intent(
            program_id="fixture", idempotency_key="key", operation_intent_id="intent",
            proposal_id="proposal", payload_json='{"changed":true}' if changed == "payload_json" else "{}",
            programs_root=root,
        )
    assert load_entry(db, first.outbox_id) == before


@pytest.mark.parametrize(
    ("changed_status", "error"),
    [
        ("dispatched", "transition"), ("succeeded", "transition"), ("audited", "transition"),
        ("failed_retryable", "transition"), ("completed", "terminal"), ("dead_letter", "terminal"),
    ],
)
def test_dispatch_refuses_state_changed_after_leasing_before_callback(tmp_path, monkeypatch, changed_status, error) -> None:
    import src.core.actuation_outbox as service
    from src.core._db import open_program_db_with_retry
    from src.core.ledger import durable_outbox_store as store
    from src.core.workspace_lease import read_lease_state

    root = tmp_path / "programs"
    enqueue_create_task_intent(
        program_id="fixture", idempotency_key="key", operation_intent_id="intent",
        proposal_id="proposal", payload_json="{}", programs_root=root,
    )
    db = outbox_db_path("fixture", programs_root=root)
    actual_lease_next = service.lease_next
    changed = []

    def change_after_lease(*args, **kwargs):
        leased = actual_lease_next(*args, **kwargs)
        assert leased is not None
        with open_program_db_with_retry(db, durability="strict") as connection:
            connection.execute(
                "UPDATE outbox SET status = ?, remote_response_hash = 'retained' WHERE outbox_id = 'key'",
                (changed_status,),
            )
        changed.append(store.load_entry(db, "key"))
        return leased

    monkeypatch.setattr(service, "lease_next", change_after_lease)
    with pytest.raises(store.OutboxError, match=error):
        service.dispatch_leased_create_task(
            program_id="fixture", idempotency_key="key", owner="worker",
            dispatch_fn=lambda entry: pytest.fail("Changed state must refuse before callback"),
            programs_root=root,
        )
    assert store.load_entry(db, "key") == changed[0]
    assert read_lease_state("fixture", mutation_domain=ACTUATION_DISPATCH_DOMAIN, programs_root=root) is None


@pytest.mark.parametrize("change", [
    "workspace_takeover", "workspace_same_owner_reacquired", "workspace_missing", "workspace_expired",
    "row_missing", "row_expired", "row_missing_expiry", "row_owner", "row_token", "row_payload", "row_domain", "row_correlation",
])
def test_dispatch_refuses_changed_or_expired_pre_dispatch_fence(tmp_path, monkeypatch, change) -> None:
    from datetime import timedelta
    import src.core.actuation_outbox as service
    import src.core.workspace_lease as workspace
    from src.core._db import open_program_db_with_retry
    from src.core.ledger import durable_outbox_store as store

    root = tmp_path / "programs"
    enqueue_create_task_intent(
        program_id="fixture", idempotency_key="key", operation_intent_id="intent",
        proposal_id="proposal", payload_json='{"original":true}', programs_root=root,
    )
    db = outbox_db_path("fixture", programs_root=root)
    workspace_db = workspace.get_workspace_lease_db_path("fixture", programs_root=root)
    actual_lease_next = service.lease_next
    retained = []
    successor = []
    calls = []
    workspace_after_change = []

    def alter_after_lease(*args, **kwargs):
        leased = actual_lease_next(*args, **kwargs)
        assert leased is not None
        current = workspace.read_lease_state("fixture", mutation_domain=ACTUATION_DISPATCH_DOMAIN, programs_root=root)
        assert current is not None
        if change in {"workspace_takeover", "workspace_same_owner_reacquired"}:
            workspace.release_lease(current, programs_root=root)
            monkeypatch.setattr(workspace, "_now", lambda: current.acquired_at + timedelta(seconds=1))
            next_owner = "worker" if change == "workspace_same_owner_reacquired" else "replacement"
            replacement = workspace.acquire_lease(
                "fixture", next_owner, mutation_domain=ACTUATION_DISPATCH_DOMAIN, programs_root=root,
            )
            assert replacement.fencing_token == current.fencing_token
            successor.append(replacement)
        elif change == "row_missing":
            with open_program_db_with_retry(db, durability="strict") as connection:
                connection.execute("DELETE FROM outbox WHERE outbox_id = 'key'")
        elif change.startswith("workspace_"):
            with open_program_db_with_retry(workspace_db, durability="strict") as connection:
                if change == "workspace_missing":
                    connection.execute("DELETE FROM workspace_lease WHERE mutation_domain = ?", (ACTUATION_DISPATCH_DOMAIN,))
                else:
                    connection.execute(
                        "UPDATE workspace_lease SET expires_at = '2000-01-01T00:00:00Z' WHERE mutation_domain = ?",
                        (ACTUATION_DISPATCH_DOMAIN,),
                    )
        else:
            column, value = {
                "row_expired": ("lease_expires_at", "2000-01-01T00:00:00Z"),
                "row_missing_expiry": ("lease_expires_at", None),
                "row_owner": ("lease_owner", "replacement"),
                "row_token": ("lease_fencing_token", leased.lease_fencing_token + 1),
                "row_payload": ("payload_json", '{"changed":true}'),
                "row_domain": ("domain", "other"),
                "row_correlation": ("correlation_id", "other"),
            }[change]
            with open_program_db_with_retry(db, durability="strict") as connection:
                connection.execute(f"UPDATE outbox SET {column} = ? WHERE outbox_id = 'key'", (value,))
        retained.append(store.load_entry(db, "key"))
        workspace_after_change.append(workspace.read_lease_state(
            "fixture", mutation_domain=ACTUATION_DISPATCH_DOMAIN, programs_root=root,
        ))
        return leased

    def dispatch(entry):
        calls.append(entry.outbox_id)
        return DispatchResult(succeeded=True, remote_id=42)

    monkeypatch.setattr(service, "lease_next", alter_after_lease)
    with pytest.raises(store.OutboxError, match="lease"):
        service.dispatch_leased_create_task(
            program_id="fixture", idempotency_key="key", owner="worker",
            dispatch_fn=dispatch, programs_root=root,
        )
    assert calls == []
    assert store.load_entry(db, "key") == retained[0]
    assert workspace.read_lease_state(
        "fixture", mutation_domain=ACTUATION_DISPATCH_DOMAIN, programs_root=root,
    ) == workspace_after_change[0]
    if successor:
        assert workspace.read_lease_state(
            "fixture", mutation_domain=ACTUATION_DISPATCH_DOMAIN, programs_root=root,
        ) == successor[0]


@pytest.mark.parametrize(
    ("column", "value", "cause"),
    [
        ("lease_expires_at", "invalid", ValueError),
        ("lease_expires_at", b"invalid", TypeError),
        ("lease_fencing_token", float("inf"), OverflowError),
    ],
)
def test_dispatch_refuses_unreadable_changed_row_without_releasing_lease(
    tmp_path, monkeypatch, column, value, cause,
) -> None:
    import src.core.actuation_outbox as service
    import src.core.workspace_lease as workspace
    from src.core._db import open_program_db_with_retry
    from src.core.ledger import durable_outbox_store as store

    root = tmp_path / "programs"
    enqueue_create_task_intent(
        program_id="fixture", idempotency_key="key", operation_intent_id="intent",
        proposal_id="proposal", payload_json="{}", programs_root=root,
    )
    db = outbox_db_path("fixture", programs_root=root)
    actual_lease_next = service.lease_next
    handle = []

    def corrupt_after_lease(*args, **kwargs):
        leased = actual_lease_next(*args, **kwargs)
        handle.append(workspace.read_lease_state(
            "fixture", mutation_domain=ACTUATION_DISPATCH_DOMAIN, programs_root=root,
        ))
        with open_program_db_with_retry(db, durability="strict") as connection:
            connection.execute(f"UPDATE outbox SET {column} = ? WHERE outbox_id = 'key'", (value,))
        return leased

    monkeypatch.setattr(service, "lease_next", corrupt_after_lease)
    with pytest.raises(store.LeaseNoLongerCurrent) as exc_info:
        service.dispatch_leased_create_task(
            program_id="fixture", idempotency_key="key", owner="worker",
            dispatch_fn=lambda entry: pytest.fail("Unreadable evidence must refuse before callback"),
            programs_root=root,
        )
    assert isinstance(exc_info.value.__cause__, cause)
    with open_program_db_with_retry(db, read_only=True) as connection:
        row = connection.execute(f"SELECT status, {column} FROM outbox WHERE outbox_id = 'key'").fetchone()
    assert tuple(row) == ("leased", value)
    assert workspace.read_lease_state(
        "fixture", mutation_domain=ACTUATION_DISPATCH_DOMAIN, programs_root=root,
    ) == handle[0]


def test_fencing_failure_does_not_release_same_owner_successor(tmp_path, monkeypatch) -> None:
    from datetime import timedelta
    import src.core.actuation_outbox as service
    import src.core.workspace_lease as workspace
    from src.core._db import open_program_db_with_retry
    from src.core.ledger import durable_outbox_store as store

    root = tmp_path / "programs"
    enqueue_create_task_intent(
        program_id="fixture", idempotency_key="key", operation_intent_id="intent",
        proposal_id="proposal", payload_json="{}", programs_root=root,
    )
    db = outbox_db_path("fixture", programs_root=root)
    actual_lease_next = service.lease_next
    successor = []

    def replace_before_transition(*args, **kwargs):
        leased = actual_lease_next(*args, **kwargs)
        assert leased is not None
        current = workspace.read_lease_state("fixture", mutation_domain=ACTUATION_DISPATCH_DOMAIN, programs_root=root)
        assert current is not None
        workspace.release_lease(current, programs_root=root)
        monkeypatch.setattr(workspace, "_now", lambda: current.acquired_at + timedelta(seconds=1))
        successor.append(workspace.acquire_lease(
            "fixture", "worker", mutation_domain=ACTUATION_DISPATCH_DOMAIN, programs_root=root,
        ))
        assert successor[0].fencing_token == current.fencing_token
        with open_program_db_with_retry(db, durability="strict") as connection:
            connection.execute(
                "UPDATE outbox SET lease_fencing_token = lease_fencing_token + 1 WHERE outbox_id = 'key'"
            )
        return leased

    monkeypatch.setattr(service, "lease_next", replace_before_transition)
    with pytest.raises(store.LeaseNoLongerCurrent):
        service.dispatch_leased_create_task(
            program_id="fixture", idempotency_key="key", owner="worker",
            dispatch_fn=lambda entry: pytest.fail("A lost lease must not invoke the provider"),
            programs_root=root,
        )
    assert workspace.read_lease_state(
        "fixture", mutation_domain=ACTUATION_DISPATCH_DOMAIN, programs_root=root,
    ) == successor[0]


@pytest.mark.parametrize("change", ["reacquired", "renewed"])
def test_dispatch_does_not_release_changed_same_owner_lease_after_callback(tmp_path, monkeypatch, change) -> None:
    from datetime import timedelta
    import src.core.workspace_lease as workspace

    root = tmp_path / "programs"
    enqueue_create_task_intent(
        program_id="fixture", idempotency_key="key", operation_intent_id="intent",
        proposal_id="proposal", payload_json="{}", programs_root=root,
    )
    successor = []

    def dispatch(entry):
        current = workspace.read_lease_state("fixture", mutation_domain=ACTUATION_DISPATCH_DOMAIN, programs_root=root)
        assert current is not None
        if change == "reacquired":
            workspace.release_lease(current, programs_root=root)
            monkeypatch.setattr(workspace, "_now", lambda: current.acquired_at + timedelta(seconds=1))
            successor.append(workspace.acquire_lease(
                "fixture", "worker", mutation_domain=ACTUATION_DISPATCH_DOMAIN, programs_root=root,
            ))
        else:
            monkeypatch.setattr(workspace, "_now", lambda: current.acquired_at)
            successor.append(workspace.renew_lease(current, ttl_seconds=301, programs_root=root))
        assert successor[0].fencing_token == current.fencing_token
        return DispatchResult(succeeded=True, remote_id=42)

    outcome = dispatch_leased_create_task(
        program_id="fixture", idempotency_key="key", owner="worker", dispatch_fn=dispatch, programs_root=root,
    )
    assert outcome.status == "completed"
    assert workspace.read_lease_state(
        "fixture", mutation_domain=ACTUATION_DISPATCH_DOMAIN, programs_root=root,
    ) == successor[0]


@pytest.mark.parametrize("deadline", ["workspace", "outbox"])
@pytest.mark.parametrize("microseconds_after", [0, 1])
def test_dispatch_samples_expiry_after_transaction_locks(tmp_path, monkeypatch, deadline, microseconds_after) -> None:
    from contextlib import contextmanager
    from datetime import datetime, timedelta, timezone
    import src.core.actuation_outbox as service
    import src.core.workspace_lease as workspace
    from src.core.ledger import durable_outbox_store as store
    from src.core.unit_of_work import open_unit_of_work

    root = tmp_path / "programs"
    enqueue_create_task_intent(
        program_id="fixture", idempotency_key="key", operation_intent_id="intent",
        proposal_id="proposal", payload_json="{}", programs_root=root,
    )
    db = outbox_db_path("fixture", programs_root=root)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(store, "_now", lambda: base)
    monkeypatch.setattr(workspace, "_now", lambda: base)
    actual_lease_next = service.lease_next
    locked = False
    retained = []
    calls = []

    @contextmanager
    def locking(databases):
        nonlocal locked
        with open_unit_of_work(databases) as transaction:
            locked = True
            yield transaction

    def clock():
        assert locked
        return base + timedelta(seconds=2, microseconds=microseconds_after)

    def expire_after_lease(*args, **kwargs):
        leased = actual_lease_next(*args, **kwargs)
        assert leased is not None
        retained.append(leased)
        monkeypatch.setattr(store, "open_unit_of_work", locking)
        monkeypatch.setattr(store, "_now", clock)
        return leased

    def dispatch(entry):
        calls.append(entry.outbox_id)
        return DispatchResult(succeeded=True, remote_id=42)

    monkeypatch.setattr(service, "lease_next", expire_after_lease)
    with pytest.raises(store.OutboxError, match="lease"):
        service.dispatch_leased_create_task(
            program_id="fixture", idempotency_key="key", owner="worker", dispatch_fn=dispatch, programs_root=root,
            workspace_lease_ttl_seconds=2 if deadline == "workspace" else 10,
            outbox_lease_ttl_seconds=2 if deadline == "outbox" else 10,
        )
    assert locked
    assert calls == []
    assert store.load_entry(db, "key") == retained[0]


def test_fence_locks_both_stores_until_dispatch_transition_commits(tmp_path, monkeypatch) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from datetime import timedelta
    from threading import Event
    import src.core.actuation_outbox as service
    import src.core.workspace_lease as workspace
    from src.core._db import open_program_db_with_retry
    from src.core.ledger import durable_outbox_store as store
    from src.core.unit_of_work import open_unit_of_work

    root = tmp_path / "programs"
    enqueue_create_task_intent(
        program_id="fixture", idempotency_key="key", operation_intent_id="intent",
        proposal_id="proposal", payload_json="{}", programs_root=root,
    )
    db = outbox_db_path("fixture", programs_root=root)
    actual_lease_next = service.lease_next
    started = [Event(), Event()]
    finished = [Event(), Event()]
    futures = []

    def compete_for_workspace():
        started[0].set()
        replacement = workspace.acquire_lease(
            "fixture", "replacement", mutation_domain=ACTUATION_DISPATCH_DOMAIN, programs_root=root,
        )
        finished[0].set()
        return replacement

    def compete_for_outbox():
        started[1].set()
        with open_program_db_with_retry(db, durability="strict") as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE outbox SET updated_at = updated_at WHERE outbox_id = 'key'")
        finished[1].set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        @contextmanager
        def locking(databases):
            with open_unit_of_work(databases) as transaction:
                futures.extend((pool.submit(compete_for_workspace), pool.submit(compete_for_outbox)))
                assert all(event.wait(5) for event in started)
                assert not any(event.wait(0.1) for event in finished)
                yield transaction

        def start_competitors_after_lease(*args, **kwargs):
            leased = actual_lease_next(*args, **kwargs)
            current = workspace.read_lease_state("fixture", mutation_domain=ACTUATION_DISPATCH_DOMAIN, programs_root=root)
            assert current is not None
            monkeypatch.setattr(workspace, "_now", lambda: current.expires_at + timedelta(seconds=1))
            monkeypatch.setattr(store, "open_unit_of_work", locking)
            return leased

        def dispatch(entry):
            assert entry.status == "dispatched"
            for future in futures:
                future.result(timeout=10)
            return DispatchResult(succeeded=True, remote_id=42)

        monkeypatch.setattr(service, "lease_next", start_competitors_after_lease)
        outcome = service.dispatch_leased_create_task(
            program_id="fixture", idempotency_key="key", owner="worker", dispatch_fn=dispatch, programs_root=root,
        )
    assert outcome.status == "completed"
    assert all(event.is_set() for event in finished)
    assert workspace.read_lease_state(
        "fixture", mutation_domain=ACTUATION_DISPATCH_DOMAIN, programs_root=root,
    ) == futures[0].result()


@pytest.mark.parametrize("status", [
    "uncertain_remote_state", "completed", "failed_terminal", "compensation_required",
    "dead_letter", "manually_resolved", "succeeded", "audited", "active", "missing_expiry", "naive_expiry", "same_owner",
])
def test_nonclassifiable_dispatch_does_not_acquire_or_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str,
) -> None:
    import src.core.actuation_outbox as service
    from src.core._db import open_program_db_with_retry
    from src.core.ledger import durable_outbox_store as store
    from src.core.workspace_lease import acquire_lease, read_lease_state

    root = tmp_path / "programs"
    enqueue_create_task_intent(
        program_id="fixture", idempotency_key="key", operation_intent_id="intent",
        proposal_id="proposal", payload_json="{}", programs_root=root,
    )
    db = outbox_db_path("fixture", programs_root=root)
    leased = store.lease_next(db, ACTUATION_DISPATCH_DOMAIN, owner="old", ttl_seconds=300 if status == "active" else 0)
    assert leased is not None
    store.mark_dispatched(db, "key", owner="old", fencing_token=leased.lease_fencing_token)
    if status in {
        "uncertain_remote_state", "completed", "failed_terminal", "compensation_required",
        "dead_letter", "manually_resolved", "succeeded", "audited",
    }:
        with open_program_db_with_retry(db, durability="strict") as connection:
            connection.execute("UPDATE outbox SET status = ?", (status,))
    if status == "missing_expiry":
        with open_program_db_with_retry(db, durability="strict") as connection:
            connection.execute("UPDATE outbox SET lease_expires_at = NULL")
    if status == "naive_expiry":
        with open_program_db_with_retry(db, durability="strict") as connection:
            connection.execute("UPDATE outbox SET lease_expires_at = REPLACE(lease_expires_at, 'Z', '')")
    before = store.load_entry(db, "key")
    workspace = acquire_lease("fixture", "workspace-holder", mutation_domain=ACTUATION_DISPATCH_DOMAIN, programs_root=root)

    def forbidden(*args, **kwargs):
        pytest.fail("No lease acquisition, backlog drain or dispatch is permitted")

    monkeypatch.setattr(service, "acquire_lease", forbidden)
    monkeypatch.setattr(service, "lease_next", forbidden)
    for _ in range(2):
        if status == "uncertain_remote_state":
            result = service.dispatch_leased_create_task(
                program_id="fixture", idempotency_key="key", owner="new",
                dispatch_fn=forbidden, programs_root=root,
            )
            assert result.entry == before
            assert result.status == "uncertain_remote_state"
            assert result.remote_id is None
        else:
            with pytest.raises(store.OutboxError):
                service.dispatch_leased_create_task(
                    program_id="fixture", idempotency_key="key",
                    owner="old" if status == "same_owner" else "new",
                    dispatch_fn=forbidden, programs_root=root,
                )
        assert store.load_entry(db, "key") == before
        assert read_lease_state("fixture", mutation_domain=ACTUATION_DISPATCH_DOMAIN, programs_root=root) == workspace


@pytest.mark.parametrize("status", [
        "missing", "failed_terminal", "compensation_required", "dead_letter", "manually_resolved",
        "succeeded", "audited",
])
def test_invalid_requested_intent_cannot_dispatch_pending_backlog(tmp_path: Path, status: str) -> None:
        from src.core._db import open_program_db_with_retry
        from src.core.ledger import durable_outbox_store as store
        from src.core.workspace_lease import read_lease_state

        root = tmp_path / "programs"
        db = outbox_db_path("fixture", programs_root=root)
        backlog = store.enqueue(
            db, "backlog", domain=ACTUATION_DISPATCH_DOMAIN, correlation_id="backlog", payload_json="{}",
        )
        if status != "missing":
            store.enqueue(db, "requested", domain=ACTUATION_DISPATCH_DOMAIN, correlation_id="requested", payload_json="{}")
            with open_program_db_with_retry(db, durability="strict") as connection:
                connection.execute("UPDATE outbox SET status = ? WHERE outbox_id = 'requested'", (status,))
        requested = store.load_entry(db, "requested")
        calls: list[str] = []

        def dispatch(entry) -> DispatchResult:
            calls.append(entry.outbox_id)
            return DispatchResult(succeeded=True, remote_id=42)

        with pytest.raises(store.OutboxError):
            dispatch_leased_create_task(
                program_id="fixture", idempotency_key="requested", owner="worker",
                dispatch_fn=dispatch, programs_root=root,
            )

        assert calls == []
        assert store.load_entry(db, "backlog") == backlog
        assert store.load_entry(db, "requested") == requested
        assert read_lease_state("fixture", mutation_domain=ACTUATION_DISPATCH_DOMAIN, programs_root=root) is None


@pytest.mark.parametrize("identity", ["missing", "domain", "correlation_id"])
def test_dispatch_refuses_missing_or_foreign_requested_row(tmp_path: Path, identity: str) -> None:
    from src.core._db import open_program_db_with_retry
    from src.core.ledger.durable_outbox_store import OutboxError

    root = tmp_path / "programs"
    if identity != "missing":
        enqueue_create_task_intent(
            program_id="fixture", idempotency_key="key", operation_intent_id="intent",
            proposal_id="proposal", payload_json="{}", programs_root=root,
        )
        with open_program_db_with_retry(outbox_db_path("fixture", programs_root=root), durability="strict") as connection:
            connection.execute(f"UPDATE outbox SET {identity} = 'other'")

    def forbidden(entry):
        pytest.fail("No callback for missing or foreign requested row")

    with pytest.raises(OutboxError):
        dispatch_leased_create_task(
            program_id="fixture", idempotency_key="key", owner="new",
            dispatch_fn=forbidden, programs_root=root,
        )


def test_classification_refuses_current_other_workspace_owner(tmp_path: Path) -> None:
    from src.core.ledger import durable_outbox_store as store
    from src.core.workspace_lease import LeaseHeldByAnotherOwner, acquire_lease, read_lease_state

    root = tmp_path / "programs"
    enqueue_create_task_intent(
        program_id="fixture", idempotency_key="key", operation_intent_id="intent",
        proposal_id="proposal", payload_json="{}", programs_root=root,
    )
    db = outbox_db_path("fixture", programs_root=root)
    old = store.lease_next(db, ACTUATION_DISPATCH_DOMAIN, owner="old", ttl_seconds=0)
    assert old is not None
    store.mark_dispatched(db, "key", owner="old", fencing_token=old.lease_fencing_token)
    current = acquire_lease("fixture", "current", mutation_domain=ACTUATION_DISPATCH_DOMAIN, programs_root=root)
    before = store.load_entry(db, "key")

    def forbidden(entry):
        pytest.fail("Current workspace owner must exclude classification and callbacks")

    with pytest.raises(LeaseHeldByAnotherOwner):
        dispatch_leased_create_task(
            program_id="fixture", idempotency_key="key", owner="new",
            dispatch_fn=forbidden, programs_root=root,
        )
    assert store.load_entry(db, "key") == before
    assert read_lease_state("fixture", mutation_domain=ACTUATION_DISPATCH_DOMAIN, programs_root=root) == current


def test_expired_dispatched_returns_uncertainty_before_backlog_drain(tmp_path: Path) -> None:
    from src.core.ledger import durable_outbox_store as store
    from src.core.workspace_lease import read_lease_state

    root = tmp_path / "programs"
    original = enqueue_create_task_intent(
        program_id="fixture", idempotency_key="key", operation_intent_id="intent",
        proposal_id="proposal", payload_json="{}", programs_root=root,
    )
    db = outbox_db_path("fixture", programs_root=root)
    old = store.lease_next(db, ACTUATION_DISPATCH_DOMAIN, owner="old", ttl_seconds=0)
    assert old is not None
    store.mark_dispatched(db, "key", owner="old", fencing_token=old.lease_fencing_token)
    store.enqueue(db, "backlog", domain=ACTUATION_DISPATCH_DOMAIN, correlation_id="backlog", payload_json="{}")

    def forbidden(entry):
        pytest.fail("Classification must return before backlog drain or dispatch")

    result = dispatch_leased_create_task(
        program_id="fixture", idempotency_key="key", owner="new", dispatch_fn=forbidden, programs_root=root,
    )
    assert result.status == result.entry.status == "uncertain_remote_state"
    assert result.remote_id is None
    assert result.entry.payload_json == original.payload_json
    assert store.load_entry(db, "key") == result.entry
    backlog = store.load_entry(db, "backlog")
    assert backlog is not None and backlog.status == "pending"
    assert read_lease_state("fixture", mutation_domain=ACTUATION_DISPATCH_DOMAIN, programs_root=root) is None


def test_stale_classifier_preserves_same_owner_successor_and_backlog(tmp_path, monkeypatch) -> None:
    from datetime import timedelta
    import src.core.actuation_outbox as service
    import src.core.workspace_lease as workspace
    from src.core.ledger import durable_outbox_store as store

    root = tmp_path / "programs"
    db = outbox_db_path("fixture", programs_root=root)
    store.enqueue(db, "key", domain=ACTUATION_DISPATCH_DOMAIN, correlation_id="key", payload_json="{}")
    old = store.lease_next(db, ACTUATION_DISPATCH_DOMAIN, owner="old", ttl_seconds=0)
    assert old is not None
    observed = store.mark_dispatched(db, "key", owner="old", fencing_token=old.lease_fencing_token)
    backlog = store.enqueue(db, "backlog", domain=ACTUATION_DISPATCH_DOMAIN, correlation_id="backlog", payload_json="{}")
    classify = service.classify_expired_dispatched
    successor = []

    def replace_before_classification(db_path, row, *, workspace_lease, programs_root):
        workspace.release_lease(workspace_lease, programs_root=programs_root)
        monkeypatch.setattr(workspace, "_now", lambda: workspace_lease.acquired_at + timedelta(seconds=1))
        successor.append(workspace.acquire_lease(
            "fixture", workspace_lease.owner, mutation_domain=ACTUATION_DISPATCH_DOMAIN, programs_root=programs_root,
        ))
        assert successor[0].fencing_token == workspace_lease.fencing_token
        return classify(db_path, row, workspace_lease=workspace_lease, programs_root=programs_root)

    monkeypatch.setattr(service, "classify_expired_dispatched", replace_before_classification)
    with pytest.raises(store.LeaseNoLongerCurrent):
        service.dispatch_leased_create_task(
            program_id="fixture", idempotency_key="key", owner="new",
            dispatch_fn=lambda entry: pytest.fail("Classification must not drain or dispatch"),
            programs_root=root,
        )
    assert store.load_entry(db, "key") == observed
    assert store.load_entry(db, "backlog") == backlog
    assert workspace.read_lease_state(
        "fixture", mutation_domain=ACTUATION_DISPATCH_DOMAIN, programs_root=root,
    ) == successor[0]


def test_idempotency_key_formula_matches_sec_8_11_3() -> None:
    key = create_task_idempotency_key(program_id="samplealpha", org="contoso", project="One", operation_intent_id="abc123")
    assert key == "samplealpha:contoso/One:create_task.v1:new:abc123"


def test_idempotency_key_excludes_approval_event_id() -> None:
    # Sec 8.11.3: the approval event id must NOT influence the key -- two
    # separate approvals of the same intent produce the same key.
    key_a = create_task_idempotency_key(program_id="samplealpha", org="contoso", project="One", operation_intent_id="abc123")
    key_b = create_task_idempotency_key(program_id="samplealpha", org="contoso", project="One", operation_intent_id="abc123")
    assert key_a == key_b


def test_outbox_db_path_is_program_scoped(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = outbox_db_path("samplealpha", programs_root=programs_root)
    assert path == programs_root / "samplealpha" / "runtime" / "actuation" / "outbox.db"


def test_dispatch_result_defaults_are_a_definite_failure_shape() -> None:
    result = DispatchResult(succeeded=False, failure_reason="boom")
    assert result.remote_id is None
    assert result.uncertain is False


def test_enqueue_create_task_intent_is_idempotent_on_key(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    key = create_task_idempotency_key(program_id="samplealpha", org="contoso", project="One", operation_intent_id="abc123")
    first = enqueue_create_task_intent(
        program_id="samplealpha", idempotency_key=key, operation_intent_id="abc123", proposal_id="prop-1",
        payload_json="{}", programs_root=programs_root,
    )
    second = enqueue_create_task_intent(
        program_id="samplealpha", idempotency_key=key, operation_intent_id="abc123", proposal_id="prop-1",
        payload_json="{}", programs_root=programs_root,
    )
    assert first.outbox_id == second.outbox_id == key
    assert first.status == "pending"
    assert second.status == "pending"


def test_enqueue_sets_domain_and_correlation_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    key = create_task_idempotency_key(program_id="samplealpha", org="contoso", project="One", operation_intent_id="abc123")
    enqueue_create_task_intent(
        program_id="samplealpha", idempotency_key=key, operation_intent_id="abc123", proposal_id="prop-1",
        payload_json='{"title": "t"}', programs_root=programs_root,
    )
    entry = load_entry(outbox_db_path("samplealpha", programs_root=programs_root), key)
    assert entry is not None
    assert entry.domain == ACTUATION_DISPATCH_DOMAIN
    assert entry.correlation_id == key
    assert entry.payload_json == '{"title": "t"}'


def test_dispatch_leased_create_task_happy_path_returns_completed_with_remote_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    key = create_task_idempotency_key(program_id="samplealpha", org="contoso", project="One", operation_intent_id="abc123")
    enqueue_create_task_intent(
        program_id="samplealpha", idempotency_key=key, operation_intent_id="abc123", proposal_id="prop-1",
        payload_json="{}", programs_root=programs_root,
    )

    def dispatch_fn(entry) -> DispatchResult:
        return DispatchResult(succeeded=True, remote_id=42, remote_response_hash="deadbeef")

    outcome = dispatch_leased_create_task(
        program_id="samplealpha", idempotency_key=key, owner="worker-a", dispatch_fn=dispatch_fn, programs_root=programs_root,
    )
    assert isinstance(outcome, ActuationDispatchOutcome)
    assert outcome.status == "completed"
    assert outcome.remote_id == 42
    assert outcome.entry.status == "completed"
    assert outcome.entry.remote_response_hash == "deadbeef"


def test_dispatch_leased_create_task_definite_failure_returns_failed_terminal(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    key = create_task_idempotency_key(program_id="samplealpha", org="contoso", project="One", operation_intent_id="abc123")
    enqueue_create_task_intent(
        program_id="samplealpha", idempotency_key=key, operation_intent_id="abc123", proposal_id="prop-1",
        payload_json="{}", programs_root=programs_root,
    )

    def dispatch_fn(entry) -> DispatchResult:
        return DispatchResult(succeeded=False, failure_reason="422 validation error")

    outcome = dispatch_leased_create_task(
        program_id="samplealpha", idempotency_key=key, owner="worker-a", dispatch_fn=dispatch_fn, programs_root=programs_root,
    )
    assert outcome.status == "failed_terminal"
    assert outcome.remote_id is None
    assert outcome.entry.failure_reason == "422 validation error"


def test_drain_processes_older_backlog_before_own_row(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    old_key = create_task_idempotency_key(program_id="samplealpha", org="contoso", project="One", operation_intent_id="old")
    enqueue_create_task_intent(
        program_id="samplealpha", idempotency_key=old_key, operation_intent_id="old", proposal_id="prop-0",
        payload_json="{}", programs_root=programs_root,
    )
    new_key = create_task_idempotency_key(program_id="samplealpha", org="contoso", project="One", operation_intent_id="new")
    enqueue_create_task_intent(
        program_id="samplealpha", idempotency_key=new_key, operation_intent_id="new", proposal_id="prop-1",
        payload_json="{}", programs_root=programs_root,
    )

    order: list[str] = []

    def dispatch_fn(entry) -> DispatchResult:
        order.append(entry.outbox_id)
        return DispatchResult(succeeded=True, remote_id=len(order))

    outcome = dispatch_leased_create_task(
        program_id="samplealpha", idempotency_key=new_key, owner="worker-a", dispatch_fn=dispatch_fn, programs_root=programs_root,
    )
    assert order == [old_key, new_key]
    assert outcome.status == "completed"
