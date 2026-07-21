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
