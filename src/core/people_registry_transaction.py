"""specs/people.md Phase 1, PPL-W1.4/PPL-W1.5: staged transaction primitive
(prepare half, §6.7 steps 1-6, and commit half + crash recovery, steps
7-10).

§6.7's 10-step canonical write sequence is split across two work items:

1. Acquire the workspace-global lease and record the fencing token.       -- PPL-W1.4
2. Re-read `registry_manifest.json`; compare expected generation/hashes.  -- PPL-W1.4
3. Apply field-level operations to an in-memory copy.                     -- PPL-W1.4
4. Validate schemas, uniqueness, references, lifecycles, cycles, policy,
   source metadata.                                                       -- PPL-W1.4
5. Write candidate files + journal/outbox records to
   `knowledge/.transactions/<transaction_id>/`.                           -- PPL-W1.4
6. Reload through production loaders; compute the new generation
   manifest.                                                              -- PPL-W1.4
7. Write a checkpoint (old files/manifest) + `PREPARED` recovery record.  -- PPL-W1.5
8. Revalidate fencing immediately before each irreversible replace;
   replace data files in order with bounded retry/backoff.                -- PPL-W1.5
9. Replace `registry_manifest.json` last.                                 -- PPL-W1.5
10. Mark `COMMITTED`, dispatch the outbox idempotently, release lease.    -- PPL-W1.5

This module implements ONLY steps 1-6: it never touches a live registry
file, and it does not release the lease it acquires -- the lease stays
held across prepare/commit as one logical sequence, and step 10 (release)
is PPL-W1.5's job once the commit half exists. Until then,
`abort_prepared_registry_transaction` is this module's own explicit,
provisional escape hatch for releasing a lease and discarding staged
files after a prepare that will never be committed (e.g. a caller decides
not to proceed, or a test needs to clean up) -- it is not one of the
spec's numbered steps.

Real person/team schemas (§7.2) arrive in Phase 2a (PPL-W2A.1 onward).
This work item proves the staged-transaction mechanism itself against a
deliberately minimal synthetic placeholder schema, `SyntheticRegistryRecord`
-- exercising every validation category §6.7 step 4 names (schema,
uniqueness, references, lifecycles, cycles, policy, source metadata)
without depending on unbuilt Phase 2a types. Phase 2a's real writer reuses
this same prepare/commit machinery; it does not reimplement it.

Similarly, the "journal/outbox records" step 5 mentions are NOT the real
signed change journal (PPL-W1.7) or durable outbox (PPL-W1.6) -- neither
exists yet. This module writes a minimal `operations.json` summary into
the staged transaction directory as an honest placeholder, documented as
superseded once PPL-W1.6/W1.7 ship.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import json
import shutil
from pathlib import Path
from typing import Callable

import yaml

import os
import time

from src.core.exceptions import ConfigError
from src.core.jsonl_utils import compute_file_checksum
from src.core.ledger.ulid import new_ulid
from src.core.people_registry_identity import (
    REGISTRY_COMPILER_VERSION,
    REGISTRY_MANIFEST_SCHEMA_VERSION,
    RegistryManifest,
    load_registry_config,
    load_registry_manifest,
    registry_manifest_path,
    write_registry_manifest,
)
from src.core.people_registry_lease import (
    RegistryLeaseFencingTokenStale,
    RegistryLeaseHandle,
    acquire_registry_lease,
    is_registry_lease_expired,
    read_registry_lease_state,
    release_registry_lease,
)

_SYNTHETIC_RECORDS_FILENAME = "registry_synthetic_records.yaml"
_TRANSACTION_STATE_FILENAME = "state.json"
_CHECKPOINT_DIRNAME = "checkpoint"
_REPLACE_MAX_ATTEMPTS = 5
_REPLACE_INITIAL_DELAY_SECONDS = 0.05
_OPERATIONS_SUMMARY_FILENAME = "operations.json"
_TRANSACTIONS_DIRNAME = ".transactions"


class RegistryOperationKind(str, Enum):
    UPSERT = "upsert"
    DEACTIVATE = "deactivate"


@dataclass(frozen=True, slots=True)
class SyntheticRegistryRecord:
    """PPL-W1.4's minimal placeholder schema -- not a real product type.
    Exists only to give the staged-transaction primitive something
    concrete to validate/stage/reload against before Phase 2a's real
    person/team schemas exist."""

    record_id: str
    value: str
    parent_id: str | None = None
    active: bool = True
    restricted: bool = False
    policy_approved: bool = False
    source: str = ""
    observed_at: datetime | None = None
    actor: str = ""

    def to_payload(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "value": self.value,
            "parent_id": self.parent_id,
            "active": self.active,
            "restricted": self.restricted,
            "policy_approved": self.policy_approved,
            "source": self.source,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "actor": self.actor,
        }

    @staticmethod
    def from_payload(raw: dict) -> "SyntheticRegistryRecord":
        observed_at_raw = raw.get("observed_at")
        return SyntheticRegistryRecord(
            record_id=str(raw["record_id"]),
            value=str(raw.get("value") or ""),
            parent_id=raw.get("parent_id"),
            active=bool(raw.get("active", True)),
            restricted=bool(raw.get("restricted", False)),
            policy_approved=bool(raw.get("policy_approved", False)),
            source=str(raw.get("source") or ""),
            observed_at=datetime.fromisoformat(str(observed_at_raw)) if observed_at_raw else None,
            actor=str(raw.get("actor") or ""),
        )


@dataclass(frozen=True, slots=True)
class RegistryFieldOperation:
    kind: RegistryOperationKind
    record: SyntheticRegistryRecord | None = None  # Required for UPSERT.
    record_id: str | None = None  # Required for DEACTIVATE.

    def target_record_id(self) -> str:
        if self.kind is RegistryOperationKind.UPSERT:
            assert self.record is not None
            return self.record.record_id
        assert self.record_id is not None
        return self.record_id


class RegistryTransactionValidationError(ConfigError):
    """Raised by §6.7 step 4's validation gate. `violations` lists every
    distinct failure found (not just the first) so a caller/operator sees
    the full picture in one round-trip."""

    def __init__(self, violations: tuple[str, ...]) -> None:
        self.violations = violations
        super().__init__("Registry transaction validation failed:\n  - " + "\n  - ".join(violations))


class RegistryGenerationStale(ConfigError):
    def __init__(self, expected: str, current: str) -> None:
        self.expected = expected
        self.current = current
        super().__init__(
            f"Registry generation changed since the caller last read it (expected {expected!r}, "
            f"current is {current!r}). Re-read the registry and retry."
        )


@dataclass(frozen=True, slots=True)
class PreparedRegistryManifestPreview:
    """The step-6 "new generation manifest" -- a preview only. It is never
    written to `registry_manifest.json`; that is step 9, PPL-W1.5's job."""

    generation_id: str
    prior_generation: str | None
    workspace_id: str
    customer_boundary_id: str
    fencing_token: int
    transaction_id: str
    source_hashes: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class PreparedRegistryTransaction:
    transaction_id: str
    staged_dir: Path
    lease: RegistryLeaseHandle
    manifest_preview: PreparedRegistryManifestPreview
    final_records: tuple[SyntheticRegistryRecord, ...]


@dataclass(frozen=True, slots=True)
class PreparedRegistryFilesTransaction:
    """A real-registry counterpart to the synthetic Phase 1 transaction.

    Callers provide only relative factual-file paths and write/reparse
    callbacks.  This module retains ownership of leasing, staging, manifest
    lineage, checkpointing, replacement, and recovery.
    """

    transaction_id: str
    staged_dir: Path
    lease: RegistryLeaseHandle
    manifest_preview: PreparedRegistryManifestPreview
    relative_paths: tuple[str, ...]


def transactions_root(knowledge_root: Path) -> Path:
    return knowledge_root / _TRANSACTIONS_DIRNAME


def synthetic_records_path(knowledge_root: Path) -> Path:
    return knowledge_root / _SYNTHETIC_RECORDS_FILENAME


def _validate_relative_paths(relative_paths: tuple[str, ...]) -> tuple[str, ...]:
    if not relative_paths:
        raise ConfigError("A registry file transaction requires at least one factual file.")
    normalized = tuple(sorted(set(relative_paths)))
    for relative_path in normalized:
        path = Path(relative_path)
        if not relative_path or path.is_absolute() or ".." in path.parts:
            raise ConfigError(f"Registry transaction file path must be a safe relative path, got {relative_path!r}.")
    return normalized


def prepare_registry_files_transaction(
    knowledge_root: Path,
    relative_paths: tuple[str, ...],
    *,
    owner: str,
    write_staged_files: Callable[[Path], None],
    validate_staged_files: Callable[[Path], None],
    expected_generation_id: str | None = None,
    as_of: datetime | None = None,
) -> PreparedRegistryFilesTransaction:
    """Prepare a typed multi-file registry transaction without touching live files.

    The supplied callbacks run only against the transaction staging directory:
    the first writes candidate bytes and the second reloads them through the
    production schema loaders.  Live publication is exclusively handled by
    :func:`commit_registry_files_transaction`.
    """
    config = load_registry_config(knowledge_root)
    if config is None:
        raise ConfigError("The registry has not been bootstrapped yet. Run 'vertex kb registry bootstrap --apply --customer-boundary-id <id>' first.")
    paths = _validate_relative_paths(relative_paths)
    lease = acquire_registry_lease(owner, knowledge_root=knowledge_root)
    staged_dir: Path | None = None
    try:
        # The manifest is deliberately read after the lease is held, closing
        # the read/lease race required by §6.7 steps 1-2.
        manifest = load_registry_manifest(knowledge_root)
        if manifest is None:
            raise ConfigError("The registry manifest is missing; recover bootstrap before preparing a transaction.")
        if expected_generation_id is not None and expected_generation_id != manifest.generation_id:
            raise RegistryGenerationStale(expected_generation_id, manifest.generation_id)

        now = as_of or datetime.now(timezone.utc)
        transaction_id = f"registry-tx-{new_ulid(now)}"
        staged_dir = transactions_root(knowledge_root) / transaction_id
        write_staged_files(staged_dir)
        missing = [relative_path for relative_path in paths if not (staged_dir / relative_path).is_file()]
        if missing:
            raise ConfigError(f"Registry transaction staging callback did not write required files: {', '.join(missing)}")
        validate_staged_files(staged_dir)
        source_hashes_by_path = dict(manifest.source_hashes)
        source_hashes_by_path.update(
            (relative_path, compute_file_checksum(staged_dir / relative_path)) for relative_path in paths
        )
        manifest_preview = PreparedRegistryManifestPreview(
            generation_id=new_ulid(now),
            prior_generation=manifest.generation_id,
            workspace_id=config.workspace_id,
            customer_boundary_id=config.customer_boundary_id,
            fencing_token=lease.fencing_token,
            transaction_id=transaction_id,
            source_hashes=tuple(sorted(source_hashes_by_path.items())),
        )
        return PreparedRegistryFilesTransaction(
            transaction_id=transaction_id,
            staged_dir=staged_dir,
            lease=lease,
            manifest_preview=manifest_preview,
            relative_paths=paths,
        )
    except Exception:
        release_registry_lease(lease, knowledge_root=knowledge_root)
        if staged_dir is not None:
            shutil.rmtree(staged_dir, ignore_errors=True)
        raise


def _load_synthetic_records(path: Path) -> tuple[SyntheticRegistryRecord, ...]:
    """The "production loader" step 6 reparses staged files through.
    Reused for both the live-baseline read (step 2/3) and the staged-file
    reload (step 6) -- the same loader function is the point: it proves
    round-trip fidelity, not merely that *a* parser exists."""
    if not path.exists():
        return ()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}")
    raw_records = raw.get("records") or []
    if not isinstance(raw_records, list):
        raise ConfigError(f"{path}: 'records' must be a list")
    return tuple(SyntheticRegistryRecord.from_payload(entry) for entry in raw_records)


def _write_synthetic_records(path: Path, records: tuple[SyntheticRegistryRecord, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": "1.0", "records": [record.to_payload() for record in sorted(records, key=lambda r: r.record_id)]}
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(payload, handle, sort_keys=False, default_flow_style=False)


def _apply_operations(
    baseline: tuple[SyntheticRegistryRecord, ...],
    operations: tuple[RegistryFieldOperation, ...],
) -> dict[str, SyntheticRegistryRecord]:
    merged: dict[str, SyntheticRegistryRecord] = {record.record_id: record for record in baseline}
    for operation in operations:
        if operation.kind is RegistryOperationKind.UPSERT:
            assert operation.record is not None
            merged[operation.record.record_id] = operation.record
        else:
            existing = merged.get(operation.record_id or "")
            if existing is None:
                continue  # Deactivating a record that doesn't exist is a no-op, not an error.
            merged[existing.record_id] = SyntheticRegistryRecord(
                record_id=existing.record_id,
                value=existing.value,
                parent_id=existing.parent_id,
                active=False,
                restricted=existing.restricted,
                policy_approved=existing.policy_approved,
                source=existing.source,
                observed_at=existing.observed_at,
                actor=existing.actor,
            )
    return merged


def _validate_transaction(
    *,
    baseline_by_id: dict[str, SyntheticRegistryRecord],
    merged: dict[str, SyntheticRegistryRecord],
    operations: tuple[RegistryFieldOperation, ...],
) -> tuple[str, ...]:
    """§6.7 step 4: "schemas, normalized uniqueness, references,
    lifecycles, hierarchy cycles, policy, and source metadata." Collects
    every violation rather than failing fast on the first, so a caller
    sees the complete picture in one round-trip."""
    violations: list[str] = []

    # Schema: record_id/value must be non-empty (dataclass construction
    # already enforces types; this enforces the semantic non-empty rule).
    for record in merged.values():
        if not record.record_id.strip():
            violations.append("schema: a record has an empty record_id")
        if not record.value.strip():
            violations.append(f"schema: record {record.record_id!r} has an empty value")

    # Uniqueness: normalized (casefolded) record_id collisions across
    # distinct raw IDs in the final set.
    normalized_seen: dict[str, str] = {}
    for record_id in merged:
        normalized = record_id.casefold()
        if normalized in normalized_seen and normalized_seen[normalized] != record_id:
            violations.append(f"uniqueness: record IDs {normalized_seen[normalized]!r} and {record_id!r} collide when normalized")
        else:
            normalized_seen[normalized] = record_id

    # References: parent_id must reference an existing record in the final set.
    for record in merged.values():
        if record.parent_id is not None and record.parent_id not in merged:
            violations.append(f"reference: record {record.record_id!r} has parent_id {record.parent_id!r}, which does not exist")

    # Lifecycles: a record inactive in the baseline cannot be reactivated by this transaction.
    touched_ids = {operation.target_record_id() for operation in operations}
    for record_id in touched_ids:
        baseline_record = baseline_by_id.get(record_id)
        final_record = merged.get(record_id)
        if baseline_record is not None and final_record is not None and not baseline_record.active and final_record.active:
            violations.append(f"lifecycle: record {record_id!r} is inactive in the baseline and cannot be reactivated by this transaction")

    # Hierarchy cycles: walk each touched record's parent chain, bounded by collection size.
    bound = len(merged) + 1
    for record_id in touched_ids:
        seen: set[str] = set()
        current = merged.get(record_id)
        steps = 0
        while current is not None and current.parent_id is not None:
            if current.parent_id in seen or current.parent_id == record_id:
                violations.append(f"cycle: record {record_id!r}'s parent chain contains a cycle through {current.parent_id!r}")
                break
            seen.add(current.parent_id)
            current = merged.get(current.parent_id)
            steps += 1
            if steps > bound:
                violations.append(f"cycle: record {record_id!r}'s parent chain exceeds the collection size without terminating")
                break

    # Policy: a restricted record requires explicit policy approval.
    for operation in operations:
        if operation.kind is RegistryOperationKind.UPSERT and operation.record is not None:
            record = operation.record
            if record.restricted and not record.policy_approved:
                violations.append(f"policy: record {record.record_id!r} is restricted but not policy_approved")

    # Source metadata: every upserted record must carry evidence (source + observed_at + actor).
    for operation in operations:
        if operation.kind is RegistryOperationKind.UPSERT and operation.record is not None:
            record = operation.record
            missing = [name for name, value in (("source", record.source), ("actor", record.actor)) if not value.strip()]
            if record.observed_at is None:
                missing.append("observed_at")
            if missing:
                violations.append(f"source metadata: record {record.record_id!r} is missing {', '.join(missing)}")

    return tuple(violations)


def prepare_registry_transaction(
    knowledge_root: Path,
    operations: tuple[RegistryFieldOperation, ...],
    *,
    owner: str,
    expected_generation_id: str | None = None,
    as_of: datetime | None = None,
) -> PreparedRegistryTransaction:
    """§6.7 steps 1-6. Raises `RegistryTransactionValidationError` or
    `RegistryGenerationStale` (releasing the lease first in either case)
    without ever creating a staged transaction directory or touching a
    live file. On success, the lease remains HELD -- the caller must
    either hand the returned `PreparedRegistryTransaction` to a future
    commit step (PPL-W1.5) or call `abort_prepared_registry_transaction`
    to release it."""
    config = load_registry_config(knowledge_root)
    manifest = load_registry_manifest(knowledge_root)
    if config is None or manifest is None:
        raise ConfigError(
            "The registry has not been bootstrapped yet. Run 'vertex kb registry bootstrap --apply "
            "--customer-boundary-id <id>' first -- a transaction cannot be prepared without an existing manifest."
        )

    lease = acquire_registry_lease(owner, knowledge_root=knowledge_root)

    if expected_generation_id is not None and expected_generation_id != manifest.generation_id:
        release_registry_lease(lease, knowledge_root=knowledge_root)
        raise RegistryGenerationStale(expected_generation_id, manifest.generation_id)

    baseline = _load_synthetic_records(synthetic_records_path(knowledge_root))
    baseline_by_id = {record.record_id: record for record in baseline}
    merged = _apply_operations(baseline, operations)

    violations = _validate_transaction(baseline_by_id=baseline_by_id, merged=merged, operations=operations)
    if violations:
        release_registry_lease(lease, knowledge_root=knowledge_root)
        raise RegistryTransactionValidationError(violations)

    now = as_of or datetime.now(timezone.utc)
    # Hyphenated, not colon-separated ("workspace:<ULID>"'s convention) --
    # this ID is used directly as a filesystem directory name under
    # `.transactions/`, and `:` is invalid in an NTFS path component.
    transaction_id = f"registry-tx-{new_ulid(now)}"
    staged_dir = transactions_root(knowledge_root) / transaction_id
    final_records = tuple(sorted(merged.values(), key=lambda r: r.record_id))

    staged_records_path = staged_dir / _SYNTHETIC_RECORDS_FILENAME
    _write_synthetic_records(staged_records_path, final_records)

    # Step 5's "journal/outbox records" placeholder -- see module docstring.
    operations_summary = {
        "schema_version": "1.0",
        "note": "Provisional operations summary. Superseded by PPL-W1.6 (durable outbox) and PPL-W1.7 (signed change journal) once those land.",
        "transaction_id": transaction_id,
        "recorded_at": now.isoformat(),
        "operations": [
            {"kind": operation.kind.value, "record_id": operation.target_record_id()}
            for operation in operations
        ],
    }
    (staged_dir / _OPERATIONS_SUMMARY_FILENAME).write_text(json.dumps(operations_summary, indent=2, sort_keys=True), encoding="utf-8")

    # Step 6: reload through the SAME production loader to prove round-trip fidelity.
    reloaded = _load_synthetic_records(staged_records_path)
    if reloaded != final_records:
        release_registry_lease(lease, knowledge_root=knowledge_root)
        shutil.rmtree(staged_dir, ignore_errors=True)
        raise ConfigError(
            f"Staged transaction {transaction_id} failed its production-loader reparse check: "
            "the reloaded records did not match the in-memory model that was staged. Serializer/schema drift suspected."
        )

    manifest_preview = PreparedRegistryManifestPreview(
        generation_id=new_ulid(now),
        prior_generation=manifest.generation_id,
        workspace_id=config.workspace_id,
        customer_boundary_id=config.customer_boundary_id,
        fencing_token=lease.fencing_token,
        transaction_id=transaction_id,
        source_hashes=((_SYNTHETIC_RECORDS_FILENAME, compute_file_checksum(staged_records_path)),),
    )

    return PreparedRegistryTransaction(
        transaction_id=transaction_id,
        staged_dir=staged_dir,
        lease=lease,
        manifest_preview=manifest_preview,
        final_records=final_records,
    )


def abort_prepared_registry_transaction(
    prepared: PreparedRegistryTransaction | PreparedRegistryFilesTransaction, *, knowledge_root: Path
) -> None:
    """Not one of §6.7's numbered steps -- this module's own provisional
    escape hatch for releasing a lease and discarding staged files after a
    prepare that will never proceed to `commit_registry_transaction`
    (e.g. a caller decides not to proceed). Once `commit_registry_transaction`
    has been called (i.e. a `state.json` exists in the staged directory),
    do not call this -- use `recover_registry_transactions` instead, which
    knows how to unwind a partially-committed transaction safely."""
    release_registry_lease(prepared.lease, knowledge_root=knowledge_root)
    shutil.rmtree(prepared.staged_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# PPL-W1.5: staged transaction primitive, commit half (§6.7 steps 7-10).
# ---------------------------------------------------------------------------


class RegistryTransactionStatus(str, Enum):
    PREPARED = "PREPARED"
    COMMITTED = "COMMITTED"


@dataclass(frozen=True, slots=True)
class CommittedRegistryTransaction:
    transaction_id: str
    manifest: RegistryManifest


@dataclass(frozen=True, slots=True)
class RegistryRecoveryOutcome:
    transaction_id: str
    action: str
    detail: str


def _transaction_state_path(staged_dir: Path) -> Path:
    return staged_dir / _TRANSACTION_STATE_FILENAME


def _checkpoint_dir(staged_dir: Path) -> Path:
    return staged_dir / _CHECKPOINT_DIRNAME


def _generation_snapshot_root(knowledge_root: Path, generation_id: str) -> Path:
    """Keep a read-only comparison baseline for manifest-drift diagnosis.

    Checkpoints protect a failed transaction, but a successful first write has
    no prior file to checkpoint.  A generation snapshot lets later direct
    edits be classified precisely without ever treating it as authority; the
    manifest hash remains the source-of-truth integrity signal.
    """
    return knowledge_root / ".state" / "registry_snapshots" / generation_id


def _write_generation_snapshots(
    knowledge_root: Path,
    *,
    generation_id: str,
    relative_paths: tuple[str, ...],
) -> None:
    snapshot_root = _generation_snapshot_root(knowledge_root, generation_id)
    for relative_path in relative_paths:
        source = knowledge_root / relative_path
        if not source.exists():
            continue
        destination = snapshot_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def _replace_with_bounded_retry(source: Path, destination: Path) -> None:
    """§6.7 step 8: "bounded retry/backoff for rename-over-open-file and
    qualified network-drive failures." `os.replace` is already atomic at
    the filesystem level -- retry exists only for *transient* failures
    (an AV scanner or reader briefly holding the file open, a network
    share's momentary contention), not to make a non-atomic operation
    atomic."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    delay = _REPLACE_INITIAL_DELAY_SECONDS
    last_error: OSError | None = None
    for attempt in range(_REPLACE_MAX_ATTEMPTS):
        try:
            os.replace(source, destination)
            return
        except OSError as error:
            last_error = error
            if attempt < _REPLACE_MAX_ATTEMPTS - 1:
                time.sleep(delay)
                delay *= 2
    raise ConfigError(
        f"Failed to replace {destination} after {_REPLACE_MAX_ATTEMPTS} attempts "
        f"(rename-over-open-file/network-drive contention suspected): {last_error}"
    ) from last_error


def commit_registry_transaction(
    prepared: PreparedRegistryTransaction,
    *,
    knowledge_root: Path,
    as_of: datetime | None = None,
    _simulate_crash_after_step: int | None = None,
) -> CommittedRegistryTransaction:
    """§6.7 steps 7-10. `_simulate_crash_after_step` (7, 8, or 9) is a
    test-only seam: it returns early right after the named step, leaving
    exactly the on-disk state a real crash at that point would leave, so
    `recover_registry_transactions` can be exercised deterministically
    without actually killing the process. Never pass it outside tests."""
    now = as_of or datetime.now(timezone.utc)
    live_data_path = synthetic_records_path(knowledge_root)
    live_manifest_path = registry_manifest_path(knowledge_root)
    staged_data_path = prepared.staged_dir / _SYNTHETIC_RECORDS_FILENAME
    checkpoint_dir = _checkpoint_dir(prepared.staged_dir)
    state_path = _transaction_state_path(prepared.staged_dir)

    # Step 7: checkpoint the OLD live files (before anything live is
    # touched) + an explicit PREPARED recovery record.
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    if live_data_path.exists():
        shutil.copy2(live_data_path, checkpoint_dir / _SYNTHETIC_RECORDS_FILENAME)
    if live_manifest_path.exists():
        shutil.copy2(live_manifest_path, checkpoint_dir / "registry_manifest.json")
    new_data_hash = dict(prepared.manifest_preview.source_hashes)[_SYNTHETIC_RECORDS_FILENAME]
    state = {
        "transaction_id": prepared.transaction_id,
        "status": RegistryTransactionStatus.PREPARED.value,
        "prior_generation": prepared.manifest_preview.prior_generation,
        "new_generation": prepared.manifest_preview.generation_id,
        "new_data_hash": new_data_hash,
        "fencing_token": prepared.manifest_preview.fencing_token,
        "lease_owner": prepared.lease.owner,
        "workspace_id": prepared.manifest_preview.workspace_id,
        "customer_boundary_id": prepared.manifest_preview.customer_boundary_id,
        "prepared_at": now.isoformat(),
        "committed_at": None,
    }
    _write_json_atomic(state_path, state)
    if _simulate_crash_after_step == 7:
        return CommittedRegistryTransaction(transaction_id=prepared.transaction_id, manifest=_manifest_from_preview(prepared.manifest_preview, committed_at=now))

    # Step 8: revalidate the fencing token immediately before the
    # irreversible replace, then replace the data file(s) in deterministic
    # order (a single file today; multiple real files sort by name once
    # Phase 2a's real schemas land).
    current_lease = read_registry_lease_state(knowledge_root=knowledge_root)
    if current_lease is None or current_lease.owner != prepared.lease.owner or current_lease.fencing_token != prepared.lease.fencing_token:
        raise RegistryLeaseFencingTokenStale(
            prepared.lease.fencing_token,
            current_lease.fencing_token if current_lease is not None else 0,
            mutation_domain=prepared.lease.mutation_domain,
        )
    _replace_with_bounded_retry(staged_data_path, live_data_path)
    if _simulate_crash_after_step == 8:
        return CommittedRegistryTransaction(transaction_id=prepared.transaction_id, manifest=_manifest_from_preview(prepared.manifest_preview, committed_at=now))

    # Step 9: replace registry_manifest.json LAST, making the new
    # generation visible to lock-free readers.
    new_manifest = _manifest_from_preview(prepared.manifest_preview, committed_at=now)
    write_registry_manifest(live_manifest_path, new_manifest)
    if _simulate_crash_after_step == 9:
        return CommittedRegistryTransaction(transaction_id=prepared.transaction_id, manifest=new_manifest)

    # Step 10: mark COMMITTED, dispatch the outbox idempotently (a no-op
    # placeholder until PPL-W1.6's real durable outbox exists -- there are
    # no "affected active programs" registered yet in Phase 1), release
    # the lease.
    state["status"] = RegistryTransactionStatus.COMMITTED.value
    state["committed_at"] = now.isoformat()
    _write_json_atomic(state_path, state)
    release_registry_lease(prepared.lease, knowledge_root=knowledge_root)

    return CommittedRegistryTransaction(transaction_id=prepared.transaction_id, manifest=new_manifest)


def commit_registry_files_transaction(
    prepared: PreparedRegistryFilesTransaction,
    *,
    knowledge_root: Path,
    as_of: datetime | None = None,
) -> CommittedRegistryTransaction:
    """Commit a typed registry transaction prepared by
    :func:`prepare_registry_files_transaction`.

    Every factual file is checkpointed and replaced in sorted path order
    under the same fencing token; the manifest remains the final visibility
    switch.  This is the only multi-file publication path for shared
    entities, people, and teams.
    """
    now = as_of or datetime.now(timezone.utc)
    checkpoint_dir = _checkpoint_dir(prepared.staged_dir)
    state_path = _transaction_state_path(prepared.staged_dir)
    source_hashes = dict(prepared.manifest_preview.source_hashes)
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    existing_paths: list[str] = []
    for relative_path in prepared.relative_paths:
        live_path = knowledge_root / relative_path
        if live_path.exists():
            existing_paths.append(relative_path)
            checkpoint_path = checkpoint_dir / relative_path
            checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(live_path, checkpoint_path)

    state = {
        "transaction_id": prepared.transaction_id,
        "status": RegistryTransactionStatus.PREPARED.value,
        "prior_generation": prepared.manifest_preview.prior_generation,
        "new_generation": prepared.manifest_preview.generation_id,
        "new_data_hashes": source_hashes,
        "relative_paths": list(prepared.relative_paths),
        "existing_paths": existing_paths,
        "fencing_token": prepared.manifest_preview.fencing_token,
        "lease_owner": prepared.lease.owner,
        "workspace_id": prepared.manifest_preview.workspace_id,
        "customer_boundary_id": prepared.manifest_preview.customer_boundary_id,
        "prepared_at": now.isoformat(),
        "committed_at": None,
    }
    _write_json_atomic(state_path, state)

    for relative_path in prepared.relative_paths:
        current_lease = read_registry_lease_state(knowledge_root=knowledge_root)
        if (
            current_lease is None
            or current_lease.owner != prepared.lease.owner
            or current_lease.fencing_token != prepared.lease.fencing_token
        ):
            raise RegistryLeaseFencingTokenStale(
                prepared.lease.fencing_token,
                current_lease.fencing_token if current_lease is not None else 0,
                mutation_domain=prepared.lease.mutation_domain,
            )
        _replace_with_bounded_retry(prepared.staged_dir / relative_path, knowledge_root / relative_path)

    new_manifest = _manifest_from_preview(prepared.manifest_preview, committed_at=now)
    write_registry_manifest(registry_manifest_path(knowledge_root), new_manifest)
    _write_generation_snapshots(
        knowledge_root,
        generation_id=new_manifest.generation_id,
        # Snapshot every manifest-managed source available in this committed
        # generation, not only the file(s) this transaction changed.  A
        # later one-file mutation still needs a baseline to classify a
        # manual edit to an untouched managed file as DIR-14A vs DIR-14B.
        relative_paths=tuple(relative_path for relative_path, _ in new_manifest.source_hashes),
    )
    state["status"] = RegistryTransactionStatus.COMMITTED.value
    state["committed_at"] = now.isoformat()
    _write_json_atomic(state_path, state)
    release_registry_lease(prepared.lease, knowledge_root=knowledge_root)
    return CommittedRegistryTransaction(transaction_id=prepared.transaction_id, manifest=new_manifest)


def _manifest_from_preview(preview: PreparedRegistryManifestPreview, *, committed_at: datetime) -> RegistryManifest:
    return RegistryManifest(
        generation_id=preview.generation_id,
        prior_generation=preview.prior_generation,
        workspace_id=preview.workspace_id,
        customer_boundary_id=preview.customer_boundary_id,
        fencing_token=preview.fencing_token,
        schema_version=REGISTRY_MANIFEST_SCHEMA_VERSION,
        compiler_version=REGISTRY_COMPILER_VERSION,
        source_hashes=preview.source_hashes,
        transaction_id=preview.transaction_id,
        committed_at=committed_at,
    )


def recover_registry_transactions(knowledge_root: Path, *, as_of: datetime | None = None) -> tuple[RegistryRecoveryOutcome, ...]:
    """Startup/doctor recovery for every crash point in §6.7. Scans
    `knowledge/.transactions/*/` and, for each staged transaction, decides
    which of the four documented crash points (if any) it represents by
    comparing the live manifest's generation and the live data file's
    hash against what the transaction recorded -- never by trusting the
    `status` field alone, since that field is exactly what a crash can
    leave stale."""
    root = transactions_root(knowledge_root)
    if not root.exists():
        return ()

    manifest = load_registry_manifest(knowledge_root)
    live_generation = manifest.generation_id if manifest is not None else None

    outcomes: list[RegistryRecoveryOutcome] = []
    for staged_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        transaction_id = staged_dir.name
        state_path = _transaction_state_path(staged_dir)
        if not state_path.exists():
            # Crashed before step 7 finished writing the PREPARED record
            # (or a prepare that was simply never committed). Nothing live
            # was ever touched -- safe to discard.
            shutil.rmtree(staged_dir, ignore_errors=True)
            outcomes.append(RegistryRecoveryOutcome(transaction_id, "rolled_back_staged", "No PREPARED record found; discarded an incomplete/uncommitted staged transaction."))
            continue

        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state["status"] == RegistryTransactionStatus.COMMITTED.value:
            outcomes.append(RegistryRecoveryOutcome(transaction_id, "no_action_needed", "Transaction already fully committed."))
            continue

        # Phase 1's synthetic transaction stored one `new_data_hash`; real
        # shared-file transactions store the path-to-hash map below.  Keep
        # accepting the original shape so recovery remains backward
        # compatible with already-staged Phase 1 transactions.
        new_data_hashes = state.get("new_data_hashes")
        if not isinstance(new_data_hashes, dict):
            new_data_hashes = {_SYNTHETIC_RECORDS_FILENAME: state["new_data_hash"]}
        relative_paths = tuple(sorted(str(path) for path in new_data_hashes))
        replaced_paths = tuple(
            relative_path
            for relative_path in relative_paths
            if (knowledge_root / relative_path).exists()
            and compute_file_checksum(knowledge_root / relative_path) == new_data_hashes[relative_path]
        )
        all_data_already_replaced = len(replaced_paths) == len(relative_paths)
        manifest_already_swapped = live_generation == state["new_generation"]

        if all_data_already_replaced and manifest_already_swapped:
            # Crash point 3: "New manifest with incomplete journal/outbox
            # dispatch: retain data and replay the idempotent outbox."
            # The placeholder outbox has nothing to replay; finish step
            # 10's bookkeeping and release the lease if it's still held.
            state["status"] = RegistryTransactionStatus.COMMITTED.value
            state["committed_at"] = (as_of or datetime.now(timezone.utc)).isoformat()
            _write_json_atomic(state_path, state)
            held_lease = read_registry_lease_state(knowledge_root=knowledge_root)
            if held_lease is not None and held_lease.owner == state["lease_owner"] and held_lease.fencing_token == state["fencing_token"]:
                release_registry_lease(held_lease, knowledge_root=knowledge_root)
            outcomes.append(RegistryRecoveryOutcome(transaction_id, "completed_commit_bookkeeping", "New generation was already live; finished commit bookkeeping and released the lease."))
        elif replaced_paths and not manifest_already_swapped:
            # Crash point 2: "Partial file replacement with old manifest:
            # restore from checkpoint."
            for relative_path in replaced_paths:
                live_data_path = knowledge_root / relative_path
                checkpoint_data_path = _checkpoint_dir(staged_dir) / relative_path
                if checkpoint_data_path.exists():
                    _replace_with_bounded_retry(checkpoint_data_path, live_data_path)
                elif live_data_path.exists():
                    # No checkpoint means this transaction created the file;
                    # do not leave that unmanifested creation behind.
                    live_data_path.unlink()
            shutil.rmtree(staged_dir, ignore_errors=True)
            held_lease = read_registry_lease_state(knowledge_root=knowledge_root)
            if held_lease is not None and held_lease.owner == state["lease_owner"] and held_lease.fencing_token == state["fencing_token"]:
                release_registry_lease(held_lease, knowledge_root=knowledge_root)
            outcomes.append(RegistryRecoveryOutcome(transaction_id, "restored_from_checkpoint", "Data file was replaced but the manifest was not; restored the pre-transaction checkpoint."))
        else:
            # Crash point 1: "PREPARED with unchanged live manifest: roll
            # back staged files." Nothing live was touched yet.
            shutil.rmtree(staged_dir, ignore_errors=True)
            held_lease = read_registry_lease_state(knowledge_root=knowledge_root)
            if held_lease is not None and held_lease.owner == state["lease_owner"] and held_lease.fencing_token == state["fencing_token"]:
                release_registry_lease(held_lease, knowledge_root=knowledge_root)
            outcomes.append(RegistryRecoveryOutcome(transaction_id, "rolled_back_staged", "Live data/manifest unchanged; discarded staged transaction."))

    return tuple(outcomes)


def detect_stale_registry_lease(knowledge_root: Path, *, as_of: datetime | None = None) -> RegistryLeaseHandle | None:
    """Crash point 4: "Stale lease: wait for TTL or use `vertex kb
    registry lease release --force --reason <text>`." Detection only --
    this module never force-releases a lease itself; that requires an
    authorized principal and an explicit reason (§6.6/§6.7), which only a
    human-invoked CLI call can supply. Returns the held (and expired)
    lease handle, or `None` if no lease is held or it has not expired."""
    handle = read_registry_lease_state(knowledge_root=knowledge_root)
    if handle is None:
        return None
    if is_registry_lease_expired(handle, at=as_of):
        return handle
    return None
