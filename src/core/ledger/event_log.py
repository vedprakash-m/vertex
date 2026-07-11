from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from src.core.jsonl_utils import append_jsonl_line, append_jsonl_lines, quarantine_and_rewrite_jsonl
from src.core.ledger.event_index import index_event, index_events
from src.core.ledger.event_types import validate_event_payload
from src.core.ledger.source_refs import SourceRef, source_document_key, source_ref_from_dict, source_ref_to_dict, validate_typed_source_ref
from src.core.ledger.ulid import new_ulid


PROGRAMS_ROOT = Path(__file__).resolve().parents[3] / "programs"
DEFAULT_MAX_BYTES = 10 * 1024 * 1024


class TemporalConfidence(StrEnum):
    EXACT = "exact"
    APPROXIMATE = "approximate"
    ESTIMATED = "estimated"
    RECONSTRUCTED = "reconstructed"


class ConfidenceTier(StrEnum):
    OPERATOR_CONFIRMED = "operator_confirmed"
    SOURCE_AUTHORITATIVE = "source_authoritative"
    AI_EXTRACTED = "ai_extracted"
    INFERRED = "inferred"


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    event_id: str
    program_id: str
    event_type: str
    occurred_at: datetime
    recorded_at: datetime
    temporal_confidence: TemporalConfidence
    confidence: ConfidenceTier
    actor: str
    payload: dict[str, Any]
    source_ref: SourceRef
    corroborating_refs: tuple[SourceRef, ...] = ()
    prev_event_hash: str = ""
    content_hash: str = ""
    dedupe_core_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "event_id": self.event_id,
            "program_id": self.program_id,
            "event_type": self.event_type,
            "occurred_at": _datetime_to_wire(self.occurred_at),
            "recorded_at": _datetime_to_wire(self.recorded_at),
            "temporal_confidence": self.temporal_confidence.value,
            "confidence": self.confidence.value,
            "actor": self.actor,
            "payload": _normalize_json_value(self.payload),
            "source_ref": source_ref_to_dict(self.source_ref),
            "corroborating_refs": [source_ref_to_dict(ref) for ref in self.corroborating_refs],
            "prev_event_hash": self.prev_event_hash,
            "content_hash": self.content_hash,
        }
        if self.dedupe_core_hash is not None:
            payload["dedupe_core_hash"] = self.dedupe_core_hash
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> EventEnvelope:
        corroborating_refs = payload.get("corroborating_refs", [])
        if not isinstance(corroborating_refs, list):
            raise ValueError("corroborating_refs must be a list.")
        source_payload = payload.get("source_ref")
        if not isinstance(source_payload, dict):
            raise ValueError("source_ref must be a mapping.")
        event_id = payload.get("event_id")
        program_id = payload.get("program_id")
        event_type = payload.get("event_type")
        actor = payload.get("actor")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("event_id must be a non-empty string.")
        if not isinstance(program_id, str) or not program_id:
            raise ValueError("program_id must be a non-empty string.")
        if not isinstance(event_type, str) or not event_type:
            raise ValueError("event_type must be a non-empty string.")
        if not isinstance(actor, str) or not actor:
            raise ValueError("actor must be a non-empty string.")
        normalized_payload = payload.get("payload")
        if not isinstance(normalized_payload, dict):
            raise ValueError("payload must be a mapping.")
        return cls(
            event_id=event_id,
            program_id=program_id,
            event_type=event_type,
            occurred_at=_parse_datetime(payload.get("occurred_at"), "occurred_at"),
            recorded_at=_parse_datetime(payload.get("recorded_at"), "recorded_at"),
            temporal_confidence=TemporalConfidence(_required_str(payload, "temporal_confidence")),
            confidence=ConfidenceTier(_required_str(payload, "confidence")),
            actor=actor,
            payload=normalized_payload,
            source_ref=source_ref_from_dict(source_payload),
            corroborating_refs=tuple(source_ref_from_dict(ref) for ref in corroborating_refs if isinstance(ref, dict)),
            prev_event_hash=_required_str(payload, "prev_event_hash"),
            content_hash=_required_str(payload, "content_hash"),
            dedupe_core_hash=_optional_str(payload, "dedupe_core_hash"),
        )


@dataclass(frozen=True, slots=True)
class EventWriteResult:
    envelope: EventEnvelope
    path: Path
    rotated: bool


@dataclass(frozen=True, slots=True)
class EventBatchWriteResult:
    envelopes: tuple[EventEnvelope, ...]
    path: Path
    rotated: bool


@dataclass(frozen=True, slots=True)
class EventLogVerification:
    ok: bool
    checked_event_count: int
    issues: tuple[str, ...]


# ---------------------------------------------------------------------------
# arch-fix.md Phase 1 (CPK event-infra hardening, H7): O(1) append tail cache.
#
# The append path previously re-read and re-parsed the ENTIRE event log on
# every single write (`read_events()` for the hash chain) plus a second
# full-file re-read (`_count_lines()`) for the index line number — O(n) per
# append, O(n^2) over a long-lived log. This tail cache makes the common
# case (single writer, no rotation boundary) O(1): it persists just enough
# state (last event hash, last recorded_at, active file name/size/line-count)
# to append without re-scanning.
#
# The cache is PURELY a write-path performance optimization, never an
# authority: every reader (`read_events`, `verify_event_log`, projections)
# continues to derive truth from the JSONL files themselves, and the cache
# self-heals whenever it's missing, stale, or doesn't name the current
# target file (e.g. a rotation just occurred, another process wrote without
# updating it, or this is the very first write) by falling back to exactly
# the previous full-scan behavior for that one write.
# ---------------------------------------------------------------------------

_TAIL_FILENAME = "_tail.json"
_TAIL_SCHEMA_VERSION = "1"


@dataclass(frozen=True, slots=True)
class _EventLogTail:
    last_event_hash: str
    last_recorded_at: str  # wire-format (ISO8601 UTC, "Z" suffix)
    active_file_name: str
    active_file_size: int
    active_file_line_count: int


def _tail_path(events_dir: Path) -> Path:
    return events_dir / _TAIL_FILENAME


def _read_tail(events_dir: Path) -> _EventLogTail | None:
    path = _tail_path(events_dir)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict) or raw.get("schema_version") != _TAIL_SCHEMA_VERSION:
        return None
    try:
        return _EventLogTail(
            last_event_hash=str(raw["last_event_hash"]),
            last_recorded_at=str(raw["last_recorded_at"]),
            active_file_name=str(raw["active_file_name"]),
            active_file_size=int(raw["active_file_size"]),
            active_file_line_count=int(raw["active_file_line_count"]),
        )
    except (KeyError, ValueError, TypeError):
        return None


def _write_tail(events_dir: Path, tail: _EventLogTail) -> None:
    path = _tail_path(events_dir)
    payload = {
        "schema_version": _TAIL_SCHEMA_VERSION,
        "last_event_hash": tail.last_event_hash,
        "last_recorded_at": tail.last_recorded_at,
        "active_file_name": tail.active_file_name,
        "active_file_size": tail.active_file_size,
        "active_file_line_count": tail.active_file_line_count,
    }
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(tmp_path, path)  # atomic rename on both POSIX and Windows


def _tail_is_fresh(tail: _EventLogTail, target_path: Path) -> bool:
    """A tail is trustworthy only if it names the CURRENT target file and its
    recorded size matches the file's actual size on disk. A mismatch means
    another writer, a rotation, or a partial crash has moved reality out from
    under it, so the caller falls back to a full recount for that one write."""
    if tail.active_file_name != target_path.name:
        return False
    if not target_path.exists():
        return False
    return target_path.stat().st_size == tail.active_file_size


def get_event_logs_dir(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "ledger" / "events"


def get_active_event_log_path(
    program_id: str,
    *,
    recorded_at: datetime | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    current = _ensure_utc(recorded_at or datetime.now(timezone.utc))
    events_dir = get_event_logs_dir(program_id, programs_root=programs_root)
    return _resolve_write_path(events_dir, current, max_bytes=DEFAULT_MAX_BYTES)


def genesis_prev_hash(program_id: str) -> str:
    return _sha256_hex(f"{program_id}:genesis")


def build_event_envelope(
    *,
    program_id: str,
    event_type: str,
    occurred_at: datetime,
    recorded_at: datetime | None,
    temporal_confidence: TemporalConfidence,
    confidence: ConfidenceTier,
    actor: str,
    payload: dict[str, Any],
    source_ref: SourceRef,
    corroborating_refs: tuple[SourceRef, ...] = (),
    dedupe_payload: dict[str, Any] | None = None,
) -> EventEnvelope:
    validate_event_payload(event_type, payload)
    validate_typed_source_ref(source_ref)
    for corroborating_ref in corroborating_refs:
        validate_typed_source_ref(corroborating_ref)
    normalized_recorded_at = _ensure_utc(recorded_at or datetime.now(timezone.utc))
    dedupe_core_hash = None
    if dedupe_payload is not None:
        dedupe_core_hash = compute_dedupe_core_hash(event_type, dedupe_payload)
    return EventEnvelope(
        event_id=new_ulid(normalized_recorded_at),
        program_id=program_id,
        event_type=event_type,
        occurred_at=_ensure_utc(occurred_at),
        recorded_at=normalized_recorded_at,
        temporal_confidence=temporal_confidence,
        confidence=confidence,
        actor=actor,
        payload=_normalize_json_value(payload),
        source_ref=source_ref,
        corroborating_refs=corroborating_refs,
        prev_event_hash="",
        content_hash=compute_content_hash(event_type, _ensure_utc(occurred_at), payload),
        dedupe_core_hash=dedupe_core_hash,
    )


def write_event(
    envelope: EventEnvelope,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    grounded_in_validator: Callable[[str], bool] | None = None,
) -> EventWriteResult:
    validate_event_payload(envelope.event_type, envelope.payload)
    _validate_grounded_in_claim_refs(envelope.payload, grounded_in_validator=grounded_in_validator)

    events_dir = get_event_logs_dir(envelope.program_id, programs_root=programs_root)
    tail = _read_tail(events_dir)

    # The clamp (below) can push recorded_at into a later month, which can
    # change which file _resolve_write_path picks — so target_path can only
    # be resolved AFTER the clamp is known. Use the tail's last_recorded_at
    # (an O(1) read) as a preliminary clamp candidate, resolve against that,
    # then verify the tail actually names that resolved file. If it does,
    # the tail's other fields (hash, line count) are consistent and safe to
    # trust. If it doesn't (stale/missing tail, rotation, cross-process
    # drift, or first-ever write), fall back to the exact previous full-scan
    # behavior, including re-resolving target_path with the authoritative
    # clamp.
    preliminary_previous_recorded_at = (
        _parse_datetime(tail.last_recorded_at, "tail.last_recorded_at") if tail is not None else None
    )
    recorded_at = envelope.recorded_at
    if preliminary_previous_recorded_at is not None and recorded_at < preliminary_previous_recorded_at:
        recorded_at = preliminary_previous_recorded_at
    target_path = _resolve_write_path(events_dir, recorded_at, max_bytes=max_bytes)

    if tail is not None and _tail_is_fresh(tail, target_path):
        prev_hash = tail.last_event_hash
        base_line_no = tail.active_file_line_count
        # recorded_at/target_path above were already resolved consistently
        # with this same tail.
    else:
        existing_events = read_events(envelope.program_id, programs_root=programs_root)
        previous = existing_events[-1] if existing_events else None
        prev_hash = genesis_prev_hash(envelope.program_id) if previous is None else compute_envelope_hash(previous)
        previous_recorded_at = previous.recorded_at if previous is not None else None
        recorded_at = envelope.recorded_at
        if previous_recorded_at is not None and recorded_at < previous_recorded_at:
            recorded_at = previous_recorded_at
        target_path = _resolve_write_path(events_dir, recorded_at, max_bytes=max_bytes)
        base_line_no = _count_lines(target_path) if target_path.exists() else 0

    persisted = replace(
        envelope,
        event_id=new_ulid(recorded_at),
        recorded_at=recorded_at,
        prev_event_hash=prev_hash,
        content_hash=compute_content_hash(envelope.event_type, envelope.occurred_at, envelope.payload),
    )
    line = canonical_json(persisted.to_dict()) + "\n"
    append_jsonl_line(target_path, line)
    line_no = base_line_no + 1
    index_event(persisted, file_path=target_path, line_no=line_no, programs_root=programs_root)
    _write_tail(
        events_dir,
        _EventLogTail(
            last_event_hash=compute_envelope_hash(persisted),
            last_recorded_at=_datetime_to_wire(persisted.recorded_at),
            active_file_name=target_path.name,
            active_file_size=target_path.stat().st_size,
            active_file_line_count=line_no,
        ),
    )
    return EventWriteResult(envelope=persisted, path=target_path, rotated=line_no == 1 and target_path.name != _month_file_name(recorded_at, 1))


def write_events_atomic(
    envelopes: tuple[EventEnvelope, ...],
    *,
    programs_root: Path = PROGRAMS_ROOT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    grounded_in_validator: Callable[[str], bool] | None = None,
) -> EventBatchWriteResult:
    if not envelopes:
        raise ValueError("write_events_atomic requires at least one envelope.")
    program_ids = {envelope.program_id for envelope in envelopes}
    if len(program_ids) != 1:
        raise ValueError("write_events_atomic requires envelopes from a single program.")
    program_id = next(iter(program_ids))

    events_dir = get_event_logs_dir(program_id, programs_root=programs_root)
    tail = _read_tail(events_dir)

    # Same two-phase resolution as write_event: use the tail's last_recorded_at
    # as a preliminary clamp candidate for the FIRST envelope so target_path
    # can be resolved before deciding whether the tail is trustworthy.
    preliminary_previous_recorded_at = (
        _parse_datetime(tail.last_recorded_at, "tail.last_recorded_at") if tail is not None else None
    )
    first_candidate_recorded_at = envelopes[0].recorded_at
    if preliminary_previous_recorded_at is not None and first_candidate_recorded_at < preliminary_previous_recorded_at:
        first_candidate_recorded_at = preliminary_previous_recorded_at
    target_path = _resolve_write_path(events_dir, first_candidate_recorded_at, max_bytes=max_bytes)

    if tail is not None and _tail_is_fresh(tail, target_path):
        previous_hash = tail.last_event_hash
        previous_recorded_at: datetime | None = preliminary_previous_recorded_at
        base_line_no = tail.active_file_line_count
    else:
        existing_events = read_events(program_id, programs_root=programs_root)
        previous = existing_events[-1] if existing_events else None
        previous_hash = genesis_prev_hash(program_id) if previous is None else compute_envelope_hash(previous)
        previous_recorded_at = previous.recorded_at if previous is not None else None
        first_recorded_at = envelopes[0].recorded_at
        if previous_recorded_at is not None and first_recorded_at < previous_recorded_at:
            first_recorded_at = previous_recorded_at
        target_path = _resolve_write_path(events_dir, first_recorded_at, max_bytes=max_bytes)
        base_line_no = _count_lines(target_path) if target_path.exists() else 0

    persisted_envelopes: list[EventEnvelope] = []
    for envelope in envelopes:
        validate_event_payload(envelope.event_type, envelope.payload)
        _validate_grounded_in_claim_refs(envelope.payload, grounded_in_validator=grounded_in_validator)
        recorded_at = envelope.recorded_at
        if previous_recorded_at is not None and recorded_at < previous_recorded_at:
            recorded_at = previous_recorded_at
        persisted = replace(
            envelope,
            event_id=new_ulid(recorded_at),
            recorded_at=recorded_at,
            prev_event_hash=previous_hash,
            content_hash=compute_content_hash(envelope.event_type, envelope.occurred_at, envelope.payload),
        )
        persisted_envelopes.append(persisted)
        previous_hash = compute_envelope_hash(persisted)
        previous_recorded_at = persisted.recorded_at

    lines = tuple(canonical_json(envelope.to_dict()) + "\n" for envelope in persisted_envelopes)
    rotated = append_jsonl_lines(target_path, lines)
    final_line_no = base_line_no + len(persisted_envelopes)
    first_line_no = final_line_no - len(persisted_envelopes) + 1
    index_events(
        tuple(
            (envelope, target_path, first_line_no + index)
            for index, envelope in enumerate(persisted_envelopes)
        ),
        program_id=program_id,
        programs_root=programs_root,
    )
    _write_tail(
        events_dir,
        _EventLogTail(
            last_event_hash=previous_hash,
            last_recorded_at=_datetime_to_wire(persisted_envelopes[-1].recorded_at),
            active_file_name=target_path.name,
            active_file_size=target_path.stat().st_size,
            active_file_line_count=final_line_no,
        ),
    )
    return EventBatchWriteResult(envelopes=tuple(persisted_envelopes), path=target_path, rotated=rotated)


def _validate_grounded_in_claim_refs(
    payload: dict[str, Any],
    *,
    grounded_in_validator: Callable[[str], bool] | None,
) -> None:
    grounded_in = payload.get("grounded_in")
    if grounded_in is None:
        return
    if not isinstance(grounded_in, list):
        raise ValueError("payload.grounded_in must be a list when present.")
    for claim_id in grounded_in:
        if not isinstance(claim_id, str) or not claim_id:
            raise ValueError("payload.grounded_in entries must be non-empty claim IDs.")
        if grounded_in_validator is not None and not grounded_in_validator(claim_id):
            raise ValueError(f"Unknown grounded_in claim id: {claim_id}")


def read_events(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> tuple[EventEnvelope, ...]:
    events_dir = get_event_logs_dir(program_id, programs_root=programs_root)
    if not events_dir.exists():
        return ()

    event_paths = _list_event_paths(events_dir)
    events: list[EventEnvelope] = []
    for path in event_paths:
        events.extend(_read_events_from_path(path))
    return tuple(events)


def latest_event_chain_state(
    program_id: str, *, programs_root: Path = PROGRAMS_ROOT
) -> tuple[str, datetime] | None:
    """Return (envelope_hash, recorded_at) of the most recent event, or None
    if the program has no events yet. Used by ``ProgramCheckpointManifest``
    (CPK) to capture the event log's position without a caller needing to
    know about the internal tail cache. Not on the hot append path — always
    does a full scan, which is correct and simple for periodic checkpoint
    capture (unlike every-single-append, this is not called often enough to
    need the O(1) tail optimization).
    """
    events = read_events(program_id, programs_root=programs_root)
    if not events:
        return None
    last = events[-1]
    return compute_envelope_hash(last), last.recorded_at


def verify_event_log(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> EventLogVerification:
    from src.core.ledger.redaction import load_redaction_registry
    events = read_events(program_id, programs_root=programs_root)
    redaction_registry = load_redaction_registry(program_id, programs_root=programs_root)
    issues: list[str] = []
    previous_hash = genesis_prev_hash(program_id)
    previous_recorded_at: datetime | None = None

    for index, event in enumerate(events, start=1):
        is_redacted = event.event_id in redaction_registry
        if is_redacted:
            # §10.8: content-hash mismatch is expected and benign for redacted events.
            # Chain continuity uses the recorded original_envelope_hash instead of rehashing.
            original_hash = redaction_registry[event.event_id].original_envelope_hash
            if event.prev_event_hash != previous_hash:
                issues.append(f"event {index} prev hash mismatch (redacted)")
            previous_hash = original_hash
        else:
            expected_content_hash = compute_content_hash(event.event_type, event.occurred_at, event.payload)
            if event.content_hash != expected_content_hash:
                issues.append(f"event {index} content hash mismatch")
            if event.prev_event_hash != previous_hash:
                issues.append(f"event {index} prev hash mismatch")
            previous_hash = compute_envelope_hash(event)
        if previous_recorded_at is not None and event.recorded_at < previous_recorded_at:
            issues.append(f"event {index} recorded_at regressed")
        previous_recorded_at = event.recorded_at

    return EventLogVerification(ok=not issues, checked_event_count=len(events), issues=tuple(issues))


def canonical_json(payload: Any) -> str:
    return json.dumps(_normalize_json_value(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_content_hash(event_type: str, occurred_at: datetime, payload: dict[str, Any]) -> str:
    occurred_wire = _datetime_to_wire(_ensure_utc(occurred_at))
    return _sha256_hex(f"{event_type}|{occurred_wire}|{canonical_json(payload)}")


def compute_dedupe_core_hash(event_type: str, payload: dict[str, Any]) -> str:
    return _sha256_hex(f"{event_type}|{canonical_json(payload)}")


def compute_envelope_hash(envelope: EventEnvelope) -> str:
    return _sha256_hex(canonical_json(envelope.to_dict()))


def dedupe_key_for_event(envelope: EventEnvelope) -> str | None:
    if envelope.dedupe_core_hash is None:
        return None
    return _sha256_hex(f"{source_document_key(envelope.source_ref)}|{envelope.dedupe_core_hash}")


def _list_event_paths(events_dir: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            (path for path in events_dir.glob("*.events.jsonl") if path.is_file()),
            key=_event_file_sort_key,
        )
    )


def iter_event_records(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> tuple[tuple[Path, int, dict[str, Any]], ...]:
    events_dir = get_event_logs_dir(program_id, programs_root=programs_root)
    records: list[tuple[Path, int, dict[str, Any]]] = []
    if not events_dir.exists():
        return ()
    for path in _list_event_paths(events_dir):
        for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            payload = json.loads(stripped)
            if isinstance(payload, dict):
                records.append((path, line_no, payload))
    return tuple(records)


def _read_events_from_path(path: Path) -> tuple[EventEnvelope, ...]:
    valid_lines: list[str] = []
    events: list[EventEnvelope] = []
    invalid_found = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
            if not isinstance(payload, dict):
                raise ValueError("event row must be an object")
            envelope = EventEnvelope.from_dict(payload)
        except (json.JSONDecodeError, ValueError):
            invalid_found = True
            continue
        events.append(envelope)
        valid_lines.append(stripped + "\n")
    if invalid_found:
        quarantine_and_rewrite_jsonl(path, valid_lines)
    return tuple(events)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _datetime_to_wire(value: datetime) -> str:
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return _ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _required_str(payload: dict[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _optional_str(payload: dict[str, Any], field_name: str) -> str | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string when present")
    return value


def _normalize_json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _datetime_to_wire(value)
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, dict):
        return {str(key): _normalize_json_value(inner) for key, inner in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported JSON value: {type(value)!r}")


def _sha256_hex(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8"))
    return f"sha256:{digest.hexdigest()}"


def _month_file_name(recorded_at: datetime, sequence: int) -> str:
    stem = recorded_at.strftime("%Y-%m")
    return f"{stem}.events.jsonl" if sequence == 1 else f"{stem}.{sequence}.events.jsonl"


def _resolve_write_path(events_dir: Path, recorded_at: datetime, *, max_bytes: int) -> Path:
    events_dir.mkdir(parents=True, exist_ok=True)
    sequence = 1
    while True:
        candidate = events_dir / _month_file_name(recorded_at, sequence)
        if not candidate.exists():
            return candidate
        if candidate.stat().st_size < max_bytes:
            return candidate
        sequence += 1


def _count_lines(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def _event_file_sort_key(path: Path) -> tuple[str, int]:
    stem = path.name.removesuffix(".events.jsonl")
    if stem.count(".") == 1:
        month_part, sequence_part = stem.split(".", maxsplit=1)
        if sequence_part.isdigit():
            return month_part, int(sequence_part)
    return stem, 1
