"""ADF-W1.3 and RELO-W5: durable, capability-bound ADO actuation dispatch.

Wires the generic, table-agnostic engine in ``src.core.ledger.durable_outbox_store``
to qualified ADO actuation operations with their own domain and
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

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src.core.actuation_approval_store import (
    ActuationAdmissionRequest,
    ActuationApproval,
    ActuationPreflightObservation,
    ApprovalRevocationResult,
    create_actuation_approval as _create_actuation_approval,
    enqueue_approved_actuation_intent as _enqueue_approved_actuation_intent,
    mark_qualified_dispatched_with_workspace_lease,
    revoke_actuation_approval as _revoke_actuation_approval,
    verify_qualified_admission,
)
from src.core.actuation_reconciliation import (
    AuthorizedEffect,
    EffectReconciliation,
    ReconciliationDisposition,
    ReconciliationObservation,
    audience_manifest_hash,
    desired_effect_hash,
    reconcile_effect,
    reconcile_uncertain_effect,
    require_offline_compensation_operation,
)
from src.core.exceptions import CredentialExpired
from src.core.journal import PROGRAMS_ROOT
from src.core.ledger.durable_outbox_store import (
    LeaseNoLongerCurrent,
    OutboxEntry,
    OutboxError,
    bind_effect,
    classify_expired_dispatched,
    enqueue,
    enqueue_with_effect,
    has_effect_binding,
    lease_next,
    load_effect_outbox_id,
    load_entry,
    load_success_receipt,
    mark_audited,
    mark_completed,
    mark_compensation_required,
    mark_dispatched_with_workspace_lease,
    mark_failed_terminal,
    mark_succeeded,
    mark_uncertain_remote_state,
)
from src.core.operation_capabilities import (
    OPERATION_CAPABILITIES,
    provider_request_key,
)
from src.core.operator_identity import OperatorIdentity
from src.core.workspace_lease import (
    ACTUATION_DISPATCH_DOMAIN,
    LeaseHandle,
    acquire_lease,
    release_lease,
)

_MAX_DRAIN_ITERATIONS = 50


def outbox_db_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "runtime" / "actuation" / "outbox.db"


def actuation_idempotency_key(
    *, program_id: str, org: str, project: str, operation: str, target_identity: str, operation_intent_id: str,
) -> str:
    """Sec 8.11.3 formula: ``program_id : tenant/provider : operation_type+version
    : target_identity : operation_intent_id``."""
    return f"{program_id}:{org}/{project}:{operation}:{target_identity}:{operation_intent_id}"


def create_task_idempotency_key(*, program_id: str, org: str, project: str, operation_intent_id: str) -> str:
    """``create_task`` has no
    pre-existing target identity (the work item does not exist yet); the
    ``operation_intent_id`` is what makes re-approval idempotent (Appendix
    B.8), so ``target_identity`` is the fixed literal ``"new"``. The
    approval-event id is deliberately NOT part of the key (Sec 8.11.3)."""
    return actuation_idempotency_key(
        program_id=program_id, org=org, project=project,
        operation="create_task.v1", target_identity="new", operation_intent_id=operation_intent_id,
    )


# Only capabilities carrying the explicit live flag may leave the outbox.
DISPATCHABLE_OPERATIONS = frozenset(
    operation
    for operation, capability in OPERATION_CAPABILITIES.items()
    if capability.dispatchable
)

_EFFECT_STATES = {
    "pending": "intent", "leased": "intent", "failed_retryable": "intent",
    "dispatched": "dispatched",
    "succeeded": "confirmed", "audited": "confirmed", "completed": "confirmed",
    "uncertain_remote_state": "uncertain",
}


def effect_id_v1(*, operation: str, target: str, payload: dict[str, object], approval_id: str, audience: str) -> str:
    """Hash the semantic effect while retaining the historical signature."""
    canonical = json.dumps(
        {"operation": operation, "target": target, "payload": payload},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def require_dispatchable_operation(operation: str) -> None:
    if operation not in DISPATCHABLE_OPERATIONS:
        raise OutboxError(f"Operation {operation!r} is draft/manual only; it cannot be dispatched")


def create_actuation_approval(
    programs_root: Path,
    *,
    request: ActuationAdmissionRequest,
    approver: OperatorIdentity,
    approved_at: datetime,
    expires_at: datetime,
    approval_id: str | None = None,
) -> ActuationApproval:
    return _create_actuation_approval(
        outbox_db_path(request.program_id, programs_root=programs_root),
        request=request,
        approver=approver,
        approved_at=approved_at,
        expires_at=expires_at,
        approval_id=approval_id,
    )


def enqueue_approved_actuation_intent(
    programs_root: Path,
    *,
    request: ActuationAdmissionRequest,
    approval: ActuationApproval,
    now: datetime,
) -> OutboxEntry:
    outbox_id = provider_request_key(
        program_id=request.program_id,
        org=request.tenant,
        project=request.project,
        operation=request.operation,
        target_identity=request.target_identity,
        operation_intent_id=request.intent_id,
    )
    return _enqueue_approved_actuation_intent(
        outbox_db_path(request.program_id, programs_root=programs_root),
        outbox_id=outbox_id,
        request=request,
        approval=approval,
        now=now,
    )


def revoke_actuation_approval(
    programs_root: Path,
    *,
    program_id: str,
    approval_id: str,
    intent_id: str,
    revision: int,
    revoked_by: OperatorIdentity,
    reason: str,
    now: datetime,
) -> ApprovalRevocationResult:
    return _revoke_actuation_approval(
        outbox_db_path(program_id, programs_root=programs_root),
        approval_id=approval_id,
        intent_id=intent_id,
        revision=revision,
        revoked_by=revoked_by,
        reason=reason,
        now=now,
    )


def _require_dispatchable_payload(payload: object) -> None:
    if not isinstance(payload, dict) or "operation" not in payload:
        return
    operation = payload["operation"]
    if not isinstance(operation, str):
        raise OutboxError("Operation is draft/manual only; it must have a versioned capability")
    require_dispatchable_operation(operation)


def _retained_payload(entry: OutboxEntry) -> dict[str, object]:
    try:
        payload = json.loads(entry.payload_json)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _require_retained_dispatchable_operation(
    entry: OutboxEntry,
    *,
    db_path: Path,
    program_id: str,
    require_effect_binding: bool = True,
) -> dict[str, object]:
    payload = _retained_payload(entry)
    operation = payload.get("operation", "create_task.v1")
    if not isinstance(operation, str) or not operation:
        raise OutboxError("Operation is draft/manual only; it must have a versioned capability")
    require_dispatchable_operation(operation)
    semantic_effect_id = (
        _semantic_effect_id(
            program_id=program_id,
            operation=operation,
            target_identity=str(payload.get("target_identity") or "new"),
            fallback_causal_occurrence_id=str(payload["operation_intent_id"]),
            payload_json=entry.payload_json,
        )
        if isinstance(payload.get("operation_intent_id"), str)
        and payload["operation_intent_id"]
        else None
    )
    semantic_owner = (
        load_effect_outbox_id(db_path, semantic_effect_id)
        if semantic_effect_id is not None
        else None
    )
    if (
        require_effect_binding
        and semantic_effect_id is not None
        and (
            (
                semantic_owner is None
                and not has_effect_binding(db_path, entry.outbox_id)
            )
            or semantic_owner not in {None, entry.outbox_id}
        )
    ):
        raise OutboxError(
            "Semantic effect is not bound to this retained outbox row "
            "(first binding won or the row is unbound)"
        )
    return payload


def effect_state(entry: OutboxEntry) -> str:
    """Map an outbox status to its effect state: intent, dispatched, confirmed or uncertain."""
    return _EFFECT_STATES.get(entry.status, entry.status)


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Outcome of one ``dispatch_fn`` call.

    ``uncertain=True`` means the provider call's outcome could not be
    determined (fault-injection item 3/4: after dispatch before response, or
    remote success with a lost response) -- the row goes to
    ``uncertain_remote_state`` and requires an operator to call
    ``mark_manually_resolved`` (no automatic retry, per INV-ADF-9).
    ``compensation_required=True`` is admitted only for operations whose
    registered capability declares compensation support.
    """

    succeeded: bool
    remote_id: int | None = None
    remote_response_hash: str | None = None
    uncertain: bool = False
    failure_reason: str | None = None
    remote_rev: int | None = None
    compensation_required: bool = False
    reconciliation: EffectReconciliation | None = None


@dataclass(frozen=True, slots=True)
class ActuationDispatchOutcome:
    entry: OutboxEntry
    remote_id: int | None
    status: str
    remote_rev: int | None = None
    reconciliation: EffectReconciliation | None = None


def enqueue_create_task_intent(
    *,
    program_id: str,
    idempotency_key: str,
    operation_intent_id: str,
    proposal_id: str,
    payload_json: str,
    programs_root: Path = PROGRAMS_ROOT,
    effect_id: str | None = None,
    operation: str = "create_task.v1",
    target_identity: str = "new",
) -> OutboxEntry:
    """Idempotent: fault-injection item 1 (crash after enqueue, before
    lease). A caller that re-derives the same ``idempotency_key`` (the
    normal crash-retry path, since ``operation_intent_id`` is persisted to
    the proposal manifest before the first dispatch attempt) gets back the
    already-enqueued row instead of a duplicate. Concurrent enqueue of the
    *same* key from two processes is prevented upstream by the proposal
    manifest's own file lock, not by this function.

    When ``effect_id`` is given and already bound to an outbox row, that row
    is returned and no second effect is enqueued.
    """
    db_path = outbox_db_path(program_id, programs_root=programs_root)
    if effect_id is not None:
        entry, created = enqueue_with_effect(
            db_path,
            idempotency_key,
            domain=ACTUATION_DISPATCH_DOMAIN,
            correlation_id=idempotency_key,
            payload_json=payload_json,
            effect_id=effect_id,
        )
        if created:
            _emit_intent_created_best_effort(
                program_id=program_id,
                operation_intent_id=operation_intent_id,
                idempotency_key=idempotency_key,
                proposal_id=proposal_id,
                programs_root=programs_root,
                operation=operation,
                target_identity=target_identity,
            )
        return entry
    semantic_effect_id = _semantic_effect_id(
        program_id=program_id,
        operation=operation,
        target_identity=target_identity,
        fallback_causal_occurrence_id=operation_intent_id,
        payload_json=payload_json,
    )
    if semantic_effect_id is not None:
        entry, created = enqueue_with_effect(
            db_path,
            idempotency_key,
            domain=ACTUATION_DISPATCH_DOMAIN,
            correlation_id=idempotency_key,
            payload_json=payload_json,
            effect_id=semantic_effect_id,
        )
        if created:
            _emit_intent_created_best_effort(
                program_id=program_id,
                operation_intent_id=operation_intent_id,
                idempotency_key=idempotency_key,
                proposal_id=proposal_id,
                programs_root=programs_root,
                operation=operation,
                target_identity=target_identity,
            )
        return entry
    existing = load_entry(db_path, idempotency_key)
    if existing is not None:
        if (
            existing.domain != ACTUATION_DISPATCH_DOMAIN
            or existing.correlation_id != idempotency_key
            or existing.payload_json != payload_json
        ):
            raise OutboxError("Existing outbox key has different domain, correlation or payload")
        return existing
    entry = enqueue(
        db_path,
        idempotency_key,
        domain=ACTUATION_DISPATCH_DOMAIN,
        correlation_id=idempotency_key,
        payload_json=payload_json,
        effect_id=effect_id,
    )
    if entry.outbox_id != idempotency_key:
        return entry
    _emit_intent_created_best_effort(
        program_id=program_id,
        operation_intent_id=operation_intent_id,
        idempotency_key=idempotency_key,
        proposal_id=proposal_id,
        programs_root=programs_root,
        operation=operation,
        target_identity=target_identity,
    )
    return entry


def _semantic_effect_id(
    *,
    program_id: str,
    operation: str,
    target_identity: str,
    fallback_causal_occurrence_id: str,
    payload_json: str,
) -> str | None:
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        from src.core.operation_capabilities import (
            normalize_semantic_payload,
            semantic_effect_key,
        )

        return semantic_effect_key(
            program_id=program_id,
            provider=str(payload.get("provider") or "ado"),
            tenant=str(payload.get("org") or "unknown-tenant"),
            project=str(payload.get("project") or "unknown-project"),
            operation=operation,
            target_identity=target_identity,
            causal_occurrence_id=str(
                payload.get("causal_occurrence_id")
                or payload.get("operation_intent_id")
                or fallback_causal_occurrence_id
            ),
            payload=normalize_semantic_payload(operation, payload),
        )
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class _QualifiedDispatchContext:
    preflight_fn: Callable[[OutboxEntry], ActuationPreflightObservation]
    approval_policy_version: str
    scope_enabled: bool
    rule_enabled: bool
    now: datetime


def dispatch_approved_actuation_intent(
    *,
    program_id: str,
    outbox_id: str,
    owner: str,
    dispatch_fn: Callable[[OutboxEntry], DispatchResult],
    preflight_fn: Callable[[OutboxEntry], ActuationPreflightObservation],
    programs_root: Path = PROGRAMS_ROOT,
    approval_policy_version: str,
    scope_enabled: bool,
    rule_enabled: bool,
    now: datetime,
    workspace_lease_ttl_seconds: int = 300,
    outbox_lease_ttl_seconds: int = 120,
    max_drain_iterations: int = _MAX_DRAIN_ITERATIONS,
) -> ActuationDispatchOutcome:
    db_path = outbox_db_path(program_id, programs_root=programs_root)
    observed = load_entry(db_path, outbox_id)
    if observed is None:
        raise OutboxError("Requested outbox row does not exist")
    try:
        observation = preflight_fn(observed)
    except Exception as error:
        raise OutboxError(
            f"Qualified admission preflight failed before leasing: {error}"
        ) from error
    admission = verify_qualified_admission(
        db_path,
        outbox_id=outbox_id,
        observation=observation,
        approval_policy_version=approval_policy_version,
        scope_enabled=scope_enabled,
        rule_enabled=rule_enabled,
        now=now,
    )
    if not admission.allowed:
        return ActuationDispatchOutcome(
            entry=admission.entry,
            remote_id=None,
            status=admission.entry.status,
        )
    return dispatch_leased_create_task(
        program_id=program_id,
        idempotency_key=outbox_id,
        owner=owner,
        dispatch_fn=dispatch_fn,
        programs_root=programs_root,
        workspace_lease_ttl_seconds=workspace_lease_ttl_seconds,
        outbox_lease_ttl_seconds=outbox_lease_ttl_seconds,
        max_drain_iterations=max_drain_iterations,
        _qualified_context=_QualifiedDispatchContext(
            preflight_fn=preflight_fn,
            approval_policy_version=approval_policy_version,
            scope_enabled=scope_enabled,
            rule_enabled=rule_enabled,
            now=now,
        ),
    )


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
    _qualified_context: _QualifiedDispatchContext | None = None,
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
    observed = load_entry(db_path, idempotency_key)
    if observed is None:
        raise OutboxError("Requested outbox row does not exist")
    if observed.domain != ACTUATION_DISPATCH_DOMAIN or observed.correlation_id != idempotency_key:
        raise OutboxError("Existing outbox key has different domain or correlation")
    _require_retained_dispatchable_operation(
        observed,
        db_path=db_path,
        program_id=program_id,
        require_effect_binding=observed.status != "dispatched",
    )
    if observed.status == "uncertain_remote_state":
        return ActuationDispatchOutcome(entry=observed, remote_id=None, status="uncertain_remote_state")
    if observed.status == "completed":
        raise OutboxError("Completed outbox row cannot be dispatched again")
    if observed.status not in {
        "pending",
        "failed_retryable",
        "leased",
        "dispatched",
        "succeeded",
        "audited",
    }:
        raise OutboxError(f"Requested outbox row is not dispatchable ({observed.status!r})")
    resume_receipt: dict[str, object] | None = None
    resume_receipt_json: str | None = None
    if observed.status in {"succeeded", "audited"}:
        resume_receipt, resume_receipt_json = _load_valid_success_receipt(
            db_path,
            observed,
            program_id=program_id,
        )
    try:
        observed_payload = json.loads(observed.payload_json)
    except (json.JSONDecodeError, TypeError):
        observed_payload = {}
    _require_dispatchable_payload(observed_payload)
    if observed.status == "dispatched" and (
        observed.lease_expires_at is None
        or observed.lease_expires_at.utcoffset() is None
        or observed.lease_expires_at > datetime.now(timezone.utc)
        or observed.lease_owner == owner
    ):
        raise OutboxError("Dispatched outbox row requires an expired recorded lease and a different owner")
    if observed.status in {"succeeded", "audited"} and (
        observed.lease_expires_at is None
        or observed.lease_expires_at.utcoffset() is None
        or observed.lease_owner == owner
        or observed.lease_expires_at > datetime.now(timezone.utc)
    ):
        raise OutboxError(
            "Successful outbox row requires an expired recorded lease and a different owner"
        )
    lease_handle = acquire_lease(
        program_id,
        owner,
        mutation_domain=ACTUATION_DISPATCH_DOMAIN,
        ttl_seconds=workspace_lease_ttl_seconds,
        programs_root=programs_root,
    )
    fence_lost = False
    try:
        if observed.status in {"succeeded", "audited"}:
            assert resume_receipt is not None and resume_receipt_json is not None
            return _resume_success(
                db_path,
                observed,
                receipt=resume_receipt,
                receipt_json=resume_receipt_json,
                workspace_lease=lease_handle,
                programs_root=programs_root,
            )
        if observed.status == "dispatched":
            classified = classify_expired_dispatched(
                db_path, observed, workspace_lease=lease_handle, programs_root=programs_root,
            )
            return ActuationDispatchOutcome(entry=classified, remote_id=None, status="uncertain_remote_state")
        outcome: ActuationDispatchOutcome | None = None
        for _ in range(max_drain_iterations):
            leased = lease_next(
                db_path,
                ACTUATION_DISPATCH_DOMAIN,
                owner=owner,
                ttl_seconds=outbox_lease_ttl_seconds,
            )
            if leased is None:
                break
            try:
                drained = _drive_one(
                    db_path,
                    leased,
                    owner=owner,
                    program_id=program_id,
                    dispatch_fn=dispatch_fn,
                    programs_root=programs_root,
                    workspace_lease=lease_handle,
                    qualified_context=_qualified_context,
                )
            except OutboxError as error:
                if leased.outbox_id == idempotency_key:
                    raise
                mark_failed_terminal(
                    db_path,
                    leased.outbox_id,
                    owner=owner,
                    fencing_token=leased.lease_fencing_token,
                    reason=str(error)[:500],
                    workspace_lease=lease_handle,
                    programs_root=programs_root,
                )
                continue
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
    except LeaseNoLongerCurrent:
        fence_lost = True
        raise
    finally:
        if not fence_lost:
            release_lease(lease_handle, programs_root=programs_root, require_unchanged=True)


def _drive_one(
    db_path: Path,
    leased: OutboxEntry,
    *,
    owner: str,
    program_id: str,
    dispatch_fn: Callable[[OutboxEntry], DispatchResult],
    programs_root: Path,
    workspace_lease: LeaseHandle,
    qualified_context: _QualifiedDispatchContext | None = None,
) -> ActuationDispatchOutcome:
    fencing = leased.lease_fencing_token
    try:
        payload_fields = json.loads(leased.payload_json)
    except Exception:
        payload_fields = None
    metadata = payload_fields if isinstance(payload_fields, dict) else {}
    operation_intent_id = str(
        metadata.get("operation_intent_id") or leased.correlation_id
    )
    org = str(metadata.get("org") or "")
    project = str(metadata.get("project") or "")

    if qualified_context is None:
        dispatched = mark_dispatched_with_workspace_lease(
            db_path,
            leased,
            workspace_lease=workspace_lease,
            programs_root=programs_root,
        )
    else:
        try:
            observation = qualified_context.preflight_fn(leased)
        except Exception as error:
            raise OutboxError(
                f"Qualified admission preflight failed after leasing: {error}"
            ) from error
        admission = mark_qualified_dispatched_with_workspace_lease(
            db_path,
            leased,
            workspace_lease=workspace_lease,
            observation=observation,
            approval_policy_version=qualified_context.approval_policy_version,
            scope_enabled=qualified_context.scope_enabled,
            rule_enabled=qualified_context.rule_enabled,
            programs_root=programs_root,
            now=qualified_context.now,
        )
        if not admission.allowed:
            return ActuationDispatchOutcome(
                entry=admission.entry,
                remote_id=None,
                status=admission.entry.status,
            )
        dispatched = admission.entry

    if not isinstance(payload_fields, dict):
        entry = mark_failed_terminal(
            db_path,
            leased.outbox_id,
            owner=owner,
            fencing_token=fencing,
            reason="Outbox payload is not a JSON object; provider dispatch refused.",
            workspace_lease=workspace_lease,
            programs_root=programs_root,
        )
        return ActuationDispatchOutcome(
            entry=entry, remote_id=None, status="failed_terminal"
        )
    try:
        _require_dispatchable_payload(payload_fields)
        operation = payload_fields.get("operation")
        require_dispatchable_operation(operation if isinstance(operation, str) else "")
        authorized_effect = _load_authorized_effect(
            payload_fields, program_id=program_id
        )
    except (OutboxError, ValueError) as error:
        entry = mark_failed_terminal(
            db_path,
            leased.outbox_id,
            owner=owner,
            fencing_token=fencing,
            reason=str(error),
            workspace_lease=workspace_lease,
            programs_root=programs_root,
        )
        _emit_receipt_recorded_best_effort(
            program_id=program_id,
            operation_intent_id=operation_intent_id,
            receipt_state="failed_terminal",
            remote_id=None,
            org=org,
            project=project,
            provider_summary={"reason": "unqualified_operation"},
            programs_root=programs_root,
        )
        return ActuationDispatchOutcome(
            entry=entry, remote_id=None, status="failed_terminal"
        )

    # Fault-injection item 3 ("after dispatch before response"): definite
    # rejections become failed results; 408/5xx/redirect or malformed-response
    # uncertainty is classified by dispatch_fn. Anything that
    # escapes dispatch_fn as an exception -- timeout, connection reset,
    # unexpected error -- means we cannot tell whether the provider received
    # and processed the request, so it must go to uncertain_remote_state
    # (operator-resolved terminal), never be silently retried.
    try:
        result = dispatch_fn(dispatched)
    except CredentialExpired as error:
        entry = mark_failed_terminal(
            db_path, leased.outbox_id, owner=owner, fencing_token=fencing,
            reason=f"credential expired: {error}"[:500],
            workspace_lease=workspace_lease, programs_root=programs_root,
        )
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
            workspace_lease=workspace_lease, programs_root=programs_root,
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

    if result.succeeded and not result.uncertain and (type(result.remote_id) is not int or result.remote_id <= 0):
        result = replace(
            result, succeeded=False, uncertain=True, remote_id=None,
            failure_reason="Provider success lacks a valid remote work-item ID; manual reconciliation required.",
        )
    if result.succeeded and not result.uncertain and result.remote_rev is not None and (
        type(result.remote_rev) is not int or result.remote_rev <= 0
    ):
        result = replace(
            result, succeeded=False, uncertain=True,
            failure_reason="Provider success contains an invalid remote revision; manual reconciliation required.",
        )
    if result.compensation_required:
        try:
            compensation_operation = require_offline_compensation_operation(
                str(payload_fields.get("operation") or "create_task.v1")
            )
        except OutboxError as error:
            result = replace(
                result, succeeded=False, uncertain=True, compensation_required=False,
                failure_reason=result.failure_reason or str(error),
            )
            compensation_operation = None
        if not result.succeeded or result.uncertain:
            result = replace(
                result, succeeded=False, uncertain=True, compensation_required=False,
                failure_reason=result.failure_reason
                or "Provider effect requires resolution; operation is not compensable.",
            )
            compensation_operation = None
    else:
        compensation_operation = None

    if result.uncertain:
        reason = result.failure_reason or "dispatch outcome uncertain"
        entry = mark_uncertain_remote_state(
            db_path, leased.outbox_id, owner=owner, fencing_token=fencing, reason=reason,
            workspace_lease=workspace_lease, programs_root=programs_root,
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
            workspace_lease=workspace_lease, programs_root=programs_root,
        )
        _emit_receipt_recorded_best_effort(
            program_id=program_id, operation_intent_id=operation_intent_id, receipt_state="failed_terminal",
            remote_id=None, org=org, project=project,
            provider_summary={"reason": result.failure_reason or ""}, programs_root=programs_root,
        )
        return ActuationDispatchOutcome(entry=entry, remote_id=None, status="failed_terminal")

    if (
        result.reconciliation is None
        or not result.reconciliation.completed
        or not _reconciliation_binds_effect(
            authorized_effect,
            result.reconciliation,
            remote_id=result.remote_id if type(result.remote_id) is int else 0,
            remote_rev=result.remote_rev,
        )
        or any(
            receipt.resulting_revision != result.remote_rev
            for receipt in result.reconciliation.receipts
        )
    ):
        reason = "post-dispatch reconciliation did not certify the authorized effect"
        if result.reconciliation is not None and result.reconciliation.mismatches:
            reason += ": " + "; ".join(result.reconciliation.mismatches[:3])
        entry = mark_uncertain_remote_state(
            db_path,
            leased.outbox_id,
            owner=owner,
            fencing_token=fencing,
            reason=reason[:500],
            reconciliation_receipt_json=(
                json.dumps(result.reconciliation.to_dict(), sort_keys=True, separators=(",", ":"))
                if result.reconciliation is not None
                else json.dumps({"status": "uncertain_remote_state", "receipts": [], "mismatches": ["provider evidence missing"]})
            ),
            workspace_lease=workspace_lease,
            programs_root=programs_root,
        )
        _emit_receipt_recorded_best_effort(
            program_id=program_id, operation_intent_id=operation_intent_id,
            receipt_state="uncertain_remote_state", remote_id=result.remote_id,
            org=org, project=project,
            provider_summary={"reason": "reconciliation_incomplete"},
            programs_root=programs_root,
        )
        _emit_uncertain_remote_state_alert_best_effort(
            program_id=program_id, outbox_id=leased.outbox_id,
            operation_intent_id=operation_intent_id, reason=reason, programs_root=programs_root,
        )
        return ActuationDispatchOutcome(
            entry=entry, remote_id=None, status="uncertain_remote_state",
            reconciliation=result.reconciliation,
        )

    receipt_json = json.dumps({
        "schema_version": "1",
        "program_id": program_id,
        "outbox_id": leased.outbox_id,
        "operation": payload_fields.get("operation", "create_task.v1"),
        "operation_intent_id": payload_fields.get("operation_intent_id"),
        "proposal_id": payload_fields.get("proposal_id"),
        "org": payload_fields.get("org"),
        "project": payload_fields.get("project"),
        "payload_sha256": hashlib.sha256(leased.payload_json.encode("utf-8")).hexdigest(),
        "remote_id": result.remote_id,
        "remote_rev": result.remote_rev,
        "remote_response_hash": result.remote_response_hash,
        "compensation_required": result.compensation_required,
        "acknowledged_at": datetime.now(timezone.utc).isoformat(),
        "attempt_count": dispatched.attempt_count,
        "fencing_token": fencing,
        **(
            {"reconciliation": result.reconciliation.to_dict()}
            if result.reconciliation is not None
            else {}
        ),
    }, sort_keys=True, separators=(",", ":"))
    mark_succeeded(
        db_path, leased.outbox_id, owner=owner, fencing_token=fencing,
        remote_response_hash=result.remote_response_hash, success_receipt_json=receipt_json,
        workspace_lease=workspace_lease, programs_root=programs_root,
    )
    # The acknowledgement receipt and success are atomic; the secondary
    # ledger projection remains best effort.
    _emit_receipt_recorded_best_effort(
        program_id=program_id, operation_intent_id=operation_intent_id,
        receipt_state="compensation_required" if result.compensation_required else "succeeded",
        remote_id=result.remote_id, org=org, project=project,
        provider_summary={"remote_id": str(result.remote_id)}, programs_root=programs_root,
    )
    retained_receipt = load_success_receipt(db_path, leased.outbox_id)
    if retained_receipt != receipt_json:
        raise OutboxError("Create success receipt is missing or changed; manual reconciliation required")
    if result.compensation_required:
        assert compensation_operation is not None
        compensated_entry = mark_compensation_required(
            db_path, leased.outbox_id, owner=owner, fencing_token=fencing,
            reason=result.failure_reason or "provider effect requires compensation",
            compensation_operation=compensation_operation,
            workspace_lease=workspace_lease, programs_root=programs_root,
        )
        receipt = json.loads(retained_receipt)
        return ActuationDispatchOutcome(
            entry=compensated_entry, remote_id=receipt["remote_id"], status="compensation_required",
            remote_rev=receipt["remote_rev"],
            reconciliation=result.reconciliation,
        )
    mark_audited(
        db_path, leased.outbox_id, owner=owner, fencing_token=fencing,
        workspace_lease=workspace_lease, programs_root=programs_root,
    )
    completed_entry = mark_completed(
        db_path, leased.outbox_id, owner=owner, fencing_token=fencing,
        workspace_lease=workspace_lease, programs_root=programs_root,
    )
    receipt = json.loads(retained_receipt)
    return ActuationDispatchOutcome(
        entry=completed_entry, remote_id=receipt["remote_id"], status="completed",
        remote_rev=receipt["remote_rev"],
        reconciliation=result.reconciliation,
    )


def _resume_success(
    db_path: Path,
    observed: OutboxEntry,
    *,
    receipt: dict[str, object],
    receipt_json: str,
    workspace_lease: LeaseHandle | None = None,
    programs_root: Path | None = None,
) -> ActuationDispatchOutcome:
    remote_id = receipt["remote_id"]
    remote_rev = receipt["remote_rev"]
    assert type(remote_id) is int
    retained = load_entry(db_path, observed.outbox_id)
    if (
        retained != observed
        or load_success_receipt(db_path, observed.outbox_id) != receipt_json
    ):
        raise OutboxError("Create success evidence changed before local completion")
    owner = observed.lease_owner
    if owner is None:
        raise OutboxError("Create success evidence has no retained lease owner")
    retained = observed
    if retained.status == "succeeded":
        retained = mark_audited(
            db_path,
            observed.outbox_id,
            owner=owner,
            fencing_token=observed.lease_fencing_token,
            workspace_lease=workspace_lease,
            programs_root=programs_root,
        )
    if receipt.get("compensation_required") is True:
        payload_fields = json.loads(observed.payload_json)
        try:
            compensation_operation = require_offline_compensation_operation(
                str(payload_fields.get("operation") or "")
            )
        except OutboxError as error:
            raise OutboxError(
                "Retained compensation request has no approved compensation operation"
            ) from error
        retained = mark_compensation_required(
            db_path,
            retained.outbox_id,
            owner=owner,
            fencing_token=retained.lease_fencing_token,
            reason="Retained provider effect requires compensation",
            compensation_operation=compensation_operation,
            workspace_lease=workspace_lease,
            programs_root=programs_root,
        )
        return ActuationDispatchOutcome(
            entry=retained,
            remote_id=remote_id,
            status="compensation_required",
            remote_rev=remote_rev if type(remote_rev) is int else None,
        )
    completed = mark_completed(
        db_path,
        retained.outbox_id,
        owner=owner,
        fencing_token=retained.lease_fencing_token,
        workspace_lease=workspace_lease,
        programs_root=programs_root,
    )
    return ActuationDispatchOutcome(
        entry=completed,
        remote_id=remote_id,
        status="completed",
        remote_rev=remote_rev if type(remote_rev) is int else None,
    )


def _load_valid_success_receipt(
    db_path: Path,
    observed: OutboxEntry,
    *,
    program_id: str,
) -> tuple[dict[str, object], str]:
    try:
        payload_fields = json.loads(observed.payload_json)
        receipt_json = load_success_receipt(db_path, observed.outbox_id)
        if receipt_json is None:
            raise OutboxError(
                "Create success receipt is missing; manual reconciliation required"
            )
        receipt = json.loads(receipt_json)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise OutboxError(
            "Create success receipt or payload is invalid; manual reconciliation required"
        ) from error
    if not isinstance(payload_fields, dict) or not isinstance(receipt, dict):
        raise OutboxError(
            "Create success receipt or payload is invalid; manual reconciliation required"
        )
    expected = {
        "schema_version": "1",
        "program_id": program_id,
        "outbox_id": observed.outbox_id,
        "operation": payload_fields.get("operation", "create_task.v1"),
        "operation_intent_id": payload_fields.get("operation_intent_id"),
        "proposal_id": payload_fields.get("proposal_id"),
        "org": payload_fields.get("org"),
        "project": payload_fields.get("project"),
        "payload_sha256": hashlib.sha256(
            observed.payload_json.encode("utf-8")
        ).hexdigest(),
        "remote_response_hash": observed.remote_response_hash,
        "attempt_count": observed.attempt_count,
        "fencing_token": observed.lease_fencing_token,
    }
    required_fields = set(expected) | {
        "remote_id",
        "remote_rev",
        "acknowledged_at",
        "compensation_required",
        "reconciliation",
    }
    remote_id = receipt.get("remote_id")
    remote_rev = receipt.get("remote_rev")
    acknowledged_at = receipt.get("acknowledged_at")
    try:
        acknowledged = (
            datetime.fromisoformat(acknowledged_at.replace("Z", "+00:00"))
            if isinstance(acknowledged_at, str)
            else None
        )
    except (OverflowError, ValueError):
        acknowledged = None
    if (
        not required_fields.issubset(receipt)
        or any(
            type(receipt[field]) is not type(value) or receipt[field] != value
            for field, value in expected.items()
        )
        or type(remote_id) is not int
        or remote_id <= 0
        or (
            remote_rev is not None
            and (type(remote_rev) is not int or remote_rev <= 0)
        )
        or acknowledged is None
        or acknowledged.utcoffset() is None
        or type(receipt.get("compensation_required")) is not bool
        or not isinstance(receipt.get("reconciliation"), dict)
    ):
        raise OutboxError(
            "Create success receipt is invalid or no longer matches the retained intent"
        )
    authorized = _load_authorized_effect(
        payload_fields,
        program_id=program_id,
    )
    if not _receipt_reconciliation_binds(
        receipt=receipt,
        authorized=authorized,
        remote_id=remote_id,
        remote_rev=remote_rev,
    ):
        raise OutboxError(
            "Create success receipt does not certify the retained authorized effect"
        )
    return receipt, receipt_json


def _receipt_reconciliation_binds(
    *,
    receipt: dict[str, object],
    authorized: AuthorizedEffect,
    remote_id: int,
    remote_rev: int | None,
) -> bool:
    reconciliation = receipt.get("reconciliation")
    if not isinstance(reconciliation, dict):
        return False
    if (
        reconciliation.get("status") != "completed"
        or reconciliation.get("completed") is not True
        or reconciliation.get("mismatches") != []
    ):
        return False
    child_receipts = reconciliation.get("receipts")
    recipients: list[str | None] = list(authorized.intended_recipients) or [None]
    if not isinstance(child_receipts, list) or len(child_receipts) != len(recipients):
        return False
    expected_effect_hash = desired_effect_hash(authorized.desired_value)
    expected_manifest_hash = audience_manifest_hash(authorized.audience_manifest)
    for child, recipient in zip(child_receipts, recipients):
        if not isinstance(child, dict):
            return False
        if (
            child.get("recipient") != recipient
            or child.get("terminal_status") != "completed"
            or child.get("intent_id") != authorized.intent_id
            or child.get("approval_id") != authorized.approval_id
            or child.get("provider") != authorized.provider
            or child.get("tenant_id") != authorized.tenant_id
            or child.get("project") != authorized.project
            or child.get("operation") != authorized.operation
            or child.get("target_identity") != authorized.target_identity
            or child.get("remote_effect_id") != str(remote_id)
            or child.get("resulting_revision") != remote_rev
            or child.get("desired_effect_hash") != expected_effect_hash
            or child.get("preflight_revision") != authorized.preflight_revision
            or child.get("execution_principal") != authorized.execution_principal
            or child.get("audience_manifest_hash") != expected_manifest_hash
            or child.get("authorized_audience_recipients")
            != list(authorized.audience_manifest.included_person_entity_ids)
            or child.get("evidence_version") != authorized.evidence_version
            or child.get("policy_version") != authorized.policy_version
            or child.get("rollback_mechanism") != authorized.rollback_mechanism
            or child.get("observed_value_hash") != expected_effect_hash
            or type(child.get("attempt_count")) is not int
            or int(child["attempt_count"]) <= 0
            or not isinstance(child.get("provider_request_id"), str)
            or not str(child["provider_request_id"]).strip()
            or not isinstance(child.get("provider_correlation_id"), str)
            or not str(child["provider_correlation_id"]).strip()
            or not isinstance(child.get("response_hash"), str)
            or not str(child["response_hash"]).strip()
            or type(child.get("resulting_revision")) is not int
            or int(child["resulting_revision"]) <= 0
        ):
            return False
    return True


def _bind_effect_or_raise(db_path: Path, effect_id: str | None, outbox_id: str) -> None:
    if effect_id is not None and bind_effect(db_path, effect_id, outbox_id) != outbox_id:
        raise OutboxError("Effect id is already bound to a different outbox row")


def _load_authorized_effect(
    payload_fields: dict[str, object],
    *,
    program_id: str,
) -> AuthorizedEffect:
    raw = payload_fields.get("authorized_effect")
    if not isinstance(raw, dict):
        raise OutboxError("outbox payload has no authorized effect binding")
    authorized = AuthorizedEffect.from_dict(raw)
    if authorized.program_id != program_id:
        raise OutboxError("authorized effect program does not match the outbox row")
    if authorized.operation != payload_fields.get("operation"):
        raise OutboxError("authorized effect operation does not match the outbox row")
    if authorized.intent_id != payload_fields.get("operation_intent_id"):
        raise OutboxError("authorized effect intent does not match the outbox row")
    if authorized.tenant_id != payload_fields.get("org"):
        raise OutboxError("authorized effect tenant does not match the outbox row")
    if authorized.project != payload_fields.get("project"):
        raise OutboxError("authorized effect project does not match the outbox row")
    if reconcile_effect(authorized, ()).status == "failed_terminal":
        raise OutboxError("authorized effect preconditions are invalid")
    return authorized


def _reconciliation_binds_effect(
    expected: AuthorizedEffect,
    reconciliation: EffectReconciliation,
    *,
    remote_id: int,
    remote_rev: int | None,
) -> bool:
    expected_recipients = tuple(expected.intended_recipients) or (None,)
    receipts = reconciliation.receipts
    return (
        reconciliation.completed
        and tuple(receipt.recipient for receipt in receipts) == expected_recipients
        and all(
            receipt.intent_id == expected.intent_id
            and receipt.approval_id == expected.approval_id
            and receipt.provider == expected.provider
            and receipt.tenant_id == expected.tenant_id
            and receipt.project == expected.project
            and receipt.operation == expected.operation
            and receipt.target_identity == expected.target_identity
            and receipt.remote_effect_id == str(remote_id)
            and receipt.resulting_revision == remote_rev
            and receipt.desired_effect_hash
            == desired_effect_hash(expected.desired_value)
            and receipt.preflight_revision == expected.preflight_revision
            and receipt.execution_principal == expected.execution_principal
            and receipt.audience_manifest_hash
            == audience_manifest_hash(expected.audience_manifest)
            and receipt.authorized_audience_recipients
            == expected.audience_manifest.included_person_entity_ids
            for receipt in receipts
        )
    )


def reconcile_uncertain_create_task(
    *,
    program_id: str,
    idempotency_key: str,
    marker_lookup: Callable[[OutboxEntry], int | None],
    reconciled_by: str,
    reconciliation: EffectReconciliation | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> ActuationDispatchOutcome:
    """Production create-task adapter for the capability-bound reconciler."""
    db_path = outbox_db_path(program_id, programs_root=programs_root)
    entry = load_entry(db_path, idempotency_key)
    if entry is None or entry.status != "uncertain_remote_state":
        raise OutboxError(
            "Marker reconciliation applies only to an uncertain_remote_state row"
        )

    def observe(current: OutboxEntry) -> ReconciliationObservation:
        found = marker_lookup(current)
        if found is None:
            return ReconciliationObservation(
                ReconciliationDisposition.ABSENT,
                "marker verified absent",
            )
        return ReconciliationObservation(
            ReconciliationDisposition.CONFIRMED,
            "provider marker lookup returned a candidate",
            remote_id=found if type(found) is int else 0,
            effect_reconciliation=reconciliation,
        )

    outcome = reconcile_uncertain_effect(
        db_path,
        idempotency_key,
        reconciled_by=reconciled_by,
        provider_lookup=observe,
    )
    if outcome.status == "completed":
        payload_fields = json.loads(entry.payload_json)
        assert isinstance(payload_fields, dict)
        _emit_receipt_recorded_best_effort(
            program_id=program_id,
            operation_intent_id=str(
                payload_fields.get("operation_intent_id") or ""
            ),
            receipt_state="succeeded",
            remote_id=outcome.remote_id,
            org=str(payload_fields.get("org") or ""),
            project=str(payload_fields.get("project") or ""),
            provider_summary={
                "remote_id": str(outcome.remote_id),
                "reconciled_by_marker": "true",
            },
            programs_root=programs_root,
        )
    return ActuationDispatchOutcome(
        entry=outcome.entry,
        remote_id=outcome.remote_id,
        status=outcome.status,
        remote_rev=(
            reconciliation.receipts[0].resulting_revision
            if outcome.status == "completed"
            and reconciliation is not None
            and reconciliation.receipts
            else None
        ),
        reconciliation=reconciliation,
    )


def _emit_intent_created_best_effort(
    *,
    program_id: str,
    operation_intent_id: str,
    idempotency_key: str,
    proposal_id: str,
    programs_root: Path,
    operation: str = "create_task.v1",
    target_identity: str = "new",
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
                "operation_type": operation,
                "target_identity": target_identity,
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
    "DISPATCHABLE_OPERATIONS",
    "ActuationAdmissionRequest",
    "ActuationApproval",
    "ActuationDispatchOutcome",
    "ActuationPreflightObservation",
    "ApprovalRevocationResult",
    "DispatchResult",
    "actuation_idempotency_key",
    "create_actuation_approval",
    "create_task_idempotency_key",
    "dispatch_approved_actuation_intent",
    "dispatch_leased_create_task",
    "effect_id_v1",
    "effect_state",
    "enqueue_approved_actuation_intent",
    "enqueue_create_task_intent",
    "outbox_db_path",
    "reconcile_uncertain_create_task",
    "require_dispatchable_operation",
    "revoke_actuation_approval",
]
