from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable
import json

from src.core.ledger.durable_outbox_store import (
    OutboxEntry,
    OutboxError,
    load_entry,
    mark_compensation_required,
    mark_reconciled_absent,
    mark_reconciled_confirmed,
    record_outbox_receipt,
)
from src.core.operation_capabilities import OperationCapability, lookup_capability


class ReconciliationDisposition(str, Enum):
    CONFIRMED = "confirmed"
    ABSENT = "absent"
    INCONCLUSIVE = "inconclusive"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class ReconciliationObservation:
    disposition: ReconciliationDisposition
    reason: str
    remote_id: int | None = None


@dataclass(frozen=True, slots=True)
class EffectReconciliationOutcome:
    entry: OutboxEntry
    remote_id: int | None
    status: str


def _retained_operation(entry: OutboxEntry) -> tuple[str, dict[str, object]]:
    try:
        payload = json.loads(entry.payload_json)
    except json.JSONDecodeError as error:
        raise OutboxError("Outbox payload is not valid JSON; manual reconciliation required") from error
    if not isinstance(payload, dict):
        raise OutboxError("Outbox payload is not an object; manual reconciliation required")
    operation = payload.get("operation", "create_task.v1")
    if not isinstance(operation, str) or not operation:
        raise OutboxError("Outbox operation is invalid; manual reconciliation required")
    return operation, payload


def _qualified_reconciliation_capability(entry: OutboxEntry) -> OperationCapability:
    operation, _ = _retained_operation(entry)
    capability = lookup_capability(operation)
    if not isinstance(capability, OperationCapability):
        raise OutboxError(f"Operation {operation!r} has no registered capability")
    if not capability.supports_remote_marker or not capability.reconcilable:
        raise OutboxError(
            f"Operation {operation!r} lacks required marker and reconciliation capability"
        )
    return capability


def _receipt(
    entry: OutboxEntry,
    *,
    disposition: ReconciliationDisposition,
    terminal_status: str,
    reconciled_by: str,
    reason: str,
    remote_id: int | None = None,
) -> dict[str, object]:
    operation, payload = _retained_operation(entry)
    recorded_at = datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": "1",
        "outbox_id": entry.outbox_id,
        "operation": operation,
        "operation_intent_id": payload.get("operation_intent_id"),
        "proposal_id": payload.get("proposal_id"),
        "org": payload.get("org"),
        "project": payload.get("project"),
        "payload_sha256": hashlib.sha256(entry.payload_json.encode("utf-8")).hexdigest(),
        "terminal_status": terminal_status,
        "disposition": disposition.value,
        "remote_id": remote_id,
        "remote_rev": None,
        "remote_response_hash": None,
        "reconciled_by": reconciled_by,
        "reconciled_by_marker": True,
        "reason": reason,
        "attempt_count": entry.attempt_count,
        "acknowledged_at": recorded_at,
        "recorded_at": recorded_at,
    }


def reconcile_uncertain_effect(
    db_path: Path,
    outbox_id: str,
    *,
    reconciled_by: str,
    provider_lookup: Callable[[OutboxEntry], ReconciliationObservation],
) -> EffectReconciliationOutcome:
    entry = load_entry(db_path, outbox_id)
    if entry is None:
        raise OutboxError(f"unknown outbox row {outbox_id!r}")
    if entry.status != "uncertain_remote_state":
        raise OutboxError("Effect reconciliation applies only to an uncertain_remote_state row")
    _qualified_reconciliation_capability(entry)

    try:
        observation = provider_lookup(entry)
    except Exception as error:
        observation = ReconciliationObservation(
            disposition=ReconciliationDisposition.INCONCLUSIVE,
            reason=f"provider lookup raised {type(error).__name__}",
        )
    if not isinstance(observation, ReconciliationObservation):
        observation = ReconciliationObservation(
            disposition=ReconciliationDisposition.INCONCLUSIVE,
            reason="provider lookup returned an invalid observation",
        )

    if observation.disposition is ReconciliationDisposition.CONFIRMED:
        if type(observation.remote_id) is not int or observation.remote_id <= 0:
            observation = ReconciliationObservation(
                disposition=ReconciliationDisposition.INCONCLUSIVE,
                reason="confirmed provider observation lacks a valid remote id",
            )
        else:
            receipt = _receipt(
                entry,
                disposition=observation.disposition,
                terminal_status="completed",
                reconciled_by=reconciled_by,
                reason=observation.reason,
                remote_id=observation.remote_id,
            )
            confirmed = mark_reconciled_confirmed(
                db_path,
                entry.outbox_id,
                reconciled_by=reconciled_by,
                receipt_json=json.dumps(
                    receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                ),
            )
            return EffectReconciliationOutcome(
                entry=confirmed,
                remote_id=observation.remote_id,
                status="completed",
            )

    if observation.disposition is ReconciliationDisposition.ABSENT:
        receipt = _receipt(
            entry,
            disposition=observation.disposition,
            terminal_status="pending",
            reconciled_by=reconciled_by,
            reason=observation.reason,
        )
        absent = mark_reconciled_absent(
            db_path,
            entry.outbox_id,
            reconciled_by=reconciled_by,
            reason=observation.reason,
            state_receipt=receipt,
        )
        return EffectReconciliationOutcome(
            entry=absent,
            remote_id=None,
            status="pending",
        )

    receipt = _receipt(
        entry,
        disposition=observation.disposition,
        terminal_status="uncertain_remote_state",
        reconciled_by=reconciled_by,
        reason=observation.reason,
    )
    record_outbox_receipt(
        db_path,
        entry.outbox_id,
        receipt,
        expected_status="uncertain_remote_state",
    )
    retained = load_entry(db_path, entry.outbox_id)
    if retained is None:
        raise OutboxError(f"outbox row {entry.outbox_id!r} disappeared during reconciliation")
    return EffectReconciliationOutcome(
        entry=retained,
        remote_id=None,
        status="uncertain_remote_state",
    )


def request_compensation(
    db_path: Path,
    outbox_id: str,
    *,
    owner: str,
    fencing_token: int,
    reason: str,
) -> OutboxEntry:
    entry = load_entry(db_path, outbox_id)
    if entry is None:
        raise OutboxError(f"unknown outbox row {outbox_id!r}")
    operation, _ = _retained_operation(entry)
    capability = lookup_capability(operation)
    if not isinstance(capability, OperationCapability):
        raise OutboxError(f"Operation {operation!r} has no registered capability")
    if not capability.compensable:
        raise OutboxError(
            f"Operation {operation!r} has no approved compensation capability"
        )
    return mark_compensation_required(
        db_path,
        outbox_id,
        owner=owner,
        fencing_token=fencing_token,
        reason=reason,
    )
