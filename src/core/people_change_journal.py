"""specs/people.md Phase 1, PPL-W1.7: field-level signed change journal.

§7.7: "All accepted writers append to `knowledge/_journal/people_changes.jsonl`"
with an illustrative per-record shape (`event_id`, `transaction_id`,
`sequence`, `operation`, `entity_id`, `field`, `before`/`after`,
`before_hash`/`after_hash`, `previous_event_hash`, `event_hash`, ...).
"Hashing, segment signing/verification, rotation and redaction continuity
reuse the platform's existing archive/event verification helpers; this
feature does not implement a second cryptographic chain." This module
honors that directive by REUSING, not reimplementing:

* `src/core/ledger/event_log.py::canonical_json` -- the platform's one
  canonical-JSON serializer, so `sha256(canonical_json(record))` here is
  the exact same hashing convention `EventEnvelope`'s own
  `compute_envelope_hash` uses, not a second scheme.
* `src/core/archive_signing.py` -- the platform's one HMAC-SHA256
  archive-segment signing/verification helper (WS-7), reused directly to
  sign each rotated (closed) segment. Signing is best-effort: if no
  keyring key is configured (`archive_signing_unavailable()`), rotation
  still succeeds unsigned, mirroring that module's own established
  skip-vs-block convention (`src/commands/archive_signing.py`) -- a
  missing signature degrades auditability, it does not block writers.
* `src/core/jsonl_utils.py::append_jsonl_line`/`read_jsonl_records` -- the
  one sanctioned JSONL append/read seam (D-18, PB-37); this module does
  not call `open()`/`json.loads()` on a JSONL line directly.

The one thing genuinely new here is the *hash-chain formula itself*
(`event_hash = sha256(canonical_json(record_without_event_hash))`,
`previous_event_hash` carried forward record-to-record) and the
`_journal/archive/<year>/<stream>_<end_sequence>.jsonl` rotation layout
-- `event_log.py`'s own chain is tightly bound to `EventEnvelope`'s
program-sequenced shape and isn't reusable as-is for this feature's
different (person/team-change-shaped, workspace-scoped, not
program-scoped) record. The FORMULA (canonical-JSON hash, chained via a
`previous_*_hash` field, with a `genesis_prev_hash`-style seed) is
mirrored from `compute_envelope_hash`/`genesis_prev_hash`, matching this
session's established "reuse the pattern, not force an ill-fitting
import" precedent (PPL-W1.2's lease module vs `workspace_lease.py`).

Two streams share this exact engine ("the same rules", §7.7):
`people_changes` (field-level change events) and `people_conflicts`
(dismiss/merge/split/bind decision events).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re

from src.core.archive_signing import (
    SignatureRecord,
    archive_signing_unavailable,
    get_archive_signing_key,
    load_signature_record,
    manifest_signature_sidecar_path,
    sign_manifest,
    verify_signature,
    write_signature_record,
)
from src.core.exceptions import ConfigError
from src.core.jsonl_utils import append_jsonl_line, read_jsonl_records
from src.core.ledger.event_log import canonical_json
from src.core.ledger.ulid import new_ulid

JOURNAL_SCHEMA_VERSION = "1.0"
STREAM_PEOPLE_CHANGES = "people_changes"
STREAM_PEOPLE_CONFLICTS = "people_conflicts"
#: PPL-W4.7: one durable, audited record per `--apply` provider-refresh run
#: (§9.3's acceptance criterion 19, "reveal/merge/force operations are
#: principal-authorized and audited," applied to the refresh surface).
#: Same hash-chain/rotation engine as the other two streams -- "the same
#: rules," per §7.7's own phrasing for `people_conflicts.jsonl`.
STREAM_PEOPLE_REFRESH_TELEMETRY = "people_refresh_telemetry"
DEFAULT_JOURNAL_MAX_BYTES = 10 * 1024 * 1024  # matches proposal_audit.jsonl's rotation cap (PPL-W1.2 precedent).

_SEGMENT_FILENAME_RE = re.compile(r"^(?P<stream>[a-z_]+)_(?P<end_sequence>\d+)\.jsonl$")


def _sha256_hex(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def genesis_prev_hash(workspace_id: str, stream: str) -> str:
    """Mirrors `event_log.py::genesis_prev_hash`'s pattern (a deterministic
    seed hash, not a real prior record) for a workspace-scoped stream
    instead of a program-scoped one."""
    return _sha256_hex(f"{workspace_id}:{stream}:genesis")


def journal_active_path(knowledge_root: Path, stream: str = STREAM_PEOPLE_CHANGES) -> Path:
    return knowledge_root / "_journal" / f"{stream}.jsonl"


def journal_archive_dir(knowledge_root: Path, year: int) -> Path:
    return knowledge_root / "_journal" / "archive" / str(year)


def _segment_end_sequence(path: Path) -> int | None:
    match = _SEGMENT_FILENAME_RE.match(path.name)
    return int(match.group("end_sequence")) if match else None


def _list_archived_segments(knowledge_root: Path, stream: str) -> tuple[Path, ...]:
    archive_root = knowledge_root / "_journal" / "archive"
    if not archive_root.exists():
        return ()
    segments = [
        path
        for year_dir in sorted(archive_root.iterdir())
        if year_dir.is_dir()
        for path in year_dir.glob(f"{stream}_*.jsonl")
    ]
    return tuple(sorted(segments, key=lambda path: _segment_end_sequence(path) or 0))


def read_journal_records(knowledge_root: Path, stream: str = STREAM_PEOPLE_CHANGES, *, include_archived: bool = True) -> tuple[dict, ...]:
    """All records for `stream`, oldest to newest, spanning archived
    segments (if `include_archived`) followed by the active file."""
    records: list[dict] = []
    if include_archived:
        for segment_path in _list_archived_segments(knowledge_root, stream):
            records.extend(read_jsonl_records(segment_path))
    records.extend(read_jsonl_records(journal_active_path(knowledge_root, stream)))
    return tuple(records)


def _journal_tail(knowledge_root: Path, stream: str, *, workspace_id: str) -> tuple[int, str]:
    """Returns (next_sequence, previous_event_hash), continuing the chain
    across a rotation boundary if the active file is currently empty but
    an archived segment exists."""
    active_records = read_jsonl_records(journal_active_path(knowledge_root, stream))
    if active_records:
        last = active_records[-1]
        return int(last["sequence"]) + 1, str(last["event_hash"])
    for segment_path in reversed(_list_archived_segments(knowledge_root, stream)):
        archived_records = read_jsonl_records(segment_path)
        if archived_records:
            last = archived_records[-1]
            return int(last["sequence"]) + 1, str(last["event_hash"])
    return 1, genesis_prev_hash(workspace_id, stream)


def _sign_rotated_segment(segment_path: Path, *, stream: str, records: tuple[dict, ...]) -> None:
    if archive_signing_unavailable() or not records:
        return
    key = get_archive_signing_key()
    if key is None:
        return
    manifest_payload = {
        "stream": stream,
        "segment": segment_path.name,
        "record_count": len(records),
        "first_sequence": records[0]["sequence"],
        "last_sequence": records[-1]["sequence"],
        "first_event_hash": records[0]["event_hash"],
        "last_event_hash": records[-1]["event_hash"],
    }
    record = sign_manifest(edition=stream, issue_number=int(records[-1]["sequence"]), manifest_payload=manifest_payload, key=key)
    write_signature_record(manifest_signature_sidecar_path(segment_path), record)


def _segment_signature_payload(segment_path: Path, *, stream: str, records: tuple[dict, ...]) -> dict:
    return {
        "stream": stream,
        "segment": segment_path.name,
        "record_count": len(records),
        "first_sequence": records[0]["sequence"],
        "last_sequence": records[-1]["sequence"],
        "first_event_hash": records[0]["event_hash"],
        "last_event_hash": records[-1]["event_hash"],
    }


def _rotate_if_oversize(knowledge_root: Path, stream: str, *, max_bytes: int) -> None:
    """§7.7: "Active journal segments are size-bounded and rotated into
    immutable `knowledge/_journal/archive/<year>/people_changes_<sequence>.jsonl`
    segments. Rotation never truncates acknowledged history." -- the
    active file is MOVED (never deleted/rewritten), so every record it
    held remains readable at its archived path."""
    active_path = journal_active_path(knowledge_root, stream)
    if not active_path.exists() or active_path.stat().st_size < max_bytes:
        return
    records = read_jsonl_records(active_path)
    if not records:
        return
    last_recorded_at = datetime.fromisoformat(str(records[-1]["recorded_at"]).replace("Z", "+00:00"))
    end_sequence = int(records[-1]["sequence"])
    archive_dir = journal_archive_dir(knowledge_root, last_recorded_at.year)
    archive_dir.mkdir(parents=True, exist_ok=True)
    segment_path = archive_dir / f"{stream}_{end_sequence}.jsonl"
    active_path.rename(segment_path)
    _sign_rotated_segment(segment_path, stream=stream, records=records)


def _append_hash_chained_record(
    knowledge_root: Path,
    stream: str,
    fields: dict,
    *,
    workspace_id: str,
    max_bytes: int,
    as_of: datetime | None,
) -> dict:
    now = as_of or datetime.now(timezone.utc)
    _rotate_if_oversize(knowledge_root, stream, max_bytes=max_bytes)
    sequence, previous_event_hash = _journal_tail(knowledge_root, stream, workspace_id=workspace_id)

    record = {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "event_id": new_ulid(now),
        "workspace_id": workspace_id,
        "sequence": sequence,
        "recorded_at": now.isoformat(),
        "previous_event_hash": previous_event_hash,
        **fields,
    }
    record["event_hash"] = _sha256_hex(canonical_json(record))

    append_jsonl_line(journal_active_path(knowledge_root, stream), canonical_json(record) + "\n")
    return record


def append_people_change_record(
    knowledge_root: Path,
    *,
    workspace_id: str,
    transaction_id: str,
    generation_id: str,
    authenticated_principal: str,
    operation: str,
    entity_id: str,
    field: str,
    before: object,
    after: object,
    source: str,
    reason: str,
    on_behalf_of: str | None = None,
    source_ref: str | None = None,
    refresh_run_id: str | None = None,
    max_bytes: int = DEFAULT_JOURNAL_MAX_BYTES,
    as_of: datetime | None = None,
) -> dict:
    """§7.7's `people_changes.jsonl` shape. "The journal stores field-level
    changes, not complete before/after directory documents" -- `before`/
    `after` are the single field's value, and `before_hash`/`after_hash`
    are recorded alongside so a redaction pass can later blank the raw
    value while the hash-based audit trail survives."""
    fields = {
        "transaction_id": transaction_id,
        "generation_id": generation_id,
        "authenticated_principal": authenticated_principal,
        "on_behalf_of": on_behalf_of,
        "operation": operation,
        "entity_id": entity_id,
        "field": field,
        "before": before,
        "after": after,
        "source": source,
        "source_ref": source_ref,
        "refresh_run_id": refresh_run_id,
        "reason": reason,
        "before_hash": None if before is None else _sha256_hex(canonical_json(before)),
        "after_hash": None if after is None else _sha256_hex(canonical_json(after)),
    }
    return _append_hash_chained_record(knowledge_root, STREAM_PEOPLE_CHANGES, fields, workspace_id=workspace_id, max_bytes=max_bytes, as_of=as_of)


def append_people_conflict_record(
    knowledge_root: Path,
    *,
    workspace_id: str,
    conflict_id: str,
    decision: str,
    authenticated_principal: str,
    reason: str,
    entity_id: str | None = None,
    max_bytes: int = DEFAULT_JOURNAL_MAX_BYTES,
    as_of: datetime | None = None,
) -> dict:
    """§7.7's `people_conflicts.jsonl` decision stream: "so dismiss/merge/
    split/bind history is preserved without overloading factual change
    events." Follows the identical rotation/hash-chain engine as
    `people_changes.jsonl` -- "the same rules" -- via a conflict-shaped
    field set instead of a field-change-shaped one."""
    fields = {
        "conflict_id": conflict_id,
        "decision": decision,
        "authenticated_principal": authenticated_principal,
        "entity_id": entity_id,
        "reason": reason,
    }
    return _append_hash_chained_record(knowledge_root, STREAM_PEOPLE_CONFLICTS, fields, workspace_id=workspace_id, max_bytes=max_bytes, as_of=as_of)


def append_people_refresh_telemetry_record(
    knowledge_root: Path,
    *,
    workspace_id: str,
    refresh_run_id: str,
    provider: str,
    tenant_id: str | None,
    requested_count: int,
    observed_count: int,
    accepted_count: int,
    quarantined_count: int,
    rejected_count: int,
    error_count: int,
    wall_time_seconds: float,
    kill_switch_engaged: bool,
    authenticated_principal: str,
    max_bytes: int = DEFAULT_JOURNAL_MAX_BYTES,
    as_of: datetime | None = None,
) -> dict:
    """PPL-W4.7: one durable, audited `people_refresh_telemetry.jsonl`
    record per `--apply` provider-refresh run -- closes acceptance
    criterion 19 ("reveal/merge/force operations are principal-authorized
    and audited") for the refresh surface specifically. Same hash-chain/
    rotation engine as `people_changes.jsonl`/`people_conflicts.jsonl`."""
    fields = {
        "refresh_run_id": refresh_run_id,
        "provider": provider,
        "tenant_id": tenant_id,
        "requested_count": requested_count,
        "observed_count": observed_count,
        "accepted_count": accepted_count,
        "quarantined_count": quarantined_count,
        "rejected_count": rejected_count,
        "error_count": error_count,
        "wall_time_seconds": wall_time_seconds,
        "kill_switch_engaged": kill_switch_engaged,
        "authenticated_principal": authenticated_principal,
    }
    return _append_hash_chained_record(
        knowledge_root, STREAM_PEOPLE_REFRESH_TELEMETRY, fields, workspace_id=workspace_id, max_bytes=max_bytes, as_of=as_of
    )


def find_refresh_telemetry_record(knowledge_root: Path, *, refresh_run_id: str) -> dict | None:
    """PPL-W4.7's own verification bar: "retrievable by run ID.\""""
    records = read_journal_records(knowledge_root, STREAM_PEOPLE_REFRESH_TELEMETRY)
    return next((record for record in records if record.get("refresh_run_id") == refresh_run_id), None)


@dataclass(frozen=True, slots=True)
class JournalHashChainVerification:
    ok: bool
    checked_record_count: int
    violations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class JournalRedactionResult:
    redacted_record_count: int
    rewritten_segment_count: int


def verify_journal_hash_chain(records: tuple[dict, ...], *, workspace_id: str, stream: str) -> JournalHashChainVerification:
    """Recomputes every record's expected `event_hash` from its own
    content and confirms `previous_event_hash` continuity record-to-record
    -- including across a rotation boundary, since `records` is expected
    to be the FULL ordered sequence from `read_journal_records` (archived
    segments + active file concatenated)."""
    violations: list[str] = []
    expected_previous = genesis_prev_hash(workspace_id, stream)
    for index, record in enumerate(records):
        if record.get("previous_event_hash") != expected_previous:
            violations.append(f"record {index} (sequence {record.get('sequence')}): previous_event_hash does not match the prior record's event_hash")
        recomputed = dict(record)
        recorded_event_hash = recomputed.pop("event_hash", None)
        expected_event_hash = _sha256_hex(canonical_json(recomputed))
        if recorded_event_hash != expected_event_hash:
            violations.append(f"record {index} (sequence {record.get('sequence')}): event_hash does not match recomputed content hash")
        # Always propagate the CORRECTLY recomputed hash forward, not the
        # possibly-tampered recorded one -- otherwise a single tampered
        # event_hash could mask a genuine chain break in later records.
        expected_previous = expected_event_hash
    return JournalHashChainVerification(ok=not violations, checked_record_count=len(records), violations=tuple(violations))


def _redact_record(record: dict, *, stream: str, entity_id: str) -> tuple[dict, bool]:
    if record.get("entity_id") != entity_id:
        return record, False
    redacted = dict(record)
    if stream == STREAM_PEOPLE_CHANGES:
        redacted["before"] = None
        redacted["after"] = None
        redacted["source_ref"] = None
    redacted["reason"] = "[REDACTED]"
    redacted["privacy_redacted"] = True
    return redacted, True


def _rechain_records(records: tuple[dict, ...], *, workspace_id: str, stream: str) -> tuple[dict, ...]:
    previous_hash = genesis_prev_hash(workspace_id, stream)
    rechained: list[dict] = []
    for record in records:
        updated = dict(record)
        updated["previous_event_hash"] = previous_hash
        updated.pop("event_hash", None)
        updated["event_hash"] = _sha256_hex(canonical_json(updated))
        previous_hash = updated["event_hash"]
        rechained.append(updated)
    return tuple(rechained)


def _write_records_atomic(path: Path, records: tuple[dict, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.privacy-redaction.tmp")
    with temporary_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(canonical_json(record) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_path, path)


def validate_person_journal_redaction(
    knowledge_root: Path,
    *,
    workspace_id: str,
    entity_id: str,
) -> None:
    """Fail closed before privacy erasure if journal history cannot be safely
    redacted and re-signed."""
    for stream in (STREAM_PEOPLE_CHANGES, STREAM_PEOPLE_CONFLICTS):
        archived_paths = _list_archived_segments(knowledge_root, stream)
        paths = (*archived_paths, journal_active_path(knowledge_root, stream))
        records_by_path = tuple((path, tuple(read_jsonl_records(path))) for path in paths)
        all_records = tuple(record for _, records in records_by_path for record in records)
        verification = verify_journal_hash_chain(all_records, workspace_id=workspace_id, stream=stream)
        if not verification.ok:
            raise ConfigError(
                f"Refusing privacy redaction because the {stream} journal hash chain is invalid: "
                f"{'; '.join(verification.violations)}"
            )

        for path, records in records_by_path:
            if path not in archived_paths or not records:
                continue
            try:
                signature = load_signature_record(manifest_signature_sidecar_path(path))
            except (OSError, ValueError) as error:
                raise ConfigError(
                    f"Refusing privacy redaction because archive signature metadata is invalid for {path.name!r}."
                ) from error
            if signature is None:
                continue
            key = get_archive_signing_key()
            if key is None or archive_signing_unavailable():
                raise ConfigError(
                    f"Refusing privacy redaction because signed archive segment {path.name!r} "
                    "cannot be re-signed with the configured archive-signing key."
                )
            if not verify_signature(
                signature,
                manifest_payload=_segment_signature_payload(path, stream=stream, records=records),
                key=key,
            ):
                raise ConfigError(
                    f"Refusing privacy redaction because archive signature verification failed for {path.name!r}."
                )


def redact_person_journal_records(
    knowledge_root: Path,
    *,
    workspace_id: str,
    entity_id: str,
) -> JournalRedactionResult:
    """Redact one person's raw journal values while preserving each stream's
    hash-chain and any existing archived-segment signature.

    Historical records are immutable for ordinary operations.  Privacy erasure
    is the explicit exception: it validates the complete existing chain and
    signed segments before replacing the redacted chain atomically.  A signed
    archive without its signing key refuses erasure rather than leaving an
    unverifiable segment behind.
    """
    validate_person_journal_redaction(
        knowledge_root,
        workspace_id=workspace_id,
        entity_id=entity_id,
    )
    redacted_count = 0
    rewritten_segments = 0
    for stream in (STREAM_PEOPLE_CHANGES, STREAM_PEOPLE_CONFLICTS):
        archived_paths = _list_archived_segments(knowledge_root, stream)
        paths = (*archived_paths, journal_active_path(knowledge_root, stream))
        records_by_path = tuple((path, tuple(read_jsonl_records(path))) for path in paths)
        all_records = tuple(record for _, records in records_by_path for record in records)
        signature_records: dict[Path, SignatureRecord] = {}
        for path, records in records_by_path:
            if path not in archived_paths or not records:
                continue
            sidecar_path = manifest_signature_sidecar_path(path)
            try:
                signature = load_signature_record(sidecar_path)
            except (OSError, ValueError) as error:
                raise ConfigError(
                    f"Refusing privacy redaction because archive signature metadata is invalid for {path.name!r}."
                ) from error
            if signature is None:
                continue
            key = get_archive_signing_key()
            if key is None or archive_signing_unavailable():
                raise ConfigError(
                    f"Refusing privacy redaction because signed archive segment {path.name!r} "
                    "cannot be re-signed with the configured archive-signing key."
                )
            if not verify_signature(
                signature,
                manifest_payload=_segment_signature_payload(path, stream=stream, records=records),
                key=key,
            ):
                raise ConfigError(
                    f"Refusing privacy redaction because archive signature verification failed for {path.name!r}."
                )
            signature_records[path] = signature

        redacted_records: list[dict] = []
        changed = False
        for record in all_records:
            updated, record_changed = _redact_record(record, stream=stream, entity_id=entity_id)
            redacted_records.append(updated)
            changed = changed or record_changed
            redacted_count += int(record_changed)
        if not changed:
            continue

        rechained = _rechain_records(tuple(redacted_records), workspace_id=workspace_id, stream=stream)
        offset = 0
        for path, original_records in records_by_path:
            replacement = rechained[offset : offset + len(original_records)]
            offset += len(original_records)
            if not original_records:
                continue
            _write_records_atomic(path, replacement)
            if path in signature_records:
                key = get_archive_signing_key()
                if key is None:
                    raise ConfigError(
                        f"Archive signing key disappeared while redacting {path.name!r}; "
                        "the operation requires manual recovery."
                    )
                signature = signature_records[path]
                write_signature_record(
                    manifest_signature_sidecar_path(path),
                    sign_manifest(
                        edition=stream,
                        issue_number=int(replacement[-1]["sequence"]),
                        manifest_payload=_segment_signature_payload(path, stream=stream, records=replacement),
                        key=key,
                        key_id=signature.key_id,
                    ),
                )
            rewritten_segments += 1
    return JournalRedactionResult(
        redacted_record_count=redacted_count,
        rewritten_segment_count=rewritten_segments,
    )
