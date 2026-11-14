"""specs/people.md Phase 1, PPL-W1.6: tests for the durable outbox
primitive (src/core/people_registry_outbox.py).

specs/people.md §9.1's own verification bar: "Replay-after-crash produces
no duplicate delivered events; archived programs do not block commit."
"""

from __future__ import annotations

from pathlib import Path

from src.core.people_registry_outbox import (
    SYNTHETIC_EVENT_TYPE,
    drain_registry_outbox_for_program,
    enqueue_registry_outbox_item,
    enqueue_registry_outbox_items,
    pending_registry_outbox_items_for_program,
)


def test_enqueue_creates_one_pending_item_per_program(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"

    entries = enqueue_registry_outbox_items(
        knowledge_root,
        transaction_id="registry-tx-1",
        program_ids=("acme", "fabrikam"),
        payload={"note": "test"},
    )

    assert {entry.domain for entry in entries} == {"acme", "fabrikam"}
    assert all(entry.status == "pending" for entry in entries)
    assert len(pending_registry_outbox_items_for_program(knowledge_root, "acme")) == 1
    assert len(pending_registry_outbox_items_for_program(knowledge_root, "fabrikam")) == 1


def test_enqueue_is_idempotent_for_the_same_transaction_program_pair(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"

    first = enqueue_registry_outbox_item(knowledge_root, transaction_id="registry-tx-1", program_id="acme", payload={"note": "a"})
    second = enqueue_registry_outbox_item(knowledge_root, transaction_id="registry-tx-1", program_id="acme", payload={"note": "b"})

    assert first.outbox_id == second.outbox_id
    assert len(pending_registry_outbox_items_for_program(knowledge_root, "acme")) == 1


def test_enqueue_payload_carries_the_event_type(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"

    entry = enqueue_registry_outbox_item(knowledge_root, transaction_id="registry-tx-1", program_id="acme", payload={"field": "manager_entity_id"})

    assert f'"event_type": "{SYNTHETIC_EVENT_TYPE}"' in entry.payload_json
    assert '"field": "manager_entity_id"' in entry.payload_json


def test_enqueue_for_an_archived_or_nonexistent_program_does_not_block_others(tmp_path: Path) -> None:
    # "archived programs do not block commit" -- enqueue never queries program
    # state at all, so an archived/nonexistent program_id enqueues exactly the
    # same as any other, and does not prevent sibling programs from enqueuing.
    knowledge_root = tmp_path / "knowledge"

    entries = enqueue_registry_outbox_items(
        knowledge_root,
        transaction_id="registry-tx-1",
        program_ids=("archived-program", "active-program"),
        payload={"note": "test"},
    )

    assert len(entries) == 2
    assert {entry.domain for entry in entries} == {"archived-program", "active-program"}


def test_drain_calls_handler_once_per_pending_item_and_marks_completed(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    enqueue_registry_outbox_items(knowledge_root, transaction_id="registry-tx-1", program_ids=("acme",), payload={"note": "a"})
    enqueue_registry_outbox_items(knowledge_root, transaction_id="registry-tx-2", program_ids=("acme",), payload={"note": "b"})
    delivered: list[str] = []

    drained = drain_registry_outbox_for_program(knowledge_root, "acme", owner="drainer-1", handler=lambda entry: delivered.append(entry.outbox_id))

    assert len(drained) == 2
    assert sorted(delivered) == sorted(entry.outbox_id for entry in drained)
    assert all(entry.status == "completed" for entry in drained)
    assert pending_registry_outbox_items_for_program(knowledge_root, "acme") == ()


def test_drain_only_processes_items_for_the_named_program(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    enqueue_registry_outbox_items(knowledge_root, transaction_id="registry-tx-1", program_ids=("acme", "fabrikam"), payload={"note": "a"})
    delivered: list[str] = []

    drain_registry_outbox_for_program(knowledge_root, "acme", owner="drainer-1", handler=lambda entry: delivered.append(entry.outbox_id))

    assert len(delivered) == 1
    assert len(pending_registry_outbox_items_for_program(knowledge_root, "fabrikam")) == 1


def test_replay_after_crash_produces_no_duplicate_delivered_events(tmp_path: Path) -> None:
    # specs/people.md §9.1's exact PPL-W1.6 verification: "Replay-after-crash
    # produces no duplicate delivered events." A "crash" between drains is
    # simulated by simply calling drain again with nothing new enqueued --
    # the program's "next eligible deterministic command" running twice.
    knowledge_root = tmp_path / "knowledge"
    enqueue_registry_outbox_items(knowledge_root, transaction_id="registry-tx-1", program_ids=("acme",), payload={"note": "a"})
    delivered: list[str] = []

    first_drain = drain_registry_outbox_for_program(knowledge_root, "acme", owner="drainer-1", handler=lambda entry: delivered.append(entry.outbox_id))
    second_drain = drain_registry_outbox_for_program(knowledge_root, "acme", owner="drainer-1", handler=lambda entry: delivered.append(entry.outbox_id))

    assert len(first_drain) == 1
    assert second_drain == ()
    assert len(delivered) == 1  # Not redelivered.


def test_drain_with_nothing_pending_is_a_no_op(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"

    drained = drain_registry_outbox_for_program(knowledge_root, "acme", owner="drainer-1", handler=lambda entry: (_ for _ in ()).throw(AssertionError("handler should not be called")))

    assert drained == ()


def test_drain_waits_for_exact_lease_expiry(tmp_path: Path, monkeypatch) -> None:
    from datetime import datetime, timedelta, timezone
    from src.core._db import open_program_db_with_retry
    from src.core.ledger import durable_outbox_store as store
    from src.core.people_registry_outbox import registry_outbox_db_path

    root = tmp_path / "knowledge"
    entry = enqueue_registry_outbox_item(root, transaction_id="tx", program_id="acme", payload={})
    db = registry_outbox_db_path(root)
    with open_program_db_with_retry(db) as connection:
        connection.execute(
            "UPDATE outbox SET status = 'leased', lease_owner = 'old', lease_fencing_token = 1, "
            "lease_expires_at = '2030-01-01T00:00:00.000001Z'",
        )
    before = store.load_entry(db, entry.outbox_id)
    base = datetime(2030, 1, 1, tzinfo=timezone.utc)
    now = [base]
    monkeypatch.setattr(store, "_now", lambda: now[0])
    delivered = []

    assert drain_registry_outbox_for_program(root, "acme", owner="new", handler=delivered.append) == ()
    assert delivered == []
    assert store.load_entry(db, entry.outbox_id) == before

    now[0] = base + timedelta(microseconds=1)
    drained = drain_registry_outbox_for_program(root, "acme", owner="new", handler=delivered.append)
    assert len(drained) == len(delivered) == 1
    assert drained[0].status == "completed"


def test_drain_cannot_complete_successor_claim_with_same_owner(tmp_path: Path, monkeypatch) -> None:
    from contextlib import contextmanager
    import pytest
    from src.core.ledger import durable_outbox_store as store
    from src.core.people_registry_outbox import registry_outbox_db_path

    root = tmp_path / "knowledge"
    entry = enqueue_registry_outbox_item(root, transaction_id="tx", program_id="acme", payload={})
    db = registry_outbox_db_path(root)
    actual_open = store.open_program_db
    takeover_pending = [True]
    successor = []

    @contextmanager
    def reclaim_after_commit(*args, **kwargs):
        with actual_open(*args, **kwargs) as connection:
            yield connection
        if takeover_pending[0] and not kwargs.get("read_only"):
            takeover_pending[0] = False
            with actual_open(db) as connection:
                connection.execute("UPDATE outbox SET lease_expires_at = '2000-01-01T00:00:00Z'")
            successor.append(store.lease_next(db, "acme", owner="worker"))

    monkeypatch.setattr(store, "open_program_db", reclaim_after_commit)
    delivered = []
    with pytest.raises(store.LeaseNoLongerCurrent):
        drain_registry_outbox_for_program(root, "acme", owner="worker", handler=delivered.append)

    assert len(delivered) == 1
    assert delivered[0].lease_fencing_token == 1
    assert successor[0] is not None
    assert successor[0].lease_fencing_token == 2
    assert store.load_entry(db, entry.outbox_id) == successor[0]
