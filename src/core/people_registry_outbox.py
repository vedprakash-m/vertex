"""specs/people.md Phase 1, PPL-W1.6: durable outbox primitive.

Reuses `src/core/ledger/durable_outbox_store.py`'s generic lease/attempt/
dead-letter engine (arch-fix.md CPK) directly rather than reimplementing
outbox mechanics -- that module is explicitly documented as table-agnostic
("Any caller that needs outbox semantics ... can open one of these
against its own SQLite file"). This module is the registry-specific
wiring on top of it: DB path, per-program `domain` partitioning,
idempotent enqueue, and a drain/replay helper.

§7.6: "The registry commit writes one idempotent outbox item per
affected active program; that program's next eligible deterministic
command drains it before projection-dependent reads. Archived programs
do not block registry commits."

Which programs count as "affected" and "active" -- and the exact
archive-marker field name -- are explicitly NOT this feature's to invent
(§8.4's binding decision: "this feature does not unilaterally define
platform lifecycle semantics," defers the exact field name to a
cross-subsystem decision record). This module is therefore agnostic to
program lifecycle state entirely: callers decide which `program_id`s to
enqueue for, and `enqueue_registry_outbox_item` never queries program
state, blocks, or errors regardless of whether that program is real,
active, or archived -- "archived programs do not block commit" holds by
construction, not by an explicit archive check.

Proven here with a synthetic event type (`SYNTHETIC_EVENT_TYPE`); real
event registration (`identity.lifecycle_changed`, `ownership.changed`,
`team.membership.changed`, §7.6) is Phase 6's scope. At the time this
module was written, it was not yet wired into any real commit path --
doing so needed a real "affected active programs" resolution that didn't
exist until real person/team/membership schemas (Phase 2a) gave the
registry something to compute program-affiliation from. (Correction,
PPL-W6.1: the real multi-file publication path every actual mutation
uses is `commit_registry_files_transaction`, not
`commit_registry_transaction` -- the latter is Phase 1's original
synthetic-schema-only transaction function and has no real callers left;
PPL-W6.1 wires `team.membership.changed` into `people_registry_writer.py::_commit`,
the single funnel every real membership mutation already passes through,
rather than into either transaction primitive directly.)
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

from src.core.ledger.durable_outbox_store import (
    OutboxEntry,
    enqueue as _engine_enqueue,
    lease_next as _engine_lease_next,
    list_by_status,
    load_entry,
    mark_completed,
)

_OUTBOX_DB_FILENAME = "registry_outbox.sqlite3"

#: PPL-W1.6's proof event type. Real event types are registered in Phase 6.
SYNTHETIC_EVENT_TYPE = "registry.synthetic_event"


def registry_outbox_db_path(knowledge_root: Path) -> Path:
    return knowledge_root / ".state" / _OUTBOX_DB_FILENAME


def _outbox_item_id(transaction_id: str, program_id: str) -> str:
    return f"{transaction_id}:{program_id}"


def enqueue_registry_outbox_item(
    knowledge_root: Path,
    *,
    transaction_id: str,
    program_id: str,
    payload: dict,
    event_type: str = SYNTHETIC_EVENT_TYPE,
) -> OutboxEntry:
    """Idempotent: enqueueing the same (transaction_id, program_id) pair
    more than once returns the EXISTING row rather than raising or
    duplicating -- §7.6's "one idempotent outbox item per affected
    active program"."""
    db_path = registry_outbox_db_path(knowledge_root)
    outbox_id = _outbox_item_id(transaction_id, program_id)
    existing = load_entry(db_path, outbox_id)
    if existing is not None:
        return existing
    payload_json = json.dumps({"event_type": event_type, **payload}, sort_keys=True)
    try:
        return _engine_enqueue(db_path, outbox_id, domain=program_id, correlation_id=transaction_id, payload_json=payload_json)
    except sqlite3.IntegrityError:
        # A concurrent enqueue for the same pair won the race -- idempotent by construction.
        existing = load_entry(db_path, outbox_id)
        assert existing is not None
        return existing


def enqueue_registry_outbox_items(
    knowledge_root: Path,
    *,
    transaction_id: str,
    program_ids: tuple[str, ...],
    payload: dict,
    event_type: str = SYNTHETIC_EVENT_TYPE,
) -> tuple[OutboxEntry, ...]:
    """One call per affected program; a program that does not exist or is
    archived enqueues exactly the same as any other -- see module
    docstring. Never partially fails: if one enqueue somehow raised, the
    programs already enqueued before it are not rolled back (each row is
    its own independent commit in the underlying engine)."""
    return tuple(
        enqueue_registry_outbox_item(knowledge_root, transaction_id=transaction_id, program_id=program_id, payload=payload, event_type=event_type)
        for program_id in program_ids
    )


def pending_registry_outbox_items_for_program(knowledge_root: Path, program_id: str) -> tuple[OutboxEntry, ...]:
    return list_by_status(registry_outbox_db_path(knowledge_root), program_id, "pending")


def drain_registry_outbox_for_program(
    knowledge_root: Path,
    program_id: str,
    *,
    owner: str,
    handler: Callable[[OutboxEntry], None],
) -> tuple[OutboxEntry, ...]:
    """"That program's next eligible deterministic command drains it
    before projection-dependent reads." Leases and processes every
    currently-eligible item for `program_id` (pending, due-retryable, or
    orphaned by an expired lease) via the generic engine's `lease_next`,
    calling `handler(entry)` once per item and marking it `completed` on
    success.

    Replay-safe for the common crash case: `lease_next` never re-selects
    an already-`completed` row, so calling this again after a crash that
    happened before any item was leased (or while one was leased but the
    process died before `handler` ran) redelivers nothing already
    finished. Like the underlying engine (documented as "at-least-once /
    effectively-once"), a crash strictly between `handler` succeeding and
    `mark_completed` running can still redeliver that one item once its
    lease expires -- `handler` should be idempotent per `entry.outbox_id`
    if that residual risk matters to the caller.
    """
    db_path = registry_outbox_db_path(knowledge_root)
    drained: list[OutboxEntry] = []
    while True:
        entry = _engine_lease_next(db_path, program_id, owner=owner)
        if entry is None:
            break
        handler(entry)
        completed = mark_completed(db_path, entry.outbox_id, owner=owner, fencing_token=entry.lease_fencing_token)
        drained.append(completed)
    return tuple(drained)
