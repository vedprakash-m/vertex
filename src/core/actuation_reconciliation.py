"""RELO-W5.9: fail-closed post-dispatch effect reconciliation.

Construction-time audience resolution is not delivery evidence.  This module
compares an authorized local effect with synthetic or provider-supplied
observations, retaining one terminal receipt candidate per intended recipient.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, cast, Literal, Mapping

from src.core.audience_manifest import AudienceManifest, AudienceManifestEntry
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

ReconciliationStatus = Literal[
    "completed",
    "partial",
    "uncertain_remote_state",
    "failed_terminal",
]
ReceiptStatus = Literal["completed", "uncertain_remote_state"]


def desired_effect_hash(value: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(
        _canonical_json(value).encode("utf-8")
    ).hexdigest()


def audience_manifest_hash(manifest: AudienceManifest) -> str:
    document = {
        "generated_at": manifest.generated_at.isoformat(),
        "program_id": manifest.program_id,
        "entries": [
            {
                "person_entity_id": entry.person_entity_id,
                "scope_id": entry.scope_id,
                "disposition": entry.disposition,
                "reason": entry.reason,
                "source": entry.source,
                "source_team_entity_id": entry.source_team_entity_id,
            }
            for entry in manifest.entries
        ],
    }
    return "sha256:" + hashlib.sha256(_canonical_json(document).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AuthorizedEffect:
    program_id: str
    intent_id: str
    approval_id: str
    provider: str
    tenant_id: str
    project: str
    operation: str
    target_identity: str
    desired_value: Mapping[str, object]
    execution_principal: str
    audience_manifest: AudienceManifest
    intended_recipients: tuple[str, ...]
    preflight_revision: int
    precondition_observed_at: datetime
    evidence_version: str
    policy_version: str
    rollback_mechanism: str

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "AuthorizedEffect":
        required = (
            "program_id",
            "intent_id",
            "approval_id",
            "provider",
            "tenant_id",
            "project",
            "operation",
            "target_identity",
            "desired_value",
            "execution_principal",
            "audience_manifest",
            "intended_recipients",
            "preflight_revision",
            "precondition_observed_at",
            "evidence_version",
            "policy_version",
            "rollback_mechanism",
        )
        if any(name not in value for name in required):
            raise ValueError("Authorized effect is missing required binding fields")
        desired_value = value["desired_value"]
        if not isinstance(desired_value, Mapping):
            raise ValueError("Authorized effect desired_value must be an object")
        manifest_value = value["audience_manifest"]
        if not isinstance(manifest_value, Mapping):
            raise ValueError("Authorized effect audience_manifest must be an object")
        recipients_value = value["intended_recipients"]
        if not isinstance(recipients_value, list) or not all(
            isinstance(recipient, str) for recipient in recipients_value
        ):
            raise ValueError("Authorized effect intended_recipients must be a string array")
        text_values: dict[str, str] = {}
        for name in required:
            if name in {
                "desired_value",
                "audience_manifest",
                "intended_recipients",
                "preflight_revision",
                "precondition_observed_at",
            }:
                continue
            text = value[name]
            if not isinstance(text, str):
                raise ValueError(f"Authorized effect {name} must be a string")
            text_values[name] = text
        revision = value["preflight_revision"]
        if type(revision) is not int:
            raise ValueError("Authorized effect preflight_revision must be an integer")
        observed_at = value["precondition_observed_at"]
        if not isinstance(observed_at, str):
            raise ValueError("Authorized effect precondition_observed_at must be a timestamp string")
        try:
            parsed_observed_at = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("Authorized effect precondition_observed_at is invalid") from error
        return cls(
            program_id=text_values["program_id"],
            intent_id=text_values["intent_id"],
            approval_id=text_values["approval_id"],
            provider=text_values["provider"],
            tenant_id=text_values["tenant_id"],
            project=text_values["project"],
            operation=text_values["operation"],
            target_identity=text_values["target_identity"],
            desired_value=dict(desired_value),
            execution_principal=text_values["execution_principal"],
            audience_manifest=_audience_manifest_from_dict(manifest_value),
            intended_recipients=tuple(recipients_value),
            preflight_revision=revision,
            precondition_observed_at=parsed_observed_at,
            evidence_version=text_values["evidence_version"],
            policy_version=text_values["policy_version"],
            rollback_mechanism=text_values["rollback_mechanism"],
        )


@dataclass(frozen=True, slots=True)
class ProviderEffectEvidence:
    intent_id: str
    approval_id: str
    provider: str
    tenant_id: str
    project: str
    operation: str
    recipient: str | None
    target_identity: str
    remote_effect_id: str
    resulting_revision: int
    preflight_revision: int
    observed_value: Mapping[str, object]
    execution_principal: str
    provider_request_id: str
    provider_correlation_id: str
    response_hash: str
    attempt_count: int
    dispatched_at: datetime
    observed_at: datetime
    evidence_version: str
    policy_version: str


@dataclass(frozen=True, slots=True)
class PerRecipientReceipt:
    intent_id: str
    approval_id: str
    provider: str
    tenant_id: str
    project: str
    operation: str
    desired_effect_hash: str
    preflight_revision: int
    provider_request_id: str | None
    provider_correlation_id: str | None
    target_identity: str | None
    remote_effect_id: str | None
    resulting_revision: int | None
    response_hash: str | None
    attempt_count: int
    terminal_status: ReceiptStatus
    dispatched_at: datetime | None
    observed_at: datetime | None
    rollback_mechanism: str
    evidence_version: str
    policy_version: str
    execution_principal: str
    audience_manifest_hash: str
    authorized_audience_recipients: tuple[str, ...]
    recipient: str | None
    observed_value_hash: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "intent_id": self.intent_id,
            "approval_id": self.approval_id,
            "provider": self.provider,
            "tenant_id": self.tenant_id,
            "project": self.project,
            "operation": self.operation,
            "desired_effect_hash": self.desired_effect_hash,
            "preflight_revision": self.preflight_revision,
            "provider_request_id": self.provider_request_id,
            "provider_correlation_id": self.provider_correlation_id,
            "target_identity": self.target_identity,
            "remote_effect_id": self.remote_effect_id,
            "resulting_revision": self.resulting_revision,
            "response_hash": self.response_hash,
            "attempt_count": self.attempt_count,
            "terminal_status": self.terminal_status,
            "dispatched_at": self.dispatched_at.isoformat() if self.dispatched_at else None,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "rollback_mechanism": self.rollback_mechanism,
            "evidence_version": self.evidence_version,
            "policy_version": self.policy_version,
            "execution_principal": self.execution_principal,
            "audience_manifest_hash": self.audience_manifest_hash,
            "authorized_audience_recipients": list(self.authorized_audience_recipients),
            "recipient": self.recipient,
            "observed_value_hash": self.observed_value_hash,
        }


@dataclass(frozen=True, slots=True)
class EffectReconciliation:
    status: ReconciliationStatus
    receipts: tuple[PerRecipientReceipt, ...]
    mismatches: tuple[str, ...]

    @property
    def completed(self) -> bool:
        return (
            self.status == "completed"
            and bool(self.receipts)
            and not self.mismatches
            and all(receipt.terminal_status == "completed" for receipt in self.receipts)
        )

    def receipt_for(self, recipient: str | None) -> PerRecipientReceipt | None:
        return next((receipt for receipt in self.receipts if receipt.recipient == recipient), None)

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "completed": self.completed,
            "receipts": [receipt.to_dict() for receipt in self.receipts],
            "mismatches": list(self.mismatches),
        }


def reconcile_effect(
    expected: AuthorizedEffect,
    provider_evidence: tuple[ProviderEffectEvidence, ...],
) -> EffectReconciliation:
    """Certify an observed effect, or retain an exact partial/uncertain state."""
    preflight_mismatches = _preflight_mismatches(expected)
    if preflight_mismatches:
        return EffectReconciliation(
            status="failed_terminal",
            receipts=(),
            mismatches=tuple(preflight_mismatches),
        )

    expected_recipients: tuple[str | None, ...]
    if expected.intended_recipients:
        expected_recipients = expected.intended_recipients
    else:
        expected_recipients = (None,)

    receipts: list[PerRecipientReceipt] = []
    mismatches: list[str] = []
    evidence_by_recipient: dict[str | None, list[ProviderEffectEvidence]] = {}
    for evidence in provider_evidence:
        evidence_by_recipient.setdefault(evidence.recipient, []).append(evidence)

    authorized_recipient_set = set(expected.audience_manifest.included_person_entity_ids)
    for recipient in expected_recipients:
        candidates = evidence_by_recipient.pop(recipient, [])
        if not candidates:
            mismatches.append(f"recipient {recipient!r}: provider evidence missing")
            receipts.append(_missing_receipt(expected, recipient))
            continue
        if len(candidates) != 1:
            mismatches.append(f"recipient {recipient!r}: duplicate provider evidence")
            receipts.append(_receipt(expected, candidates[0], recipient, ("recipient duplicate",)))
            continue
        evidence = candidates[0]
        receipt_mismatches = _evidence_mismatches(expected, evidence, recipient, authorized_recipient_set)
        mismatches.extend(
            f"recipient {recipient!r}: {mismatch}" for mismatch in receipt_mismatches
        )
        receipts.append(_receipt(expected, evidence, recipient, receipt_mismatches))

    for recipient in sorted((key for key in evidence_by_recipient), key=lambda value: "" if value is None else value):
        mismatches.append(f"recipient {recipient!r}: unexpected provider recipient")
        for evidence in evidence_by_recipient[recipient]:
            receipts.append(
                _receipt(
                    expected,
                    evidence,
                    recipient,
                    ("recipient unexpected",),
                )
            )

    completed_count = sum(receipt.terminal_status == "completed" for receipt in receipts)
    if (
        not mismatches
        and completed_count == len(receipts)
        and len(receipts) == len(expected_recipients)
    ):
        status: ReconciliationStatus = "completed"
    elif completed_count > 0:
        status = "partial"
    elif provider_evidence:
        status = "uncertain_remote_state"
    else:
        status = "uncertain_remote_state"
    return EffectReconciliation(status=status, receipts=tuple(receipts), mismatches=tuple(mismatches))


def _preflight_mismatches(expected: AuthorizedEffect) -> list[str]:
    mismatches: list[str] = []
    required_text = (
        ("program_id", expected.program_id),
        ("intent_id", expected.intent_id),
        ("approval_id", expected.approval_id),
        ("provider", expected.provider),
        ("tenant_id", expected.tenant_id),
        ("project", expected.project),
        ("operation", expected.operation),
        ("target_identity", expected.target_identity),
        ("execution_principal", expected.execution_principal),
        ("evidence_version", expected.evidence_version),
        ("policy_version", expected.policy_version),
        ("rollback_mechanism", expected.rollback_mechanism),
    )
    mismatches.extend(f"{name} is blank" for name, value in required_text if not value.strip())
    if expected.audience_manifest.program_id != expected.program_id:
        mismatches.append("audience manifest program does not match effect program")
    if len(expected.intended_recipients) != len(set(expected.intended_recipients)):
        mismatches.append("intended recipients contain duplicates")
    if any(not recipient.strip() for recipient in expected.intended_recipients):
        mismatches.append("intended recipients contain a blank identity")
    authorized = set(expected.audience_manifest.included_person_entity_ids)
    unauthorized = set(expected.intended_recipients) - authorized
    if unauthorized:
        mismatches.append("audience manifest does not authorize intended recipient set")
    if type(expected.preflight_revision) is not int or expected.preflight_revision <= 0:
        mismatches.append("preflight revision is invalid")
    if _as_utc(expected.precondition_observed_at) is None:
        mismatches.append("precondition timestamp is not timezone-aware")
    elif _as_utc(expected.audience_manifest.generated_at) is None:
        mismatches.append("audience manifest timestamp is not timezone-aware")
    elif expected.audience_manifest.generated_at > expected.precondition_observed_at:
        mismatches.append("audience manifest is newer than the retained precondition")
    try:
        desired_effect_hash(expected.desired_value)
    except (TypeError, ValueError):
        mismatches.append("desired value is not canonical JSON")
    return mismatches


def _evidence_mismatches(
    expected: AuthorizedEffect,
    evidence: ProviderEffectEvidence,
    recipient: str | None,
    authorized_recipient_set: set[str],
) -> tuple[str, ...]:
    mismatches: list[str] = []
    exact_fields = (
        ("intent", evidence.intent_id, expected.intent_id),
        ("approval", evidence.approval_id, expected.approval_id),
        ("provider", evidence.provider, expected.provider),
        ("tenant", evidence.tenant_id, expected.tenant_id),
        ("project", evidence.project, expected.project),
        ("operation", evidence.operation, expected.operation),
        ("target", evidence.target_identity, expected.target_identity),
        ("principal", evidence.execution_principal, expected.execution_principal),
        ("evidence version", evidence.evidence_version, expected.evidence_version),
        ("policy version", evidence.policy_version, expected.policy_version),
    )
    mismatches.extend(
        f"{label} mismatch" for label, observed, wanted in exact_fields if observed != wanted
    )
    if recipient is not None and recipient not in authorized_recipient_set:
        mismatches.append("audience mismatch")
    if evidence.preflight_revision != expected.preflight_revision:
        mismatches.append("preflight revision mismatch")
    if type(evidence.resulting_revision) is not int or evidence.resulting_revision <= 0:
        mismatches.append("resulting revision invalid")
    if type(evidence.attempt_count) is not int or evidence.attempt_count <= 0:
        mismatches.append("attempt count invalid")
    if not evidence.remote_effect_id.strip():
        mismatches.append("remote effect ID missing")
    if not evidence.provider_request_id.strip() or not evidence.provider_correlation_id.strip():
        mismatches.append("provider request identity missing")
    if not evidence.response_hash.strip():
        mismatches.append("response hash missing")
    dispatched_at = _as_utc(evidence.dispatched_at)
    observed_at = _as_utc(evidence.observed_at)
    precondition_observed_at = _as_utc(expected.precondition_observed_at)
    if dispatched_at is None or observed_at is None:
        mismatches.append("provider evidence timestamps are invalid")
    elif (
        precondition_observed_at is None
        or observed_at < dispatched_at
        or dispatched_at < precondition_observed_at
    ):
        mismatches.append("precondition freshness mismatch")
    try:
        observed_hash = desired_effect_hash(evidence.observed_value)
    except (TypeError, ValueError):
        mismatches.append("observed value is not canonical JSON")
    else:
        if observed_hash != desired_effect_hash(expected.desired_value):
            mismatches.append("value mismatch")
    return tuple(mismatches)


def _receipt(
    expected: AuthorizedEffect,
    evidence: ProviderEffectEvidence,
    recipient: str | None,
    mismatches: tuple[str, ...],
) -> PerRecipientReceipt:
    try:
        observed_hash = desired_effect_hash(evidence.observed_value)
    except (TypeError, ValueError):
        observed_hash = None
    return PerRecipientReceipt(
        intent_id=expected.intent_id,
        approval_id=expected.approval_id,
        provider=expected.provider,
        tenant_id=expected.tenant_id,
        project=expected.project,
        operation=expected.operation,
        desired_effect_hash=desired_effect_hash(expected.desired_value),
        preflight_revision=expected.preflight_revision,
        provider_request_id=evidence.provider_request_id,
        provider_correlation_id=evidence.provider_correlation_id,
        target_identity=evidence.target_identity,
        remote_effect_id=evidence.remote_effect_id,
        resulting_revision=evidence.resulting_revision,
        response_hash=evidence.response_hash,
        attempt_count=evidence.attempt_count,
        terminal_status="completed" if not mismatches else "uncertain_remote_state",
        dispatched_at=evidence.dispatched_at if _as_utc(evidence.dispatched_at) else None,
        observed_at=evidence.observed_at if _as_utc(evidence.observed_at) else None,
        rollback_mechanism=expected.rollback_mechanism,
        evidence_version=expected.evidence_version,
        policy_version=expected.policy_version,
        execution_principal=expected.execution_principal,
        audience_manifest_hash=audience_manifest_hash(expected.audience_manifest),
        authorized_audience_recipients=expected.audience_manifest.included_person_entity_ids,
        recipient=recipient,
        observed_value_hash=observed_hash,
    )


def _missing_receipt(expected: AuthorizedEffect, recipient: str | None) -> PerRecipientReceipt:
    return PerRecipientReceipt(
        intent_id=expected.intent_id,
        approval_id=expected.approval_id,
        provider=expected.provider,
        tenant_id=expected.tenant_id,
        project=expected.project,
        operation=expected.operation,
        desired_effect_hash=desired_effect_hash(expected.desired_value),
        preflight_revision=expected.preflight_revision,
        provider_request_id=None,
        provider_correlation_id=None,
        target_identity=None,
        remote_effect_id=None,
        resulting_revision=None,
        response_hash=None,
        attempt_count=0,
        terminal_status="uncertain_remote_state",
        dispatched_at=None,
        observed_at=None,
        rollback_mechanism=expected.rollback_mechanism,
        evidence_version=expected.evidence_version,
        policy_version=expected.policy_version,
        execution_principal=expected.execution_principal,
        audience_manifest_hash=audience_manifest_hash(expected.audience_manifest),
        authorized_audience_recipients=expected.audience_manifest.included_person_entity_ids,
        recipient=recipient,
        observed_value_hash=None,
    )


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _as_utc(value: datetime) -> datetime | None:
    if value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def _audience_manifest_from_dict(value: Mapping[str, object]) -> AudienceManifest:
    generated_at = value.get("generated_at")
    program_id = value.get("program_id")
    entries = value.get("entries")
    if not isinstance(generated_at, str) or not isinstance(program_id, str) or not isinstance(entries, list):
        raise ValueError("Audience manifest is missing generated_at, program_id or entries")
    try:
        parsed_generated_at = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Audience manifest generated_at is invalid") from error
    parsed_entries: list[AudienceManifestEntry] = []
    for raw_entry in entries:
        if not isinstance(raw_entry, Mapping):
            raise ValueError("Audience manifest entry must be an object")
        person_entity_id = raw_entry.get("person_entity_id")
        scope_id = raw_entry.get("scope_id")
        disposition = raw_entry.get("disposition")
        reason = raw_entry.get("reason")
        source = raw_entry.get("source")
        source_team_entity_id = raw_entry.get("source_team_entity_id")
        if (
            not isinstance(person_entity_id, str)
            or not isinstance(scope_id, str)
            or not isinstance(disposition, str)
            or not isinstance(reason, str)
            or (source is not None and not isinstance(source, str))
            or (source_team_entity_id is not None and not isinstance(source_team_entity_id, str))
        ):
            raise ValueError("Audience manifest entry fields are invalid")
        parsed_entries.append(AudienceManifestEntry(
            person_entity_id=person_entity_id,
            scope_id=scope_id,
            disposition=disposition,
            reason=reason,
            source=source,
            source_team_entity_id=source_team_entity_id,
        ))
    return AudienceManifest(
        generated_at=parsed_generated_at,
        program_id=program_id,
        entries=tuple(parsed_entries),
    )


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
    effect_reconciliation: EffectReconciliation | None = None


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


def _authorized_effect_for_entry(entry: OutboxEntry) -> AuthorizedEffect:
    operation, payload = _retained_operation(entry)
    raw = payload.get("authorized_effect")
    if not isinstance(raw, dict):
        raise OutboxError(
            "Effect reconciliation requires retained authorized-effect evidence"
        )
    try:
        authorized = AuthorizedEffect.from_dict(raw)
    except ValueError as error:
        raise OutboxError("Retained authorized-effect evidence is invalid") from error
    if (
        authorized.operation != operation
        or payload.get("operation_intent_id") != authorized.intent_id
        or payload.get("org") != authorized.tenant_id
        or payload.get("project") != authorized.project
    ):
        raise OutboxError(
            "Retained authorized-effect evidence does not bind the outbox intent"
        )
    return authorized


def _reconciliation_binds_authorized_effect(
    expected: AuthorizedEffect,
    observed: EffectReconciliation,
    *,
    remote_id: int,
) -> bool:
    if not observed.completed:
        return False
    recipients = expected.intended_recipients or (None,)
    if tuple(receipt.recipient for receipt in observed.receipts) != recipients:
        return False
    manifest_hash = audience_manifest_hash(expected.audience_manifest)
    effect_hash = desired_effect_hash(expected.desired_value)
    resulting_revisions: set[int] = set()
    for receipt in observed.receipts:
        if (
            not isinstance(expected.precondition_observed_at, datetime)
            or not isinstance(receipt.dispatched_at, datetime)
            or not isinstance(receipt.observed_at, datetime)
        ):
            return False
        precondition_at = _as_utc(expected.precondition_observed_at)
        dispatched_at = _as_utc(receipt.dispatched_at)
        observed_at = _as_utc(receipt.observed_at)
        if (
            precondition_at is None
            or dispatched_at is None
            or observed_at is None
            or receipt.terminal_status != "completed"
            or type(receipt.resulting_revision) is not int
            or receipt.resulting_revision <= 0
            or type(receipt.attempt_count) is not int
            or receipt.attempt_count <= 0
            or not receipt.provider_request_id
            or not receipt.provider_request_id.strip()
            or not receipt.provider_correlation_id
            or not receipt.provider_correlation_id.strip()
            or not receipt.response_hash
            or not receipt.response_hash.strip()
            or receipt.evidence_version != expected.evidence_version
            or receipt.policy_version != expected.policy_version
            or receipt.rollback_mechanism != expected.rollback_mechanism
            or receipt.observed_value_hash != effect_hash
            or dispatched_at < precondition_at
            or observed_at < dispatched_at
        ):
            return False
        resulting_revisions.add(receipt.resulting_revision)
    if len(resulting_revisions) != 1:
        return False
    return all(
        receipt.intent_id == expected.intent_id
        and receipt.approval_id == expected.approval_id
        and receipt.provider == expected.provider
        and receipt.tenant_id == expected.tenant_id
        and receipt.project == expected.project
        and receipt.operation == expected.operation
        and receipt.target_identity == expected.target_identity
        and receipt.remote_effect_id == str(remote_id)
        and receipt.desired_effect_hash == effect_hash
        and receipt.preflight_revision == expected.preflight_revision
        and receipt.execution_principal == expected.execution_principal
        and receipt.audience_manifest_hash == manifest_hash
        and receipt.authorized_audience_recipients
        == expected.audience_manifest.included_person_entity_ids
        for receipt in observed.receipts
    )


def _outbox_receipt(
    entry: OutboxEntry,
    *,
    disposition: ReconciliationDisposition,
    terminal_status: str,
    reconciled_by: str,
    reason: str,
    remote_id: int | None = None,
    effect_reconciliation: EffectReconciliation | None = None,
    compensation_operation: str | None = None,
) -> dict[str, object]:
    operation, payload = _retained_operation(entry)
    recorded_at = datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": "1",
        "program_id": payload.get("program_id"),
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
        "remote_rev": (
            effect_reconciliation.receipts[0].resulting_revision
            if effect_reconciliation is not None
            and effect_reconciliation.receipts
            else None
        ),
        "remote_response_hash": None,
        "reconciled_by": reconciled_by,
        "reconciled_by_marker": True,
        "reason": reason,
        "attempt_count": entry.attempt_count,
        "fencing_token": entry.lease_fencing_token,
        "compensation_required": False,
        "acknowledged_at": recorded_at,
        "recorded_at": recorded_at,
        "effect_reconciliation": (
            effect_reconciliation.to_dict()
            if effect_reconciliation is not None
            else None
        ),
        "compensation_operation": compensation_operation,
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
            try:
                authorized = _authorized_effect_for_entry(entry)
            except OutboxError as error:
                observation = ReconciliationObservation(
                    disposition=ReconciliationDisposition.INCONCLUSIVE,
                    reason=str(error),
                )
            else:
                effect_reconciliation = observation.effect_reconciliation
                remote_id = cast(int, observation.remote_id)
                if (
                    effect_reconciliation is None
                    or not _reconciliation_binds_authorized_effect(
                        authorized,
                        effect_reconciliation,
                        remote_id=remote_id,
                    )
                ):
                    observation = ReconciliationObservation(
                        disposition=ReconciliationDisposition.INCONCLUSIVE,
                        reason="marker confirmation lacks exact authorized-effect evidence",
                    )
                else:
                    receipt = _outbox_receipt(
                        entry,
                        disposition=observation.disposition,
                        terminal_status="completed",
                        reconciled_by=reconciled_by,
                        reason=observation.reason,
                        remote_id=remote_id,
                        effect_reconciliation=effect_reconciliation,
                    )
                    confirmed = mark_reconciled_confirmed(
                        db_path,
                        entry.outbox_id,
                        reconciled_by=reconciled_by,
                        receipt_json=json.dumps(
                            receipt,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ),
                    )
                    return EffectReconciliationOutcome(
                        entry=confirmed,
                        remote_id=observation.remote_id,
                        status="completed",
                    )

    if observation.disposition is ReconciliationDisposition.ABSENT:
        receipt = _outbox_receipt(
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

    receipt = _outbox_receipt(
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
    if not capability.compensation_operation:
        raise OutboxError(
            f"Operation {operation!r} has no named compensation operation"
        )
    inverse = lookup_capability(capability.compensation_operation)
    if not isinstance(inverse, OperationCapability) or inverse.dispatchable:
        raise OutboxError(
            f"Operation {operation!r} has no registered offline compensation policy"
        )
    return mark_compensation_required(
        db_path,
        outbox_id,
        owner=owner,
        fencing_token=fencing_token,
        reason=reason,
        compensation_operation=capability.compensation_operation,
    )


__all__ = [
    "AuthorizedEffect",
    "EffectReconciliation",
    "EffectReconciliationOutcome",
    "PerRecipientReceipt",
    "ProviderEffectEvidence",
    "ReconciliationDisposition",
    "ReconciliationObservation",
    "audience_manifest_hash",
    "desired_effect_hash",
    "reconcile_effect",
    "reconcile_uncertain_effect",
    "request_compensation",
]
