"""Unit tests for ADF-W1.3 ``src.core.actuation_outbox`` (Sec 8.11 ADF-F11).

Fault-injection scenarios live in
``tests/contracts/test_outbox_create_fault_matrix.py``; this file covers the
module's smaller building blocks in isolation.
"""

from __future__ import annotations

from pathlib import Path

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


def test_idempotency_key_formula_matches_sec_8_11_3() -> None:
    key = create_task_idempotency_key(program_id="xpf", org="contoso", project="One", operation_intent_id="abc123")
    assert key == "xpf:contoso/One:create_task.v1:new:abc123"


def test_idempotency_key_excludes_approval_event_id() -> None:
    # Sec 8.11.3: the approval event id must NOT influence the key -- two
    # separate approvals of the same intent produce the same key.
    key_a = create_task_idempotency_key(program_id="xpf", org="contoso", project="One", operation_intent_id="abc123")
    key_b = create_task_idempotency_key(program_id="xpf", org="contoso", project="One", operation_intent_id="abc123")
    assert key_a == key_b


def test_outbox_db_path_is_program_scoped(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = outbox_db_path("xpf", programs_root=programs_root)
    assert path == programs_root / "xpf" / "runtime" / "actuation" / "outbox.db"


def test_dispatch_result_defaults_are_a_definite_failure_shape() -> None:
    result = DispatchResult(succeeded=False, failure_reason="boom")
    assert result.remote_id is None
    assert result.uncertain is False


def test_enqueue_create_task_intent_is_idempotent_on_key(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    key = create_task_idempotency_key(program_id="xpf", org="contoso", project="One", operation_intent_id="abc123")
    first = enqueue_create_task_intent(
        program_id="xpf", idempotency_key=key, operation_intent_id="abc123", proposal_id="prop-1",
        payload_json="{}", programs_root=programs_root,
    )
    second = enqueue_create_task_intent(
        program_id="xpf", idempotency_key=key, operation_intent_id="abc123", proposal_id="prop-1",
        payload_json="{}", programs_root=programs_root,
    )
    assert first.outbox_id == second.outbox_id == key
    assert first.status == "pending"
    assert second.status == "pending"


def test_enqueue_sets_domain_and_correlation_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    key = create_task_idempotency_key(program_id="xpf", org="contoso", project="One", operation_intent_id="abc123")
    enqueue_create_task_intent(
        program_id="xpf", idempotency_key=key, operation_intent_id="abc123", proposal_id="prop-1",
        payload_json='{"title": "t"}', programs_root=programs_root,
    )
    entry = load_entry(outbox_db_path("xpf", programs_root=programs_root), key)
    assert entry is not None
    assert entry.domain == ACTUATION_DISPATCH_DOMAIN
    assert entry.correlation_id == key
    assert entry.payload_json == '{"title": "t"}'


def test_dispatch_leased_create_task_happy_path_returns_completed_with_remote_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    key = create_task_idempotency_key(program_id="xpf", org="contoso", project="One", operation_intent_id="abc123")
    enqueue_create_task_intent(
        program_id="xpf", idempotency_key=key, operation_intent_id="abc123", proposal_id="prop-1",
        payload_json="{}", programs_root=programs_root,
    )

    def dispatch_fn(entry) -> DispatchResult:
        return DispatchResult(succeeded=True, remote_id=42, remote_response_hash="deadbeef")

    outcome = dispatch_leased_create_task(
        program_id="xpf", idempotency_key=key, owner="worker-a", dispatch_fn=dispatch_fn, programs_root=programs_root,
    )
    assert isinstance(outcome, ActuationDispatchOutcome)
    assert outcome.status == "completed"
    assert outcome.remote_id == 42
    assert outcome.entry.status == "completed"
    assert outcome.entry.remote_response_hash == "deadbeef"


def test_dispatch_leased_create_task_definite_failure_returns_failed_terminal(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    key = create_task_idempotency_key(program_id="xpf", org="contoso", project="One", operation_intent_id="abc123")
    enqueue_create_task_intent(
        program_id="xpf", idempotency_key=key, operation_intent_id="abc123", proposal_id="prop-1",
        payload_json="{}", programs_root=programs_root,
    )

    def dispatch_fn(entry) -> DispatchResult:
        return DispatchResult(succeeded=False, failure_reason="422 validation error")

    outcome = dispatch_leased_create_task(
        program_id="xpf", idempotency_key=key, owner="worker-a", dispatch_fn=dispatch_fn, programs_root=programs_root,
    )
    assert outcome.status == "failed_terminal"
    assert outcome.remote_id is None
    assert outcome.entry.failure_reason == "422 validation error"


def test_drain_processes_older_backlog_before_own_row(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    old_key = create_task_idempotency_key(program_id="xpf", org="contoso", project="One", operation_intent_id="old")
    enqueue_create_task_intent(
        program_id="xpf", idempotency_key=old_key, operation_intent_id="old", proposal_id="prop-0",
        payload_json="{}", programs_root=programs_root,
    )
    new_key = create_task_idempotency_key(program_id="xpf", org="contoso", project="One", operation_intent_id="new")
    enqueue_create_task_intent(
        program_id="xpf", idempotency_key=new_key, operation_intent_id="new", proposal_id="prop-1",
        payload_json="{}", programs_root=programs_root,
    )

    order: list[str] = []

    def dispatch_fn(entry) -> DispatchResult:
        order.append(entry.outbox_id)
        return DispatchResult(succeeded=True, remote_id=len(order))

    outcome = dispatch_leased_create_task(
        program_id="xpf", idempotency_key=new_key, owner="worker-a", dispatch_fn=dispatch_fn, programs_root=programs_root,
    )
    assert order == [old_key, new_key]
    assert outcome.status == "completed"
