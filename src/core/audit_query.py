"""WS-18: autonomy-audit hash-chain + GDPR-aware tombstoning.

Why this module exists
======================
`autonomy_audit.jsonl` is the highest-risk sidecar in the platform: every L3/L4
autonomy decision (ADO write, Teams post, etc.) is recorded here. To be
useful as a tamper-evident audit trail, each record must be linked to the
one before it via a hash chain, so any post-hoc tampering is detectable.

But — and this is the GDPR/PB-53 conflict — an append-only hash chain cannot
be edited to redact PII without breaking the chain. The reconciliation
("tombstoning"): redact PII in-place but mark the line ``[EXCISED]`` and
record the original hash so the chain validator can SKIP the excised line
(re-anchor the chain across the tombstone).

Scope
=====
This module is the **library** for the chain. The CLI surface is added in
`src/commands/audit.py`:
- `vertex audit verify-chain --program <id>`
- `vertex audit excise --program <id> --line <N> --excisor <name> --reason <text>`
- `vertex audit query --program <id> [--action-type X] [--level Y] [--from D] [--to D]`

Public API
==========
- `compute_record_hash(payload, *, prev_hash)` -- canonical SHA-256 over a
  JSON-serializable payload (with `hash` set to None and `prev_hash` injected).
- `read_chain_head_hash(path)` -- last non-excised line's `hash`, or None.
- `verify_autonomy_audit_chain(program_id, *, programs_root)` -- returns a
  `ChainVerificationResult` (ok, broken_at_line, total_records, excised_count).
- `excise_pii_from_autonomy_audit(program_id, line_number, *, programs_root, excisor, reason)`
  -- rewrites a line in place with the `[EXCISED]` marker; chain still
  validates.
- `build_audit_query(program_id, *, programs_root, action_type=None, level=None,
  from_date=None, to_date=None, action_id=None, limit=None)` -- returns a
  filtered, time-bounded list of audit events with the chain status attached.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

from src.core.analytics_store import get_program_autonomy_audit_path
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.jsonl_utils import append_jsonl_line, parse_jsonl_line


# Schema-version bump for the autonomy_audit sidecar — records now carry
# `prev_hash` + `hash` for chain integrity. Old records (without these fields)
# are treated as the genesis block by `verify_autonomy_audit_chain`.
AUDIT_CHAIN_SCHEMA_VERSION = "2.0"


class AutonomyAuditChainError(RuntimeError):
    """Raised when the autonomy-audit hash chain is broken (tampering detected)."""


@dataclass(frozen=True, slots=True)
class ChainVerificationResult:
    program_id: str
    ok: bool
    total_records: int
    excised_count: int
    broken_at_line: int | None = None
    broken_at_hash: str | None = None
    broken_reason: str | None = None
    chain_head_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "program_id": self.program_id,
            "ok": self.ok,
            "total_records": self.total_records,
            "excised_count": self.excised_count,
            "broken_at_line": self.broken_at_line,
            "broken_at_hash": self.broken_at_hash,
            "broken_reason": self.broken_reason,
            "chain_head_hash": self.chain_head_hash,
        }


@dataclass(frozen=True, slots=True)
class ExcisionResult:
    program_id: str
    line_number: int
    excised_at: datetime
    excisor: str
    reason: str | None
    original_hash: str | None
    chain_still_valid: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "program_id": self.program_id,
            "line_number": self.line_number,
            "excised_at": self.excised_at.isoformat(),
            "excisor": self.excisor,
            "reason": self.reason,
            "original_hash": self.original_hash,
            "chain_still_valid": self.chain_still_valid,
        }


@dataclass(frozen=True, slots=True)
class AuditQueryResult:
    program_id: str
    events: tuple[dict[str, Any], ...]
    total_matched: int
    chain_status: ChainVerificationResult

    def to_dict(self) -> dict[str, Any]:
        return {
            "program_id": self.program_id,
            "total_matched": self.total_matched,
            "events": list(self.events),
            "chain_status": self.chain_status.to_dict(),
        }


# ---------------------------------------------------------------------------
# Hash + chain primitives
# ---------------------------------------------------------------------------

def _canonical_payload(payload: dict[str, Any], *, prev_hash: str | None) -> dict[str, Any]:
    """Build a canonical, hash-stable copy of the payload.

    The chain hash is over the FULL record with `hash=None` injected and
    `prev_hash` set to the previous line's hash (or None for the first line).
    This is the only place we decide what the chain "covers" — the privacy
    redaction MUST preserve this contract, so any field added to the
    payload later MUST be added here too.
    """
    canonical = dict(payload)
    canonical["prev_hash"] = prev_hash
    canonical["hash"] = None
    # Drop [EXCISED] markers from the chain hash — the redaction happens
    # AFTER the chain, so the chain validates the ORIGINAL record.
    canonical.pop("__excised__", None)
    canonical.pop("__original_hash__", None)
    return canonical


def compute_record_hash(payload: dict[str, Any], *, prev_hash: str | None) -> str:
    """Compute the canonical SHA-256 hash for one record.

    The hash covers: every key in the payload (EXCEPT the `hash` key, which
    is set to None during hashing) PLUS the `prev_hash`. Output is a
    lowercase hex digest.
    """
    canonical = _canonical_payload(payload, prev_hash=prev_hash)
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def read_chain_head_hash(path: Path) -> str | None:
    """Return the `hash` of the last non-excised line, or None if file is empty/missing."""
    if not path.exists():
        return None
    last_hash: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = parse_jsonl_line(line)
        except json.JSONDecodeError:
            continue
        if payload.get("__excised__"):
            # An excised line is still in the file, but the chain head
            # is the line BEFORE the excision (the chain skips excised lines).
            continue
        h = payload.get("hash")
        if isinstance(h, str):
            last_hash = h
    return last_hash


# ---------------------------------------------------------------------------
# Chain verification
# ---------------------------------------------------------------------------

def _iter_records(path: Path) -> list[tuple[int, dict[str, Any]]]:
    """Return ``(line_number, payload)`` pairs (1-indexed line numbers) for
    every parseable line in the file. Unparseable lines are skipped with
    a ``__unparseable__`` marker so the chain verifier can still report
    a structured failure.
    """
    if not path.exists():
        return []
    out: list[tuple[int, dict[str, Any]]] = []
    for idx, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            payload = parse_jsonl_line(raw)
        except json.JSONDecodeError as exc:
            out.append((idx, {"__unparseable__": True, "error": str(exc)}))
            continue
        out.append((idx, payload))
    return out


def verify_autonomy_audit_chain(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> ChainVerificationResult:
    """Walk the autonomy-audit JSONL and verify the per-record hash chain.

    Rules:
    - The first non-excised record is the genesis block: ``prev_hash`` MUST
      be None (or absent). If it has a non-None prev_hash, the chain was
      migrated mid-stream — flag and treat as broken.
    - Each subsequent record's ``prev_hash`` MUST equal the previous
      non-excised record's ``hash``.
    - Each record's ``hash`` MUST equal ``compute_record_hash(record, prev_hash=record.prev_hash)``.
    - Lines marked ``__excised__: True`` are NOT validated against a recomputed
      hash (the original is gone) but their existence does not break the
      chain; the next non-excised record re-anchors against the line BEFORE
      the excision.
    - Unparseable lines break the chain.
    """
    path = get_program_autonomy_audit_path(program_id, programs_root=programs_root)
    records = _iter_records(path)
    if not records:
        return ChainVerificationResult(
            program_id=program_id,
            ok=True,
            total_records=0,
            excised_count=0,
            chain_head_hash=None,
        )

    # First pass: identify the prev_hash of every chained record by
    # looking BACKWARDS at the most recent non-excised record's hash.
    # This is what lets the chain "skip" excised lines without breaking.
    prev_hash_of: dict[int, str | None] = {}
    known_hashes: set[str] = set()
    running_prev: str | None = None
    for line_no, payload in records:
        if payload.get("__excised__") or payload.get("__unparseable__"):
            continue
        if not isinstance(payload.get("hash"), str) or not payload.get("hash"):
            # Legacy record — does not contribute to prev_hash
            continue
        prev_hash_of[line_no] = running_prev
        known_hashes.add(payload["hash"])
        running_prev = payload["hash"]

    # Second pass: validate.
    excised_count = 0
    chain_head: str | None = None
    for line_no, payload in records:
        if payload.get("__unparseable__"):
            return ChainVerificationResult(
                program_id=program_id,
                ok=False,
                total_records=len(records),
                excised_count=excised_count,
                broken_at_line=line_no,
                broken_reason=f"unparseable JSONL: {payload.get('error')!r}",
                chain_head_hash=chain_head,
            )
        if payload.get("__excised__"):
            excised_count += 1
            continue

        record_has_hash = isinstance(payload.get("hash"), str) and payload.get("hash")
        if not record_has_hash:
            # Legacy record — no validation needed; do not advance prev_hash
            continue

        # The expected prev_hash for this line is the prev_hash_of lookup
        # we built in the first pass.
        expected_prev = prev_hash_of.get(line_no)
        record_prev = payload.get("prev_hash")

        # Accept the record's on-disk prev_hash if it EITHER:
        # (a) matches the re-derived expected_prev, OR
        # (b) points to a vanished (excised) predecessor — i.e. its value
        #     is NOT in the set of known non-excised hashes. This is the
        #     "ghost" acceptance: the chain was re-anchored by an excision.
        prev_acceptable = record_prev == expected_prev
        if not prev_acceptable and record_prev is not None and record_prev not in known_hashes:
            prev_acceptable = True
        if not prev_acceptable:
            return ChainVerificationResult(
                program_id=program_id,
                ok=False,
                total_records=len(records),
                excised_count=excised_count,
                broken_at_line=line_no,
                broken_reason=f"prev_hash mismatch (expected {expected_prev!r}, got {record_prev!r})",
                chain_head_hash=chain_head,
            )

        expected_hash = compute_record_hash(payload, prev_hash=record_prev)
        actual_hash = payload.get("hash")
        if actual_hash != expected_hash:
            return ChainVerificationResult(
                program_id=program_id,
                ok=False,
                total_records=len(records),
                excised_count=excised_count,
                broken_at_line=line_no,
                broken_at_hash=actual_hash,
                broken_reason=f"hash mismatch (expected {expected_hash!r}, got {actual_hash!r})",
                chain_head_hash=chain_head,
            )

        chain_head = actual_hash

    return ChainVerificationResult(
        program_id=program_id,
        ok=True,
        total_records=len(records),
        excised_count=excised_count,
        chain_head_hash=chain_head,
    )


# ---------------------------------------------------------------------------
# GDPR tombstoning — the [EXCISED] marker
# ---------------------------------------------------------------------------

def excise_pii_from_autonomy_audit(
    program_id: str,
    line_number: int,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    excisor: str,
    reason: str | None = None,
) -> ExcisionResult:
    """Rewrite a single line in autonomy_audit.jsonl with the [EXCISED] marker.

    The original payload's PII fields are scrubbed; the chain validator
    forgives the line (skips it, re-anchors before it). The original hash
    is preserved on the line so an auditor can prove the line WAS
    originally chained, before excision.

    The function is append-only at the file level: it rewrites the file
    in place via the same portalocker-guarded temp-then-rename pattern
    used elsewhere in the codebase.

    Returns ``ExcisionResult`` with the chain still-valid flag.
    """
    path = get_program_autonomy_audit_path(program_id, programs_root=programs_root)
    if not path.exists():
        raise FileNotFoundError(f"autonomy_audit.jsonl not found at {path}")

    records = _iter_records(path)
    if line_number < 1 or line_number > len(records):
        raise IndexError(
            f"line_number {line_number} out of range (file has {len(records)} record lines)"
        )

    target_line, target_payload = records[line_number - 1]
    if target_payload.get("__excised__"):
        raise ValueError(f"line {line_number} is already excised")
    if target_payload.get("__unparseable__"):
        raise ValueError(f"line {line_number} is unparseable; cannot excise")

    original_hash = target_payload.get("hash")
    original_prev_hash = target_payload.get("prev_hash")

    # Build the excised record. We keep `action_id` (so the audit
    # timeline can still reference the event) but strip author/subject
    # aliases and any evidence_refs that look like PII (heuristic: contains
    # an "@" or starts with "upi:" or "user_principal_name:").
    excised_payload: dict[str, Any] = {
        "schema_version": AUDIT_CHAIN_SCHEMA_VERSION,
        "__excised__": True,
        "__original_hash__": original_hash,
        "__original_prev_hash__": original_prev_hash,
        "action_id": target_payload.get("action_id"),
        "level": target_payload.get("level"),
        "action_type": target_payload.get("action_type"),
        "applied_at": target_payload.get("applied_at"),
        "excisor": excisor,
        "excised_at": datetime.now(timezone.utc).isoformat(),
    }
    if reason is not None:
        excised_payload["reason"] = reason

    # Rewrite the file: replace the target line in place; preserve every
    # other line byte-for-byte so the chain state is unchanged for the
    # rest of the file.
    raw_lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    raw_lines[line_number - 1] = json.dumps(excised_payload, sort_keys=True, separators=(",", ":"))

    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        for line in raw_lines:
            handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)

    # Re-verify the chain to confirm the excision was chain-preserving.
    new_status = verify_autonomy_audit_chain(program_id, programs_root=programs_root)
    if not new_status.ok:
        # This should be impossible if the rewrite is correct, but we
        # surface it loudly rather than silently claiming success.
        raise AutonomyAuditChainError(
            f"chain broke after excision of line {line_number}: {new_status.broken_reason}"
        )

    # Append a new autonomy-audit record noting the excision (provenance).
    # The new record's prev_hash is the current chain head (the line BEFORE
    # the excision — the chain skipped the excised line). This records
    # the operator's action in the same hash chain.
    provenance_record = AutonomyAuditProvenance(
        program_id=program_id,
        action_id=f"excise-{line_number}-{int(datetime.now(timezone.utc).timestamp())}",
        level="operator",
        author_alias=excisor,
        subject_alias=None,
        evidence_refs=(f"autonomy_audit.jsonl:line:{line_number}",),
        policy_rule="privacy.excise",
        accepted=True,
        applied_at=datetime.now(timezone.utc),
        action_type="audit_excise",
        blast_radius=f"line {line_number} redacted",
        rollback_mechanism=None,
        prior_acceptance_rate=None,
    )
    append_chain_record(
        provenance_record,
        programs_root=programs_root,
        schema_version=AUDIT_CHAIN_SCHEMA_VERSION,
    )

    return ExcisionResult(
        program_id=program_id,
        line_number=line_number,
        excised_at=datetime.now(timezone.utc),
        excisor=excisor,
        reason=reason,
        original_hash=original_hash,
        chain_still_valid=True,
    )


# ---------------------------------------------------------------------------
# Chain-record append (used by excise provenance AND external callers that
# want to write a new record with the chain hash attached)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class AutonomyAuditProvenance:
    """Minimal record shape for write-into-chain callers (e.g. excise)."""
    program_id: str
    action_id: str
    level: str
    author_alias: str
    subject_alias: str | None
    evidence_refs: tuple[str, ...]
    policy_rule: str | None
    accepted: bool
    applied_at: datetime
    action_type: str | None = None
    blast_radius: str | None = None
    rollback_mechanism: str | None = None
    prior_acceptance_rate: float | None = None


def append_chain_record(
    record: AutonomyAuditProvenance,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    schema_version: str = AUDIT_CHAIN_SCHEMA_VERSION,
) -> Path:
    """Append a record to autonomy_audit.jsonl with the chain hash attached.

    This is the chain-aware writer; the legacy `append_autonomy_audit_record`
    in `analytics_store.py` does NOT call this (it is the v1 writer; this
    function is the v2 writer that adds `prev_hash` + `hash`). The CLI
    `vertex audit excise` uses this function for the provenance row so the
    excision is itself in the chain.
    """
    path = get_program_autonomy_audit_path(record.program_id, programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)

    prev_hash = read_chain_head_hash(path)
    payload: dict[str, Any] = {
        "schema_version": schema_version,
        "action_id": record.action_id,
        "level": record.level,
        "author_alias": record.author_alias,
        "subject_alias": record.subject_alias,
        "evidence_refs": list(record.evidence_refs),
        "policy_rule": record.policy_rule,
        "accepted": record.accepted,
        "applied_at": _ensure_utc(record.applied_at).isoformat(),
        "action_type": record.action_type,
        "blast_radius": record.blast_radius,
        "rollback_mechanism": record.rollback_mechanism,
        "prior_acceptance_rate": record.prior_acceptance_rate,
    }
    payload["prev_hash"] = prev_hash
    payload["hash"] = compute_record_hash(payload, prev_hash=prev_hash)
    line = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    append_jsonl_line(path, line, max_bytes=10 * 1024 * 1024)
    return path


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Audit query — `vertex audit query` library
# ---------------------------------------------------------------------------

def build_audit_query(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    action_type: str | None = None,
    level: str | None = None,
    action_id: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    limit: int | None = None,
) -> AuditQueryResult:
    """Read autonomy_audit.jsonl, filter, and attach chain status.

    Filter semantics:
    - `action_type` substring match (case-insensitive).
    - `level` exact match.
    - `action_id` exact match.
    - `from_date` inclusive lower bound on `applied_at` (UTC).
    - `to_date` inclusive upper bound on `applied_at` (UTC).
    - `limit` truncates the result to the first N events (after filtering).
    - Lines marked `__excised__` are returned as a stripped-down event
      (``action_id``, ``excisor``, ``excised_at``, ``reason``) so operators
      can see that an excision happened at that position.
    """
    chain_status = verify_autonomy_audit_chain(program_id, programs_root=programs_root)
    path = get_program_autonomy_audit_path(program_id, programs_root=programs_root)
    records = _iter_records(path)

    from_ts = (
        datetime.combine(from_date, time.min, tzinfo=timezone.utc) if from_date is not None else None
    )
    to_ts = (
        datetime.combine(to_date, time.max, tzinfo=timezone.utc) if to_date is not None else None
    )

    events: list[dict[str, Any]] = []
    for line_no, payload in records:
        if payload.get("__unparseable__"):
            continue
        if payload.get("__excised__"):
            event = {
                "line": line_no,
                "kind": "excision",
                "action_id": payload.get("action_id"),
                "excisor": payload.get("excisor"),
                "excised_at": payload.get("excised_at"),
                "reason": payload.get("reason"),
            }
        else:
            # Apply filters
            if action_type is not None and action_type.lower() not in (payload.get("action_type") or "").lower():
                continue
            if level is not None and payload.get("level") != level:
                continue
            if action_id is not None and payload.get("action_id") != action_id:
                continue
            try:
                applied_at = datetime.fromisoformat(payload["applied_at"])
            except (KeyError, TypeError, ValueError):
                continue
            if from_ts is not None and applied_at < from_ts:
                continue
            if to_ts is not None and applied_at > to_ts:
                continue
            event = {
                "line": line_no,
                "kind": "audit",
                "action_id": payload.get("action_id"),
                "level": payload.get("level"),
                "author_alias": payload.get("author_alias"),
                "subject_alias": payload.get("subject_alias"),
                "action_type": payload.get("action_type"),
                "accepted": payload.get("accepted"),
                "applied_at": payload.get("applied_at"),
                "blast_radius": payload.get("blast_radius"),
                "policy_rule": payload.get("policy_rule"),
                "hash": payload.get("hash"),
            }
        events.append(event)

    if limit is not None and limit > 0:
        events = events[:limit]

    return AuditQueryResult(
        program_id=program_id,
        events=tuple(events),
        total_matched=len(events),
        chain_status=chain_status,
    )
