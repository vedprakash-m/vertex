"""Ledger compliance redaction (§10.8).

The ONLY physical mutation in the ledger system, explicitly audited to prevent
silent content destruction.  Chain continuity is preserved via the redaction
registry's ``original_envelope_hash``, so ``verify_event_log`` stays valid after
content is destroyed.

Concurrency model: all writes use portalocker exclusive lock over the event file.
Zone boundary: Zone A (no ai/m365 imports).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

import portalocker

from src.core.config_loader import PROGRAMS_ROOT
from src.core.jsonl_utils import append_jsonl_line


REDACTIONS_FILENAME = ".redactions.jsonl"


@dataclass(frozen=True, slots=True)
class EventRedactionRecord:
    event_id: str
    original_envelope_hash: str
    redacted_at: datetime
    actor: str
    reason: str


def get_redaction_registry_path(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    return programs_root / program_id / "ledger" / "events" / REDACTIONS_FILENAME


def load_redaction_registry(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> dict[str, EventRedactionRecord]:
    path = get_redaction_registry_path(program_id, programs_root=programs_root)
    if not path.exists():
        return {}
    records: dict[str, EventRedactionRecord] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
            record = _record_from_payload(payload)
            records[record.event_id] = record
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    return records


def _record_from_payload(payload: dict[str, Any]) -> EventRedactionRecord:
    event_id = payload["event_id"]
    original_envelope_hash = payload["original_envelope_hash"]
    redacted_at_raw = payload["redacted_at"]
    actor = payload["actor"]
    reason = payload["reason"]
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("event_id must be a non-empty string")
    if not isinstance(original_envelope_hash, str) or not original_envelope_hash:
        raise ValueError("original_envelope_hash must be a non-empty string")
    if not isinstance(actor, str) or not actor:
        raise ValueError("actor must be a non-empty string")
    if not isinstance(reason, str):
        raise ValueError("reason must be a string")
    if not isinstance(redacted_at_raw, str):
        raise ValueError("redacted_at must be a string")
    redacted_at = datetime.fromisoformat(redacted_at_raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    return EventRedactionRecord(
        event_id=event_id,
        original_envelope_hash=original_envelope_hash,
        redacted_at=redacted_at,
        actor=actor,
        reason=reason,
    )


def _append_redaction_record(
    program_id: str,
    record: EventRedactionRecord,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    path = get_redaction_registry_path(program_id, programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "event_id": record.event_id,
        "original_envelope_hash": record.original_envelope_hash,
        "redacted_at": record.redacted_at.isoformat().replace("+00:00", "Z"),
        "actor": record.actor,
        "reason": record.reason,
    }
    append_jsonl_line(path, json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


def redact_event(
    program_id: str,
    event_id: str,
    *,
    actor: str,
    reason: str,
    scrub_source_fields: tuple[str, ...] | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> EventRedactionRecord | None:
    """Redact a single event in-place (§10.8).

    Finds the event line in its JSONL file, computes the original envelope hash,
    rewrites the file atomically replacing the payload with ``{"redacted": true}``,
    then appends a redaction registry record.

    Returns the new ``EventRedactionRecord`` on success, or None if the event_id was
    not found (the caller should distinguish this from an already-redacted event).

    Raises ``ValueError`` if the event is already present in the redaction registry.
    """
    existing = load_redaction_registry(program_id, programs_root=programs_root)
    if event_id in existing:
        raise ValueError(f"Event {event_id!r} is already redacted.")

    events_dir = programs_root / program_id / "ledger" / "events"
    if not events_dir.exists():
        return None

    for event_file in sorted(events_dir.glob("*.events.jsonl")):
        result = _try_redact_in_file(event_file, event_id, scrub_source_fields=scrub_source_fields)
        if result is None:
            continue
        original_envelope_hash, _ = result
        record = EventRedactionRecord(
            event_id=event_id,
            original_envelope_hash=original_envelope_hash,
            redacted_at=datetime.now(timezone.utc),
            actor=actor,
            reason=reason,
        )
        _append_redaction_record(program_id, record, programs_root=programs_root)
        return record

    return None


def _try_redact_in_file(
    event_file: Path,
    event_id: str,
    *,
    scrub_source_fields: tuple[str, ...] | None,
) -> tuple[str, int] | None:
    """Atomically rewrites the event file replacing the target event's payload.

    Returns (original_envelope_hash, line_no) if found, None otherwise.
    Holds portalocker exclusive lock for the entire read-rewrite-fsync operation.
    """
    lock_path = event_file.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with portalocker.Lock(str(lock_path), timeout=30, flags=portalocker.LOCK_EX):
        lines = event_file.read_text(encoding="utf-8").splitlines(keepends=True)
        target_line_no: int | None = None
        original_envelope_hash: str | None = None

        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("event_id") != event_id:
                continue
            target_line_no = i
            original_envelope_hash = _sha256_hex(json.dumps(payload, sort_keys=True, separators=(",", ":")))
            break

        if target_line_no is None or original_envelope_hash is None:
            return None

        new_lines = list(lines)
        original_payload = json.loads(lines[target_line_no].strip())
        redacted_payload = dict(original_payload)
        redacted_payload["payload"] = {"redacted": True}
        if scrub_source_fields:
            for field in scrub_source_fields:
                for ref_key in ("source_ref", "corroborating_refs"):
                    if ref_key == "source_ref" and isinstance(redacted_payload.get(ref_key), dict):
                        ref = dict(redacted_payload[ref_key])
                        ref.pop(field, None)
                        redacted_payload[ref_key] = ref
                    elif ref_key == "corroborating_refs" and isinstance(redacted_payload.get(ref_key), list):
                        redacted_payload[ref_key] = [
                            {k: v for k, v in r.items() if k != field} if isinstance(r, dict) else r
                            for r in redacted_payload[ref_key]
                        ]

        redacted_line = json.dumps(redacted_payload, sort_keys=True, separators=(",", ":")) + "\n"
        new_lines[target_line_no] = redacted_line

        tmp_path = event_file.with_suffix(".tmp_redact")
        tmp_path.write_text("".join(new_lines), encoding="utf-8")
        import os
        with open(tmp_path, "r+b") as f:
            f.flush()
            os.fsync(f.fileno())
        tmp_path.replace(event_file)
        return original_envelope_hash, target_line_no


def _sha256_hex(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8"))
    return f"sha256:{digest.hexdigest()}"
