"""Durable approval identity, admission and reservation primitives for RELO-W5."""
from __future__ import annotations

import json
import hashlib
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from src.core._db import open_program_db_with_retry as open_program_db
from src.core.ledger.durable_outbox_store import (
    _SCHEMA_SQL,
    _append_state_receipt,
    _entry_from_row,
    _state_receipt,
    OutboxEntry,
    OutboxError,
)
from src.core.operation_capabilities import (
    CapabilityRefusal,
    EffectClass,
    approved_remote_marker,
    create_target_identity,
    desired_effect_hash,
    lookup_capability,
    semantic_effect_key,
)
from src.core.operator_identity import OperatorIdentity
from src.core.unit_of_work import open_unit_of_work
from src.core.workspace_lease import ACTUATION_DISPATCH_DOMAIN, LeaseHandle, get_workspace_lease_db_path

_APPROVAL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS outbox_approvals (
    approval_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    semantic_effect_key TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    approval_policy_version TEXT NOT NULL,
    approver_actor TEXT NOT NULL,
    approver_principal TEXT NOT NULL,
    approver_machine TEXT NOT NULL,
    approver_session TEXT NOT NULL,
    evidence_snapshot_id TEXT NOT NULL,
    evidence_expires_at TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    revoked_by_json TEXT,
    revocation_reason TEXT,
    UNIQUE(intent_id, revision)
);
CREATE INDEX IF NOT EXISTS ix_outbox_approvals_intent
    ON outbox_approvals(intent_id, revision);
CREATE TABLE IF NOT EXISTS outbox_current_approvals (
    intent_id TEXT PRIMARY KEY,
    approval_id TEXT NOT NULL,
    revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox_admissions (
    outbox_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    approval_id TEXT NOT NULL,
    approval_revision INTEGER NOT NULL,
    semantic_effect_key TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    desired_effect_hash TEXT NOT NULL,
    provider TEXT NOT NULL,
    tenant TEXT NOT NULL,
    project TEXT NOT NULL,
    operation TEXT NOT NULL,
    target_identity TEXT NOT NULL,
    causal_occurrence_id TEXT NOT NULL,
    expected_revision INTEGER,
    expected_state TEXT,
    rate_key TEXT NOT NULL,
    rate_limit INTEGER NOT NULL,
    blast_radius INTEGER NOT NULL,
    admitted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_outbox_admissions_target
    ON outbox_admissions(provider, tenant, project, target_identity);
CREATE TABLE IF NOT EXISTS outbox_rate_reservations (
    outbox_id TEXT PRIMARY KEY,
    rate_key TEXT NOT NULL,
    reserved_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_outbox_rate_reservations_key
    ON outbox_rate_reservations(rate_key);
CREATE TABLE IF NOT EXISTS outbox_marker_reservations (
    marker_value TEXT PRIMARY KEY,
    outbox_id TEXT NOT NULL,
    semantic_effect_key TEXT NOT NULL,
    reserved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox_effects (
    effect_id TEXT PRIMARY KEY,
    outbox_id TEXT NOT NULL
);
"""

_OUTBOX_SELECT = (
    "outbox_id, domain, correlation_id, payload_json, status, lease_owner, "
    "lease_fencing_token, lease_expires_at, attempt_count, next_attempt_at, "
    "remote_request_hash, remote_response_hash, failure_reason, created_at, updated_at"
)
_ACTIVE_STATUSES = (
    "pending",
    "failed_retryable",
    "leased",
    "dispatched",
    "succeeded",
    "audited",
    "uncertain_remote_state",
    "compensation_required",
)


@dataclass(frozen=True, slots=True)
class ActuationApproval:
    approval_id: str
    intent_id: str
    revision: int
    semantic_effect_key: str
    payload_sha256: str
    approval_policy_version: str
    approver: OperatorIdentity
    evidence_snapshot_id: str
    evidence_expires_at: datetime
    approved_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ActuationAdmissionRequest:
    program_id: str
    intent_id: str
    proposal_id: str
    provider: str
    tenant: str
    project: str
    operation: str
    target_identity: str
    causal_occurrence_id: str
    desired_effect: Mapping[str, object]
    payload_json: str
    approval_policy_version: str
    rate_scope: str
    rate_limit: int
    blast_radius: int
    scope_enabled: bool
    rule_enabled: bool
    expected_revision: int | None
    expected_state: str | None
    evidence_snapshot_id: str
    evidence_expires_at: datetime


@dataclass(frozen=True, slots=True)
class ActuationPreflightObservation:
    evidence_snapshot_id: str
    target_count: int
    current_revision: int | None
    current_state: str | None
    marker_found: bool
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class ApprovalRevocationResult:
    approval_id: str
    cancelled_outbox_ids: tuple[str, ...]
    reconciliation_required_outbox_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QualifiedDispatchAdmission:
    allowed: bool
    entry: OutboxEntry
    reason: str | None = None


def _wire(value: datetime) -> str:
    return _aware_utc(value).isoformat().replace("+00:00", "Z")


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise OutboxError("Actuation admission requires timezone-aware timestamps")
    return value.astimezone(timezone.utc)


def _parse(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError) as error:
        raise OutboxError("Actuation admission contains invalid stored time") from error


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OutboxError(f"{name} must be a nonempty string")
    return value


def _canonical_payload(payload_json: str) -> dict[str, object]:
    try:
        decoded = json.loads(payload_json)
    except (TypeError, json.JSONDecodeError) as error:
        raise OutboxError("Actuation payload is not valid JSON") from error
    if not isinstance(decoded, dict):
        raise OutboxError("Actuation payload must be a JSON object")
    return decoded


def _payload_hash(payload_json: str) -> str:
    canonical = json.dumps(
        _canonical_payload(payload_json), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _request_semantic_key(request: ActuationAdmissionRequest) -> str:
    try:
        return semantic_effect_key(
            program_id=request.program_id,
            provider=request.provider,
            tenant=request.tenant,
            project=request.project,
            operation=request.operation,
            target_identity=request.target_identity,
            causal_occurrence_id=request.causal_occurrence_id,
            payload=request.desired_effect,
        )
    except (TypeError, ValueError) as error:
        raise OutboxError(str(error)) from error


def _request_marker(request: ActuationAdmissionRequest) -> str | None:
    semantic = _request_semantic_key(request)
    try:
        return approved_remote_marker(
            operation=request.operation,
            semantic_effect_key=semantic,
        )
    except ValueError as error:
        raise OutboxError(str(error)) from error


def load_marker_outbox_id(db_path: Path, marker: str) -> str | None:
    if not db_path.exists():
        return None
    with open_program_db(db_path, read_only=True) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'outbox_marker_reservations'"
        ).fetchone()
        if table is None:
            return None
        row = connection.execute(
            "SELECT outbox_id FROM outbox_marker_reservations WHERE marker_value = ?",
            (marker,),
        ).fetchone()
    return None if row is None else str(row[0])


def _validate_request(request: ActuationAdmissionRequest) -> None:
    for name, value in (
        ("intent_id", request.intent_id),
        ("proposal_id", request.proposal_id),
        ("approval_policy_version", request.approval_policy_version),
        ("rate_scope", request.rate_scope),
        ("evidence_snapshot_id", request.evidence_snapshot_id),
    ):
        _text(name, value)
    if request.evidence_snapshot_id.strip().casefold() in {
        "unknown", "none", "snapshot", "<snapshot>", "placeholder",
    }:
        raise OutboxError("Approval evidence snapshot must be a real snapshot identity")
    if type(request.rate_limit) is not int or request.rate_limit <= 0:
        raise OutboxError("Actuation rate limit must be a positive integer")
    if type(request.blast_radius) is not int or request.blast_radius <= 0:
        raise OutboxError("Actuation blast radius must be a positive integer")
    capability = lookup_capability(request.operation)
    if isinstance(capability, CapabilityRefusal):
        raise OutboxError(f"Operation {request.operation!r} is draft/manual only; it cannot be dispatched")
    if request.blast_radius > capability.max_blast_radius:
        raise OutboxError("Actuation blast radius exceeds the operation capability")
    if request.expected_revision is not None and (
        type(request.expected_revision) is not int or request.expected_revision <= 0
    ):
        raise OutboxError("Actuation expected revision must be a positive integer or unavailable")
    if request.operation != "create_task.v1" and request.expected_revision is None:
        raise OutboxError("Current revision is required for non-create actuation")
    if request.operation == "create_task.v1":
        expected_target = create_target_identity(
            provider=request.provider,
            tenant=request.tenant,
            project=request.project,
            causal_occurrence_id=request.causal_occurrence_id,
            payload=request.desired_effect,
        )
        if request.target_identity != expected_target:
            raise OutboxError("Create target identity must derive from a stable origin, not an intent")
    if type(request.scope_enabled) is not bool or type(request.rule_enabled) is not bool:
        raise OutboxError("Actuation scope and rule enablement must be boolean")
    if not request.scope_enabled or not request.rule_enabled:
        raise OutboxError("Actuation scope and rule must both be enabled")
    payload = _canonical_payload(request.payload_json)
    if (
        payload.get("operation") != request.operation
        or payload.get("operation_intent_id") != request.intent_id
        or payload.get("proposal_id") != request.proposal_id
        or payload.get("org") != request.tenant
        or payload.get("project") != request.project
        or payload.get("desired_effect") != request.desired_effect
    ):
        raise OutboxError("Retained payload identity does not match the approved request")
    expected_marker = _request_marker(request)
    if (
        expected_marker is not None
        and payload.get("approved_remote_marker") != expected_marker
    ):
        raise OutboxError("Retained payload does not contain the approved remote marker")
    _request_semantic_key(request)


def _validate_identity(identity: OperatorIdentity) -> None:
    if not isinstance(identity, OperatorIdentity):
        raise OutboxError("Approval operator identity is required")
    for name, value in (
        ("principal", identity.principal),
        ("machine", identity.machine),
        ("session", identity.session),
    ):
        if not isinstance(value, str) or not value.strip() or value.strip().casefold() in {
            "unknown", "none", "placeholder", "<principal>",
        }:
            raise OutboxError(f"Approval operator identity requires a real {name}")


def create_actuation_approval(
    db_path: Path,
    *,
    request: ActuationAdmissionRequest,
    approver: OperatorIdentity,
    approved_at: datetime,
    expires_at: datetime,
    approval_id: str | None = None,
) -> ActuationApproval:
    _validate_request(request)
    _validate_identity(approver)
    approved = _aware_utc(approved_at)
    expires = _aware_utc(expires_at)
    evidence_expires = _aware_utc(request.evidence_expires_at)
    if expires <= approved or evidence_expires <= approved:
        raise OutboxError("Approval and evidence expiry must be after approval time")
    semantic = _request_semantic_key(request)
    resolved_id = approval_id or f"approval-{uuid.uuid4().hex}"
    _text("approval_id", resolved_id)
    payload_hash = _payload_hash(request.payload_json)

    with open_program_db(db_path, durability="strict") as connection:
        connection.executescript(_SCHEMA_SQL + _APPROVAL_SCHEMA_SQL)
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT COALESCE(MAX(revision), 0) FROM outbox_approvals WHERE intent_id = ?",
            (request.intent_id,),
        ).fetchone()[0]
        revision = int(current) + 1
        connection.execute(
            """
            INSERT INTO outbox_approvals (
                approval_id, intent_id, revision, semantic_effect_key, payload_sha256,
                approval_policy_version, approver_actor, approver_principal,
                approver_machine, approver_session, evidence_snapshot_id,
                evidence_expires_at, approved_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                resolved_id, request.intent_id, revision, semantic, payload_hash,
                request.approval_policy_version, approver.actor, approver.principal,
                approver.machine, approver.session, request.evidence_snapshot_id,
                _wire(evidence_expires), _wire(approved), _wire(expires),
            ),
        )
        connection.execute(
            """
            INSERT INTO outbox_current_approvals (intent_id, approval_id, revision)
            VALUES (?, ?, ?)
            ON CONFLICT(intent_id) DO UPDATE SET
                approval_id = excluded.approval_id, revision = excluded.revision
            """,
            (request.intent_id, resolved_id, revision),
        )
    return ActuationApproval(
        approval_id=resolved_id,
        intent_id=request.intent_id,
        revision=revision,
        semantic_effect_key=semantic,
        payload_sha256=payload_hash,
        approval_policy_version=request.approval_policy_version,
        approver=approver,
        evidence_snapshot_id=request.evidence_snapshot_id,
        evidence_expires_at=evidence_expires,
        approved_at=approved,
        expires_at=expires,
    )


def _approval_matches_row(
    row: sqlite3.Row, request: ActuationAdmissionRequest, approval: ActuationApproval,
) -> bool:
    return (
        row["approval_id"] == approval.approval_id
        and int(row["revision"]) == approval.revision
        and row["intent_id"] == request.intent_id
        and row["semantic_effect_key"] == approval.semantic_effect_key == _request_semantic_key(request)
        and row["payload_sha256"] == approval.payload_sha256 == _payload_hash(request.payload_json)
        and row["approval_policy_version"] == approval.approval_policy_version
        == request.approval_policy_version
        and row["revoked_at"] is None
    )


def _authorized_payload_failure(
    *,
    request: ActuationAdmissionRequest,
    payload_json: str,
    approval_id: str,
    approval_principal: str,
    approval_policy_version: str,
    evidence_snapshot_id: str,
) -> str | None:
    try:
        payload = _canonical_payload(payload_json)
    except OutboxError as error:
        return str(error)
    raw = payload.get("authorized_effect")
    if not isinstance(raw, Mapping):
        return "Retained payload has no authorized-effect binding"
    expected_values = {
        "program_id": request.program_id,
        "intent_id": request.intent_id,
        "approval_id": approval_id,
        "provider": request.provider,
        "tenant_id": request.tenant,
        "project": request.project,
        "operation": request.operation,
        "target_identity": request.target_identity,
        "execution_principal": approval_principal,
        "evidence_version": evidence_snapshot_id,
        "policy_version": approval_policy_version,
    }
    for field, expected in expected_values.items():
        if raw.get(field) != expected:
            return f"Authorized-effect {field} does not match the approval admission"
    desired = raw.get("desired_value")
    if not isinstance(desired, Mapping):
        return "Authorized-effect desired value is invalid"
    try:
        if desired_effect_hash(desired) != desired_effect_hash(request.desired_effect):
            return "Authorized-effect desired value does not match the approved payload"
    except ValueError:
        return "Authorized-effect desired value is not canonical JSON"
    return None


def _retained_approval_payload(
    request: ActuationAdmissionRequest,
    approval: ActuationApproval,
) -> str:
    payload = dict(_canonical_payload(request.payload_json))
    retained = payload.get("semantic_effect_id")
    if retained is not None and retained != approval.semantic_effect_key:
        raise OutboxError(
            "Approved payload semantic effect does not match the retained approval"
        )
    payload["semantic_effect_id"] = approval.semantic_effect_key
    payload["target_identity"] = request.target_identity
    payload["causal_occurrence_id"] = request.causal_occurrence_id
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def enqueue_approved_actuation_intent(
    db_path: Path,
    *,
    outbox_id: str,
    request: ActuationAdmissionRequest,
    approval: ActuationApproval,
    now: datetime,
) -> OutboxEntry:
    _validate_request(request)
    current_time = _aware_utc(now)
    if approval.intent_id != request.intent_id or approval.semantic_effect_key != _request_semantic_key(request):
        raise OutboxError("Approval is not bound to the requested intent and semantic effect")
    if type(approval.revision) is not int or approval.revision <= 0:
        raise OutboxError("Approval revision must be a positive integer")
    if approval.payload_sha256 != _payload_hash(request.payload_json):
        raise OutboxError("Approval payload hash does not match the retained payload")
    marker = _request_marker(request)
    retained_payload_json = _retained_approval_payload(request, approval)

    with open_program_db(db_path, durability="strict") as connection:
        connection.executescript(_SCHEMA_SQL + _APPROVAL_SCHEMA_SQL)
        connection.execute("BEGIN IMMEDIATE")
        stored = connection.execute(
            "SELECT a.*, c.revision AS current_revision FROM outbox_approvals a "
            "JOIN outbox_current_approvals c ON c.intent_id = a.intent_id AND c.approval_id = a.approval_id "
            "WHERE a.approval_id = ?",
            (approval.approval_id,),
        ).fetchone()
        if stored is None or not _approval_matches_row(stored, request, approval):
            raise OutboxError("Approval is missing, changed or not the current revision")
        if (
            _parse(stored["approved_at"]) > current_time
            or current_time >= _parse(stored["expires_at"])
            or current_time >= _parse(stored["evidence_expires_at"])
        ):
            raise OutboxError("Approval or evidence has expired")

        existing_effect = connection.execute(
            "SELECT outbox_id FROM outbox_effects WHERE effect_id = ?",
            (approval.semantic_effect_key,),
        ).fetchone()
        if existing_effect is not None:
            existing_id = str(existing_effect[0])
            existing = connection.execute(
                f"SELECT {_OUTBOX_SELECT} FROM outbox WHERE outbox_id = ?",
                (existing_id,),
            ).fetchone()
            if existing is None:
                raise OutboxError("Semantic effect is bound to a missing outbox row")
            if marker is not None:
                marker_row = connection.execute(
                    "SELECT outbox_id, semantic_effect_key "
                    "FROM outbox_marker_reservations WHERE marker_value = ?",
                    (marker,),
                ).fetchone()
                if (
                    marker_row is None
                    or marker_row["outbox_id"] != existing_id
                    or marker_row["semantic_effect_key"] != approval.semantic_effect_key
                ):
                    raise OutboxError("Approved marker reservation is missing or changed")
            return _entry_from_row(tuple(existing))

        existing = connection.execute(
            f"SELECT {_OUTBOX_SELECT} FROM outbox WHERE outbox_id = ?",
            (outbox_id,),
        ).fetchone()
        if existing is not None:
            admission = connection.execute(
                "SELECT approval_id, approval_revision, semantic_effect_key, payload_sha256 "
                "FROM outbox_admissions WHERE outbox_id = ?",
                (outbox_id,),
            ).fetchone()
            if (
                admission is None
                or admission["approval_id"] != approval.approval_id
                or int(admission["approval_revision"]) != approval.revision
                or admission["semantic_effect_key"] != approval.semantic_effect_key
                or admission["payload_sha256"] != approval.payload_sha256
                or existing["payload_json"] != retained_payload_json
            ):
                raise OutboxError("Existing provider request has different approval or payload identity")
            return _entry_from_row(tuple(existing))

        approval_principal = approval.approver.principal
        if not isinstance(approval_principal, str) or not approval_principal.strip():
            raise OutboxError("Approved payload has no durable approval principal")
        binding_failure = _authorized_payload_failure(
            request=request,
            payload_json=request.payload_json,
            approval_id=approval.approval_id,
            approval_principal=approval_principal,
            approval_policy_version=approval.approval_policy_version,
            evidence_snapshot_id=approval.evidence_snapshot_id,
        )
        if binding_failure is not None:
            raise OutboxError(binding_failure)

        conflict = connection.execute(
            f"""
            SELECT a.outbox_id
            FROM outbox_admissions a
            JOIN outbox o ON o.outbox_id = a.outbox_id
            WHERE a.provider = ? AND a.tenant = ? AND a.project = ?
              AND a.target_identity = ? AND o.status IN ({",".join("?" for _ in _ACTIVE_STATUSES)})
            ORDER BY a.admitted_at LIMIT 1
            """,
            (
                request.provider, request.tenant, request.project, request.target_identity,
                *_ACTIVE_STATUSES,
            ),
        ).fetchone()
        if conflict is not None:
            raise OutboxError("A conflicting active intent is already reserved for this target")

        if marker is not None:
            marker_conflict = connection.execute(
                "SELECT outbox_id FROM outbox_marker_reservations WHERE marker_value = ?",
                (marker,),
            ).fetchone()
            if marker_conflict is not None and marker_conflict["outbox_id"] != outbox_id:
                raise OutboxError("Approved marker is reserved by a different outbox effect")

        reservation_count = int(connection.execute(
            "SELECT COUNT(*) FROM outbox_rate_reservations WHERE rate_key = ?",
            (request.rate_scope,),
        ).fetchone()[0])
        if reservation_count >= request.rate_limit:
            raise OutboxError("Actuation rate reservation capacity is exhausted")

        now_wire = _wire(current_time)
        connection.execute(
            """
            INSERT INTO outbox (
                outbox_id, domain, correlation_id, payload_json, status,
                lease_fencing_token, attempt_count, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'pending', 0, 0, ?, ?)
            """,
            (
                outbox_id, ACTUATION_DISPATCH_DOMAIN, outbox_id, retained_payload_json,
                now_wire, now_wire,
            ),
        )
        connection.execute(
            """
            INSERT INTO outbox_admissions (
                outbox_id, intent_id, approval_id, approval_revision,
                semantic_effect_key, payload_sha256, desired_effect_hash,
                provider, tenant, project, operation, target_identity,
                causal_occurrence_id, expected_revision, expected_state,
                rate_key, rate_limit, blast_radius, admitted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                outbox_id, request.intent_id, approval.approval_id, approval.revision,
                approval.semantic_effect_key, approval.payload_sha256,
                desired_effect_hash(request.desired_effect), request.provider, request.tenant,
                request.project, request.operation, request.target_identity,
                request.causal_occurrence_id, request.expected_revision, request.expected_state,
                request.rate_scope, request.rate_limit, request.blast_radius, now_wire,
            ),
        )
        connection.execute(
            "INSERT INTO outbox_effects (effect_id, outbox_id) VALUES (?, ?)",
            (approval.semantic_effect_key, outbox_id),
        )
        if marker is not None:
            connection.execute(
                """
                INSERT INTO outbox_marker_reservations (
                    marker_value, outbox_id, semantic_effect_key, reserved_at
                ) VALUES (?, ?, ?, ?)
                """,
                (marker, outbox_id, approval.semantic_effect_key, now_wire),
            )
        connection.execute(
            "INSERT INTO outbox_rate_reservations (outbox_id, rate_key, reserved_at) VALUES (?, ?, ?)",
            (outbox_id, request.rate_scope, now_wire),
        )
        row = connection.execute(
            f"SELECT {_OUTBOX_SELECT} FROM outbox WHERE outbox_id = ?",
            (outbox_id,),
        ).fetchone()
    return _entry_from_row(tuple(row))


def revoke_actuation_approval(
    db_path: Path,
    *,
    approval_id: str,
    intent_id: str,
    revision: int,
    revoked_by: OperatorIdentity,
    reason: str,
    now: datetime,
) -> ApprovalRevocationResult:
    _validate_identity(revoked_by)
    _text("revocation reason", reason)
    revoked_at = _aware_utc(now)
    cancelled: list[str] = []
    reconciliation: list[str] = []
    with open_program_db(db_path, durability="strict") as connection:
        connection.executescript(_APPROVAL_SCHEMA_SQL)
        connection.execute("BEGIN IMMEDIATE")
        approval = connection.execute(
            """
            SELECT a.approval_id
            FROM outbox_approvals a
            JOIN outbox_current_approvals c
              ON c.intent_id = a.intent_id AND c.approval_id = a.approval_id AND c.revision = a.revision
            WHERE a.approval_id = ? AND a.intent_id = ? AND a.revision = ? AND a.revoked_at IS NULL
            """,
            (approval_id, intent_id, revision),
        ).fetchone()
        if approval is None:
            raise OutboxError("Approval revocation requires the unchanged current revision")
        connection.execute(
            """
            UPDATE outbox_approvals
            SET revoked_at = ?, revoked_by_json = ?, revocation_reason = ?
            WHERE approval_id = ?
            """,
            (
                _wire(revoked_at), json.dumps(revoked_by.as_dict(), sort_keys=True),
                reason, approval_id,
            ),
        )
        rows = connection.execute(
            """
            SELECT o.outbox_id, o.status, o.domain, o.correlation_id, o.payload_json,
                   o.lease_fencing_token, o.attempt_count
            FROM outbox_admissions a JOIN outbox o ON o.outbox_id = a.outbox_id
            WHERE a.approval_id = ? ORDER BY o.created_at
            """,
            (approval_id,),
        ).fetchall()
        for row in rows:
            outbox_id = str(row["outbox_id"])
            if row["status"] in {"pending", "leased", "failed_retryable"}:
                connection.execute(
                    """
                    UPDATE outbox
                    SET status = 'cancelled', lease_owner = NULL, lease_expires_at = NULL,
                        failure_reason = ?, updated_at = ?
                    WHERE outbox_id = ?
                    """,
                    (f"Approval revoked: {reason}", _wire(revoked_at), outbox_id),
                )
                connection.execute(
                    "DELETE FROM outbox_rate_reservations WHERE outbox_id = ?",
                    (outbox_id,),
                )
                connection.execute(
                    "DELETE FROM outbox_marker_reservations WHERE outbox_id = ?",
                    (outbox_id,),
                )
                receipt_at = _wire(revoked_at)
                assert receipt_at is not None
                receipt_json = _state_receipt(
                    outbox_id=outbox_id,
                    domain=str(row["domain"]),
                    correlation_id=str(row["correlation_id"]),
                    payload_json=str(row["payload_json"]),
                    terminal_status="cancelled",
                    owner=revoked_by.principal or revoked_by.actor,
                    fencing_token=int(row["lease_fencing_token"]),
                    attempt_count=int(row["attempt_count"]),
                    reason=f"Approval revoked: {reason}",
                    recorded_at=receipt_at,
                )
                _append_state_receipt(
                    connection,
                    outbox_id,
                    "cancelled",
                    receipt_at,
                    receipt_json,
                )
                cancelled.append(outbox_id)
            elif row["status"] not in {"cancelled", "failed_terminal", "manually_resolved", "completed"}:
                reconciliation.append(outbox_id)
    return ApprovalRevocationResult(
        approval_id=approval_id,
        cancelled_outbox_ids=tuple(cancelled),
        reconciliation_required_outbox_ids=tuple(reconciliation),
    )


def verify_qualified_admission(
    db_path: Path,
    *,
    outbox_id: str,
    observation: ActuationPreflightObservation,
    approval_policy_version: str,
    scope_enabled: bool,
    rule_enabled: bool,
    now: datetime,
) -> QualifiedDispatchAdmission:
    current_time = _aware_utc(now)
    with open_program_db(db_path, durability="strict") as connection:
        connection.executescript(_APPROVAL_SCHEMA_SQL)
        connection.execute("BEGIN IMMEDIATE")
        return _verify_in_transaction(
            connection,
            outbox_id=outbox_id,
            observation=observation,
            approval_policy_version=approval_policy_version,
            scope_enabled=scope_enabled,
            rule_enabled=rule_enabled,
            now=current_time,
        )


def mark_qualified_dispatched_with_workspace_lease(
    db_path: Path,
    observed_entry: OutboxEntry,
    *,
    workspace_lease: LeaseHandle,
    observation: ActuationPreflightObservation,
    approval_policy_version: str,
    scope_enabled: bool,
    rule_enabled: bool,
    programs_root: Path,
    now: datetime,
) -> QualifiedDispatchAdmission:
    current_time = _aware_utc(now)
    with open_unit_of_work({
        "outbox": db_path,
        "fence": get_workspace_lease_db_path(workspace_lease.program_id, programs_root=programs_root),
    }) as transaction:
        fence = transaction.connection.execute(
            "SELECT owner, fencing_token, acquired_at, expires_at "
            "FROM fence.workspace_lease WHERE mutation_domain = ?",
            (workspace_lease.mutation_domain,),
        ).fetchone()
        if (
            workspace_lease.mutation_domain != ACTUATION_DISPATCH_DOMAIN
            or workspace_lease.acquired_at.utcoffset() is None
            or workspace_lease.expires_at.utcoffset() is None
            or fence is None
            or fence["owner"] != workspace_lease.owner
            or int(fence["fencing_token"]) != workspace_lease.fencing_token
            or fence["acquired_at"] != _wire(workspace_lease.acquired_at)
            or fence["expires_at"] != _wire(workspace_lease.expires_at)
            or workspace_lease.expires_at <= current_time
        ):
            from src.core.ledger.durable_outbox_store import LeaseNoLongerCurrent

            raise LeaseNoLongerCurrent(observed_entry.outbox_id)
        checked = _verify_in_transaction(
            transaction.connection,
            outbox_id=observed_entry.outbox_id,
            observation=observation,
            approval_policy_version=approval_policy_version,
            scope_enabled=scope_enabled,
            rule_enabled=rule_enabled,
            now=current_time,
        )
        if not checked.allowed:
            return checked
        current = checked.entry
        if (
            current != observed_entry
            or current.status != "leased"
            or current.lease_owner != workspace_lease.owner
            or current.lease_expires_at is None
            or current.lease_expires_at <= current_time
        ):
            from src.core.ledger.durable_outbox_store import LeaseNoLongerCurrent

            raise LeaseNoLongerCurrent(observed_entry.outbox_id)
        row = transaction.connection.execute(
            "UPDATE outbox SET status = 'dispatched', remote_request_hash = NULL, updated_at = ? "
            f"WHERE outbox_id = ? RETURNING {_OUTBOX_SELECT}",
            (_wire(current_time), observed_entry.outbox_id),
        ).fetchone()
    return QualifiedDispatchAdmission(allowed=True, entry=_entry_from_row(tuple(row)))


def _verify_in_transaction(
    connection,
    *,
    outbox_id: str,
    observation: ActuationPreflightObservation,
    approval_policy_version: str,
    scope_enabled: bool,
    rule_enabled: bool,
    now: datetime,
) -> QualifiedDispatchAdmission:
    row = connection.execute(
        f"SELECT {_OUTBOX_SELECT} FROM outbox WHERE outbox_id = ?",
        (outbox_id,),
    ).fetchone()
    if row is None:
        raise OutboxError(f"unknown outbox row {outbox_id!r}")
    entry = _entry_from_row(tuple(row))
    if entry.status == "cancelled":
        return QualifiedDispatchAdmission(False, entry, entry.failure_reason)
    admission = connection.execute(
        "SELECT * FROM outbox_admissions WHERE outbox_id = ?",
        (outbox_id,),
    ).fetchone()
    if admission is None:
        return _block_in_transaction(
            connection, entry, "Approval admission evidence is missing", cancelled=False, now=now,
        )
    approval = connection.execute(
        """
        SELECT a.*, c.approval_id AS current_approval_id, c.revision AS current_revision
        FROM outbox_approvals a
        LEFT JOIN outbox_current_approvals c ON c.intent_id = a.intent_id
        WHERE a.approval_id = ? AND a.intent_id = ?
        """,
        (admission["approval_id"], admission["intent_id"]),
    ).fetchone()
    failure, blocked_status = _admission_failure(
        entry=entry,
        admission=admission,
        approval=approval,
        observation=observation,
        approval_policy_version=approval_policy_version,
        scope_enabled=scope_enabled,
        rule_enabled=rule_enabled,
        now=now,
    )
    if failure is not None:
        return _block_in_transaction(
            connection, entry, failure, cancelled=blocked_status == "cancelled", now=now,
        )
    rate = connection.execute(
        "SELECT rate_key FROM outbox_rate_reservations WHERE outbox_id = ?",
        (outbox_id,),
    ).fetchone()
    if rate is None or rate["rate_key"] != admission["rate_key"]:
        return _block_in_transaction(
            connection, entry, "Rate reservation is missing", cancelled=False, now=now,
        )
    rate_count = int(connection.execute(
        "SELECT COUNT(*) FROM outbox_rate_reservations WHERE rate_key = ?",
        (admission["rate_key"],),
    ).fetchone()[0])
    if rate_count > int(admission["rate_limit"]):
        return _block_in_transaction(
            connection, entry, "Rate reservation capacity is exceeded", cancelled=False, now=now,
        )
    effect = connection.execute(
        "SELECT outbox_id FROM outbox_effects WHERE effect_id = ?",
        (admission["semantic_effect_key"],),
    ).fetchone()
    if effect is None or effect["outbox_id"] != outbox_id:
        return _block_in_transaction(
            connection, entry, "Current effect reservation is missing or changed", cancelled=False,
            now=now,
        )
    marker = approved_remote_marker(
        operation=str(admission["operation"]),
        semantic_effect_key=str(admission["semantic_effect_key"]),
    )
    if marker is not None:
        marker_reservation = connection.execute(
            "SELECT outbox_id, semantic_effect_key "
            "FROM outbox_marker_reservations WHERE marker_value = ?",
            (marker,),
        ).fetchone()
        if (
            marker_reservation is None
            or marker_reservation["outbox_id"] != outbox_id
            or marker_reservation["semantic_effect_key"] != admission["semantic_effect_key"]
        ):
            return _block_in_transaction(
                connection,
                entry,
                "Approved marker reservation is missing or changed",
                cancelled=False,
                now=now,
            )
    conflict = connection.execute(
        f"""
        SELECT a.outbox_id
        FROM outbox_admissions a JOIN outbox o ON o.outbox_id = a.outbox_id
        WHERE a.provider = ? AND a.tenant = ? AND a.project = ? AND a.target_identity = ?
          AND a.outbox_id <> ? AND o.status IN ({",".join("?" for _ in _ACTIVE_STATUSES)})
        LIMIT 1
        """,
        (
            admission["provider"], admission["tenant"], admission["project"],
            admission["target_identity"], outbox_id, *_ACTIVE_STATUSES,
        ),
    ).fetchone()
    if conflict is not None:
        return _block_in_transaction(
            connection, entry, "Conflicting active intent is present", cancelled=False, now=now,
        )
    return QualifiedDispatchAdmission(True, entry)


def _admission_failure(
    *,
    entry: OutboxEntry,
    admission,
    approval,
    observation: ActuationPreflightObservation,
    approval_policy_version: str,
    scope_enabled: bool,
    rule_enabled: bool,
    now: datetime,
) -> tuple[str | None, str]:
    if type(scope_enabled) is not bool or not scope_enabled:
        return "Actuation scope is disabled", "cancelled"
    if type(rule_enabled) is not bool or not rule_enabled:
        return "Actuation rule is disabled", "cancelled"
    if approval is None:
        return "Approval admission identity is missing", "cancelled"
    if approval_policy_version != approval["approval_policy_version"]:
        return "Approval policy version is not current", "cancelled"
    if approval["revoked_at"] is not None:
        return "Approval was revoked before dispatch", "cancelled"
    if (
        approval["current_approval_id"] != admission["approval_id"]
        or approval["current_revision"] is None
        or int(approval["current_revision"]) != int(admission["approval_revision"])
    ):
        return "Approval revision is not current", "cancelled"
    if now >= _parse(approval["expires_at"]):
        return "Approval has expired", "cancelled"
    if now >= _parse(approval["evidence_expires_at"]):
        return "Evidence snapshot has expired", "cancelled"
    if observation.evidence_snapshot_id != approval["evidence_snapshot_id"]:
        return "Evidence snapshot is not the approved snapshot", "cancelled"
    if approval["semantic_effect_key"] != admission["semantic_effect_key"]:
        return "Approval semantic effect changed", "cancelled"
    payload = _canonical_payload(entry.payload_json)
    retained_semantic = payload.pop("semantic_effect_id", None)
    retained_target = payload.pop("target_identity", None)
    retained_causal = payload.pop("causal_occurrence_id", None)
    if retained_semantic != admission["semantic_effect_key"]:
        return "Retained payload semantic effect identity is missing or changed", "cancelled"
    if retained_target != admission["target_identity"]:
        return "Retained target identity is missing or changed", "cancelled"
    if retained_causal != admission["causal_occurrence_id"]:
        return "Retained causal occurrence is missing or changed", "cancelled"
    approved_payload_json = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    if (
        _payload_hash(entry.payload_json) != admission["payload_sha256"]
        and _payload_hash(approved_payload_json) != admission["payload_sha256"]
    ):
        return "Retained payload hash does not match approval", "cancelled"
    if approval["payload_sha256"] != admission["payload_sha256"]:
        return "Approval payload identity changed", "cancelled"
    desired = payload.get("desired_effect")
    program_id = payload.get("program_id")
    if not isinstance(desired, Mapping):
        return "Retained authorized desired effect is missing", "failed_terminal"
    if not isinstance(program_id, str) or not program_id.strip():
        return "Retained approval program identity is missing", "failed_terminal"
    request = ActuationAdmissionRequest(
        program_id=program_id,
        intent_id=str(admission["intent_id"]),
        proposal_id="retained-admission",
        provider=str(admission["provider"]),
        tenant=str(admission["tenant"]),
        project=str(admission["project"]),
        operation=str(admission["operation"]),
        target_identity=str(admission["target_identity"]),
        causal_occurrence_id=str(admission["causal_occurrence_id"]),
        desired_effect=desired,
        payload_json=entry.payload_json,
        approval_policy_version=str(approval["approval_policy_version"]),
        rate_scope=str(admission["rate_key"]),
        rate_limit=int(admission["rate_limit"]),
        blast_radius=int(admission["blast_radius"]),
        scope_enabled=True,
        rule_enabled=True,
        expected_revision=(
            int(admission["expected_revision"])
            if admission["expected_revision"] is not None
            else None
        ),
        expected_state=admission["expected_state"],
        evidence_snapshot_id=str(approval["evidence_snapshot_id"]),
        evidence_expires_at=_parse(approval["evidence_expires_at"]),
    )
    if (
        not isinstance(observation.evidence_snapshot_id, str)
        or not observation.evidence_snapshot_id.strip()
        or type(observation.target_count) is not int
        or observation.target_count < 0
        or type(observation.marker_found) is not bool
        or (
            observation.current_revision is not None
            and (type(observation.current_revision) is not int or observation.current_revision <= 0)
        )
        or (observation.current_state is not None and not isinstance(observation.current_state, str))
        or not isinstance(observation.observed_at, datetime)
        or observation.observed_at.utcoffset() is None
    ):
        return "Preflight observation is malformed", "failed_terminal"
    if observation.observed_at > now:
        return "Preflight observation is future-dated", "failed_terminal"
    capability = lookup_capability(admission["operation"])
    if isinstance(capability, CapabilityRefusal):
        return "Operation capability is not qualified", "failed_terminal"
    if int(admission["blast_radius"]) > capability.max_blast_radius:
        return "Blast radius exceeds operation capability", "failed_terminal"
    binding_failure = _authorized_payload_failure(
        request=request,
        payload_json=entry.payload_json,
        approval_id=str(admission["approval_id"]),
        approval_principal=str(approval["approver_principal"]),
        approval_policy_version=str(approval["approval_policy_version"]),
        evidence_snapshot_id=str(approval["evidence_snapshot_id"]),
    )
    if binding_failure is not None:
        return binding_failure, "failed_terminal"
    expected_target_count = 0 if capability.effect_class is EffectClass.CREATE else 1
    if observation.target_count != expected_target_count:
        return "Target identity is not unique", "cancelled"
    if admission["expected_revision"] is not None:
        if observation.current_revision != int(admission["expected_revision"]):
            return "Current revision does not match the approval precondition", "cancelled"
    if admission["expected_state"] is not None:
        if observation.current_state != admission["expected_state"]:
            return "Current state does not match the approval precondition", "cancelled"
    if observation.marker_found:
        return "Existing marker or remote effect requires reconciliation", "uncertain_remote_state"
    return None, ""
def _block_in_transaction(
    connection, entry: OutboxEntry, reason: str, *, cancelled: bool, now: datetime,
) -> QualifiedDispatchAdmission:
    status = "cancelled" if cancelled else "failed_terminal"
    if status == "cancelled" and entry.status in {"dispatched", "succeeded", "audited"}:
        status = "uncertain_remote_state"
    if "marker" in reason.casefold() or "remote effect" in reason.casefold():
        status = "uncertain_remote_state"
    connection.execute(
        "UPDATE outbox SET status = ?, failure_reason = ?, updated_at = ? WHERE outbox_id = ?",
        (status, reason, _wire(now), entry.outbox_id),
    )
    if status == "cancelled":
        connection.execute(
            "DELETE FROM outbox_rate_reservations WHERE outbox_id = ?",
            (entry.outbox_id,),
        )
        connection.execute(
            "DELETE FROM outbox_marker_reservations WHERE outbox_id = ?",
            (entry.outbox_id,),
        )
    row = connection.execute(
        f"SELECT {_OUTBOX_SELECT} FROM outbox WHERE outbox_id = ?",
        (entry.outbox_id,),
    ).fetchone()
    blocked = _entry_from_row(tuple(row))
    recorded_at = _wire(now)
    assert recorded_at is not None
    receipt_json = _state_receipt(
        outbox_id=blocked.outbox_id,
        domain=blocked.domain,
        correlation_id=blocked.correlation_id,
        payload_json=blocked.payload_json,
        terminal_status=blocked.status,
        owner=blocked.lease_owner or "admission",
        fencing_token=blocked.lease_fencing_token,
        attempt_count=blocked.attempt_count,
        reason=reason,
        recorded_at=recorded_at,
    )
    _append_state_receipt(
        connection,
        blocked.outbox_id,
        blocked.status,
        recorded_at,
        receipt_json,
    )
    return QualifiedDispatchAdmission(False, blocked, reason)


__all__ = [
    "ActuationAdmissionRequest",
    "ActuationApproval",
    "ActuationPreflightObservation",
    "ApprovalRevocationResult",
    "QualifiedDispatchAdmission",
    "create_actuation_approval",
    "enqueue_approved_actuation_intent",
    "load_marker_outbox_id",
    "mark_qualified_dispatched_with_workspace_lease",
    "revoke_actuation_approval",
    "verify_qualified_admission",
]
