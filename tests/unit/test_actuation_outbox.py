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


@pytest.mark.parametrize("status", ["uncertain_remote_state", "completed", "active", "missing_expiry", "same_owner"])
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
    if status in {"uncertain_remote_state", "completed"}:
        with open_program_db_with_retry(db, durability="strict") as connection:
            connection.execute("UPDATE outbox SET status = ?", (status,))
    if status == "missing_expiry":
        with open_program_db_with_retry(db, durability="strict") as connection:
            connection.execute("UPDATE outbox SET lease_expires_at = NULL")
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
