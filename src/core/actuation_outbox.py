"""ADF-W1.3 (arch-data-fix.md Sec 8.11 ADF-F11): outbox-backed create-task dispatch.

Wires the generic, table-agnostic engine in ``src.core.ledger.durable_outbox_store``
to the create-task actuation surface with its own domain and its own
per-program SQLite file (Sec 0.5 warns against reusing REV's
``projection_outbox`` -- a different bounded context). This module owns:

- Sec 8.11.3's idempotency-key formula for ``create_task``;
- idempotent enqueue (a retried approval for the same
  ``operation_intent_id`` reuses the already-enqueued row rather than
  raising on the primary-key clash -- INV-ADF-9);
- Sec 8.11.6's workspace-lease wrapping around dispatch (the
  ``actuation_dispatch`` mutation domain from Appendix A.11), draining any
  older backlog for the domain before a fresh row (crash-recovery order);
- classifying a dispatch attempt's outcome into the durable-receipt chain
  (Sec 8.11.2: leased dispatch -> remote correlation -> durable receipt ->
  audit receipt -> completed), emitting the two ``actuation.*`` ledger
  events registered in ``event_types.py``.

The outbox row's own ``payload_json`` carries everything a dispatcher needs
to complete the create -- not just what one in-process closure captured --
so a *different* process that reclaims a stale lease (worker crash mid-way)
can still finish the same intent correctly. ``dispatch_fn`` therefore
receives the leased ``OutboxEntry`` itself, not the caller's original
proposal entry.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src.core.exceptions import CredentialExpired
from src.core.journal import PROGRAMS_ROOT
from src.core.ledger.durable_outbox_store import (
    OutboxEntry,
    OutboxError,
    enqueue,
    lease_next,
    load_entry,
    mark_audited,
    mark_completed,
    mark_dispatched,
    mark_failed_terminal,
    mark_succeeded,
    mark_uncertain_remote_state,
)
from src.core.workspace_lease import ACTUATION_DISPATCH_DOMAIN, acquire_lease, release_lease

_MAX_DRAIN_ITERATIONS = 50


def outbox_db_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "runtime" / "actuation" / "outbox.db"


def create_task_idempotency_key(*, program_id: str, org: str, project: str, operation_intent_id: str) -> str:
    """Sec 8.11.3 formula: ``program_id : tenant/provider : operation_type+version
    : target_identity : operation_intent_id``. ``create_task`` has no
    pre-existing target identity (the work item does not exist yet); the
    ``operation_intent_id`` is what makes re-approval idempotent (Appendix
    B.8), so ``target_identity`` is the fixed literal ``"new"``. The
    approval-event id is deliberately NOT part of the key (Sec 8.11.3)."""
    return f"{program_id}:{org}/{project}:create_task.v1:new:{operation_intent_id}"


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Outcome of one ``dispatch_fn`` call.

    ``uncertain=True`` means the provider call's outcome could not be
    determined (fault-injection item 3/4: after dispatch before response, or
    remote success with a lost response) -- the row goes to
    ``uncertain_remote_state`` and requires an operator to call
    ``mark_manually_resolved`` (no automatic retry, per INV-ADF-9).
    """

    succeeded: bool
    remote_id: int | None = None
    remote_response_hash: str | None = None
    uncertain: bool = False
    failure_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ActuationDispatchOutcome:
    entry: OutboxEntry
    remote_id: int | None
    status: str  # "completed" | "uncertain_remote_state" | "failed_terminal"


def enqueue_create_task_intent(
    *,
    program_id: str,
    idempotency_key: str,
    operation_intent_id: str,
    proposal_id: str,
    payload_json: str,
    programs_root: Path = PROGRAMS_ROOT,
) -> OutboxEntry:
    """Idempotent: fault-injection item 1 (crash after enqueue, before
    lease). A caller that re-derives the same ``idempotency_key`` (the
    normal crash-retry path, since ``operation_intent_id`` is persisted to
    the proposal manifest before the first dispatch attempt) gets back the
    already-enqueued row instead of a duplicate. Concurrent enqueue of the
    *same* key from two processes is prevented upstream by the proposal
    manifest's own file lock, not by this function.
    """
    db_path = outbox_db_path(program_id, programs_root=programs_root)
    existing = load_entry(db_path, idempotency_key)
    if existing is not None:
        return existing
    entry = enqueue(
        db_path,
        idempotency_key,
        domain=ACTUATION_DISPATCH_DOMAIN,
        correlation_id=idempotency_key,
        payload_json=payload_json,
    )
    _emit_intent_created_best_effort(
        program_id=program_id,
        operation_intent_id=operation_intent_id,
        idempotency_key=idempotency_key,
        proposal_id=proposal_id,
        programs_root=programs_root,
    )
    return entry


def dispatch_leased_create_task(
    *,
    program_id: str,
    idempotency_key: str,
    owner: str,
    dispatch_fn: Callable[[OutboxEntry], DispatchResult],
    programs_root: Path = PROGRAMS_ROOT,
    workspace_lease_ttl_seconds: int = 300,
    outbox_lease_ttl_seconds: int = 120,
    max_drain_iterations: int = _MAX_DRAIN_ITERATIONS,
) -> ActuationDispatchOutcome:
    """Sec 8.11.6: acquire the program-level workspace lease for the
    ``actuation_dispatch`` domain, then drain the outbox for that domain --
    oldest row first, so a crashed worker's stale-leased or still-pending
    rows are finished before this call's own row (fault-injection item 2:
    crash after lease, before dispatch). Raises ``LeaseHeldByAnotherOwner``
    if another owner currently holds the workspace lease (propagated to the
    caller rather than silently retried).
    """
    db_path = outbox_db_path(program_id, programs_root=programs_root)
    lease_handle = acquire_lease(
        program_id,
        owner,
        mutation_domain=ACTUATION_DISPATCH_DOMAIN,
        ttl_seconds=workspace_lease_ttl_seconds,
        programs_root=programs_root,
    )
    try:
        outcome: ActuationDispatchOutcome | None = None
        for _ in range(max_drain_iterations):
            leased = lease_next(db_path, ACTUATION_DISPATCH_DOMAIN, owner=owner, ttl_seconds=outbox_lease_ttl_seconds)
            if leased is None:
                break
            drained = _drive_one(
                db_path,
                leased,
                owner=owner,
                program_id=program_id,
                dispatch_fn=dispatch_fn,
                programs_root=programs_root,
            )
            if leased.outbox_id == idempotency_key:
                outcome = drained
                break
        else:
            raise OutboxError(
                f"actuation dispatch drain exceeded {max_drain_iterations} iterations for domain "
                f"{ACTUATION_DISPATCH_DOMAIN!r} in program {program_id!r}; investigate outbox backlog."
            )
        if outcome is None:
            raise OutboxError(
                f"actuation dispatch drain finished without processing outbox row {idempotency_key!r}; "
                "the row was not found pending/leaseable under this call's workspace lease."
            )
        return outcome
    finally:
        release_lease(lease_handle, programs_root=programs_root)


def _drive_one(
    db_path: Path,
    leased: OutboxEntry,
    *,
    owner: str,
    program_id: str,
    dispatch_fn: Callable[[OutboxEntry], DispatchResult],
    programs_root: Path,
) -> ActuationDispatchOutcome:
    fencing = leased.lease_fencing_token
    try:
        payload_fields = json.loads(leased.payload_json)
    except Exception:
        payload_fields = {}
    operation_intent_id = str(payload_fields.get("operation_intent_id") or leased.correlation_id)
    org = str(payload_fields.get("org") or "")
    project = str(payload_fields.get("project") or "")

    dispatched = mark_dispatched(db_path, leased.outbox_id, owner=owner, fencing_token=fencing)

    # Fault-injection item 3 ("after dispatch before response"): a 401/429/4xx
    # is a DEFINITE outcome (the provider responded; dispatch_fn already
    # turns those into DispatchResult(succeeded=False)). Anything that
    # escapes dispatch_fn as an exception -- timeout, connection reset,
    # unexpected error -- means we cannot tell whether the provider received
    # and processed the request, so it must go to uncertain_remote_state
    # (operator-resolved terminal), never be silently retried.
    try:
        result = dispatch_fn(dispatched)
    except CredentialExpired as error:
        entry = mark_failed_terminal(db_path, leased.outbox_id, owner=owner, fencing_token=fencing, reason=f"credential expired: {error}"[:500])
        _emit_receipt_recorded_best_effort(
            program_id=program_id, operation_intent_id=operation_intent_id, receipt_state="failed_terminal",
            remote_id=None, org=org, project=project, provider_summary={"reason": "credential_expired"},
            programs_root=programs_root,
        )
        return ActuationDispatchOutcome(entry=entry, remote_id=None, status="failed_terminal")
    except Exception as error:
        reason = f"ambiguous dispatch failure ({type(error).__name__}): {error}"[:500]
        entry = mark_uncertain_remote_state(
            db_path, leased.outbox_id, owner=owner, fencing_token=fencing, reason=reason,
        )
        _emit_receipt_recorded_best_effort(
            program_id=program_id, operation_intent_id=operation_intent_id, receipt_state="uncertain_remote_state",
            remote_id=None, org=org, project=project, provider_summary={"error": str(error)[:200]},
            programs_root=programs_root,
        )
        _emit_uncertain_remote_state_alert_best_effort(
            program_id=program_id, outbox_id=leased.outbox_id, operation_intent_id=operation_intent_id,
            reason=reason, programs_root=programs_root,
        )
        return ActuationDispatchOutcome(entry=entry, remote_id=None, status="uncertain_remote_state")

    if result.uncertain:
        reason = result.failure_reason or "dispatch outcome uncertain"
        entry = mark_uncertain_remote_state(
            db_path, leased.outbox_id, owner=owner, fencing_token=fencing, reason=reason,
        )
        _emit_receipt_recorded_best_effort(
            program_id=program_id, operation_intent_id=operation_intent_id, receipt_state="uncertain_remote_state",
            remote_id=result.remote_id, org=org, project=project,
            provider_summary={"reason": result.failure_reason or ""}, programs_root=programs_root,
        )
        _emit_uncertain_remote_state_alert_best_effort(
            program_id=program_id, outbox_id=leased.outbox_id, operation_intent_id=operation_intent_id,
            reason=reason, programs_root=programs_root,
        )
        return ActuationDispatchOutcome(entry=entry, remote_id=None, status="uncertain_remote_state")

    if not result.succeeded:
        entry = mark_failed_terminal(
            db_path, leased.outbox_id, owner=owner, fencing_token=fencing,
            reason=result.failure_reason or "dispatch failed",
        )
        _emit_receipt_recorded_best_effort(
            program_id=program_id, operation_intent_id=operation_intent_id, receipt_state="failed_terminal",
            remote_id=None, org=org, project=project,
            provider_summary={"reason": result.failure_reason or ""}, programs_root=programs_root,
        )
        return ActuationDispatchOutcome(entry=entry, remote_id=None, status="failed_terminal")

    succeeded_entry = mark_succeeded(
        db_path, leased.outbox_id, owner=owner, fencing_token=fencing,
        remote_response_hash=result.remote_response_hash,
    )
    # Fault-injection item 5 ("after response before receipt commit"): the
    # ledger receipt event is best-effort, mirroring ado_writer's existing
    # _emit_duplicate_prevented precedent -- the outbox row's own
    # succeeded/completed transition (already durably committed above, in
    # SQLite) is the source of truth for "did we dispatch"; a failed local
    # ledger append must never leave a successfully-dispatched row stuck.
    _emit_receipt_recorded_best_effort(
        program_id=program_id, operation_intent_id=operation_intent_id, receipt_state="succeeded",
        remote_id=result.remote_id, org=org, project=project,
        provider_summary={"remote_id": str(result.remote_id)}, programs_root=programs_root,
    )
    del succeeded_entry
    mark_audited(db_path, leased.outbox_id, owner=owner, fencing_token=fencing)
    completed_entry = mark_completed(db_path, leased.outbox_id, owner=owner, fencing_token=fencing)
    return ActuationDispatchOutcome(entry=completed_entry, remote_id=result.remote_id, status="completed")


def _emit_intent_created_best_effort(
    *,
    program_id: str,
    operation_intent_id: str,
    idempotency_key: str,
    proposal_id: str,
    programs_root: Path,
) -> None:
    try:
        from src.core.ledger.event_log import ConfidenceTier, TemporalConfidence, build_event_envelope, write_event
        from src.core.ledger.source_refs import OperatorAssertionRef

        now = datetime.now(timezone.utc)
        envelope = build_event_envelope(
            program_id=program_id,
            event_type="actuation.intent_created.v1",
            occurred_at=now,
            recorded_at=now,
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="ado_writer",
            payload={
                "operation_intent_id": operation_intent_id,
                "idempotency_key": idempotency_key,
                "operation_type": "create_task.v1",
                "target_identity": "new",
                "proposal_id": proposal_id,
                # No standalone immutable "approval event" is emitted yet
                # (Sec 8.11.2 lists it as a separate chain step); the
                # proposal manifest is today's durable approval record.
                "approval_event_ref": f"proposal:{proposal_id}",
            },
            source_ref=OperatorAssertionRef(
                asserted_by="system:ado_writer",
                asserted_at=now,
                context=f"outbox enqueue for proposal {proposal_id}",
            ),
        )
        write_event(envelope, programs_root=programs_root)
    except Exception:  # pragma: no cover - audit write must never break enqueue
        pass


def _emit_receipt_recorded_best_effort(
    *,
    program_id: str,
    operation_intent_id: str,
    receipt_state: str,
    remote_id: int | None,
    org: str,
    project: str,
    provider_summary: dict[str, object],
    programs_root: Path,
) -> None:
    try:
        from src.core.ledger.event_log import ConfidenceTier, TemporalConfidence, build_event_envelope, write_event
        from src.core.ledger.source_refs import ADOWorkItemRef, OperatorAssertionRef

        now = datetime.now(timezone.utc)
        source_ref = (
            ADOWorkItemRef(org=org, project=project, work_item_id=remote_id)
            if remote_id is not None and org and project
            else OperatorAssertionRef(asserted_by="system:ado_writer", asserted_at=now)
        )
        payload: dict[str, object] = {
            "operation_intent_id": operation_intent_id,
            "receipt_state": receipt_state,
            "provider_summary": provider_summary,
        }
        if remote_id is not None:
            payload["remote_id"] = str(remote_id)
        envelope = build_event_envelope(
            program_id=program_id,
            event_type="actuation.receipt_recorded.v1",
            occurred_at=now,
            recorded_at=now,
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="ado_writer",
            payload=payload,
            source_ref=source_ref,
        )
        write_event(envelope, programs_root=programs_root)
    except Exception:  # pragma: no cover - audit write must never break dispatch
        pass


def _emit_uncertain_remote_state_alert_best_effort(
    *, program_id: str, outbox_id: str, operation_intent_id: str, reason: str, programs_root: Path,
) -> None:
    """ADF-W5.8 (Section 8.2.5): "outbox dead letter or uncertain remote
    state" is one of the ten named alert categories. Entity-scoped on the
    outbox row itself so repeated ambiguous dispatches for the SAME row
    correlate to one alert (cooldown-suppressed), not a flood of distinct
    rows. Best-effort -- an alert-store failure must never break dispatch,
    matching `_emit_receipt_recorded_best_effort`'s own contract."""
    try:
        from src.core.alerts import AlertSeverity, append_or_suppress_alert

        append_or_suppress_alert(
            program_id=program_id,
            category="outbox_uncertain_remote_state",
            entity_type="outbox_entry",
            entity_id=outbox_id,
            severity=AlertSeverity.ERROR,
            message=f"Actuation outbox entry {outbox_id!r} (intent {operation_intent_id!r}) is in an "
            f"uncertain remote state: {reason}",
            next_command="vertex ledger outbox --show",
            programs_root=programs_root,
        )
    except Exception:  # pragma: no cover - alert write must never break dispatch
        pass


__all__ = [
    "ActuationDispatchOutcome",
    "DispatchResult",
    "create_task_idempotency_key",
    "dispatch_leased_create_task",
    "enqueue_create_task_intent",
    "outbox_db_path",
]
