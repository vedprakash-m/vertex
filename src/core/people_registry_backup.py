"""specs/people.md Phase 1, PPL-W1.8 (backup half): manifest-consistent
registry backup/restore.

§7.10: "`vertex backup` already covers the `knowledge/` root; Phase 1
extends its manifest classification and restore drill for the registry
generation, journal/archive, transaction recovery records,
policies/bindings, and encrypted artifacts... Backup reads one committed
registry manifest, copies exactly that generation plus required
journal/checkpoint/policy files, then re-reads/verifies the same
manifest. A directory walk spanning a generation transition is not a
valid backup."

`src/core/backup.py::create_repository_backup` already whole-tree-walks
`knowledge/` (plus `programs/`, `editions/`) -- fine for most files, but
NOT generation-consistency-safe for the registry specifically: nothing
stops a concurrent commit (PPL-W1.5) from swapping `registry_manifest.json`
mid-walk, leaving a snapshot with data files from one generation paired
with a manifest from another. This module is the targeted fix for that
one gap; it does not replace `create_repository_backup`, which remains
the right tool for the rest of the repository. Reads never acquire the
workspace lease (§6.7: "Reads never acquire the writer lease") -- instead,
the live manifest is read once before copying and re-read once after;
any concurrent commit necessarily swaps `registry_manifest.json`'s
`generation_id` (step 9 of the commit sequence), so a before/after
comparison catches ANY race regardless of which individual file it
straddled, without needing a lock held for the whole copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil

from src.core.exceptions import ConfigError
from src.core.jsonl_utils import compute_file_checksum
from src.core.people_change_journal import STREAM_PEOPLE_CHANGES, STREAM_PEOPLE_CONFLICTS, journal_active_path
from src.core.people_registry_identity import load_registry_manifest, registry_config_path, registry_manifest_path
from src.core.people_registry_storage_class import registry_capability_status_path
from src.core.people_registry_transaction import synthetic_records_path, transactions_root

BACKUP_SNAPSHOT_MANIFEST_NAME = "registry_backup_manifest.json"


@dataclass(frozen=True, slots=True)
class RegistryBackupFileRecord:
    relative_path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class RegistryBackupSnapshotResult:
    destination_root: Path
    generation_id: str
    file_count: int


@dataclass(frozen=True, slots=True)
class RegistryRestoreResult:
    destination_root: Path
    generation_id: str
    file_count: int
    verified: bool


class RegistryGenerationChangedDuringBackup(ConfigError):
    def __init__(self, before: str, after: str) -> None:
        self.before = before
        self.after = after
        super().__init__(f"Registry generation changed during backup (was {before!r}, now {after!r}). Retry the backup.")


def _collect_registry_backup_paths(knowledge_root: Path) -> tuple[Path, ...]:
    """Every artifact §7.10 names: "the registry generation, journal/
    archive, transaction recovery records, policies/bindings, and
    encrypted artifacts." Deliberately excludes
    `.state/registry_lease.sqlite3` (ephemeral operational lock state, not
    durable data) and `.state/registry_outbox.sqlite3` (durable but not
    yet carrying real payloads -- PPL-W1.6 is not wired into commit yet;
    revisit once it is)."""
    candidates = [
        registry_config_path(knowledge_root),
        registry_manifest_path(knowledge_root),
        registry_capability_status_path(knowledge_root),
        synthetic_records_path(knowledge_root),
        journal_active_path(knowledge_root, STREAM_PEOPLE_CHANGES),
        journal_active_path(knowledge_root, STREAM_PEOPLE_CONFLICTS),
    ]
    journal_archive_root = knowledge_root / "_journal" / "archive"
    if journal_archive_root.exists():
        candidates.extend(sorted(path for path in journal_archive_root.rglob("*") if path.is_file()))
    transactions_dir = transactions_root(knowledge_root)
    if transactions_dir.exists():
        candidates.extend(sorted(path for path in transactions_dir.rglob("*") if path.is_file()))
    return tuple(path for path in candidates if path.exists())


def create_registry_backup_snapshot(knowledge_root: Path, destination_root: Path) -> RegistryBackupSnapshotResult:
    manifest_before = load_registry_manifest(knowledge_root)
    if manifest_before is None:
        raise ConfigError("Cannot back up a registry that has not been bootstrapped yet (no registry_manifest.json).")

    destination_root.mkdir(parents=True, exist_ok=True)
    if any(destination_root.iterdir()):
        raise ConfigError(f"Registry backup destination must be empty: {destination_root}")

    file_records: list[RegistryBackupFileRecord] = []
    for source_path in _collect_registry_backup_paths(knowledge_root):
        relative_path = source_path.relative_to(knowledge_root)
        destination_path = destination_root / relative_path
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination_path)
        file_records.append(RegistryBackupFileRecord(relative_path=relative_path.as_posix(), sha256=compute_file_checksum(destination_path)))

    # "then re-reads/verifies the same manifest" -- see module docstring.
    manifest_after = load_registry_manifest(knowledge_root)
    if manifest_after is None or manifest_after.generation_id != manifest_before.generation_id:
        shutil.rmtree(destination_root, ignore_errors=True)
        raise RegistryGenerationChangedDuringBackup(
            manifest_before.generation_id, manifest_after.generation_id if manifest_after is not None else "<missing>"
        )

    snapshot_manifest_path = destination_root / BACKUP_SNAPSHOT_MANIFEST_NAME
    snapshot_payload = {
        "schema_version": "1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "generation_id": manifest_before.generation_id,
        "workspace_id": manifest_before.workspace_id,
        "files": [{"relative_path": record.relative_path, "sha256": record.sha256} for record in file_records],
    }
    _write_atomic_json(snapshot_manifest_path, snapshot_payload)

    return RegistryBackupSnapshotResult(destination_root=destination_root, generation_id=manifest_before.generation_id, file_count=len(file_records))


def restore_registry_backup_snapshot(snapshot_dir: Path, knowledge_root: Path) -> RegistryRestoreResult:
    snapshot_manifest_path = snapshot_dir / BACKUP_SNAPSHOT_MANIFEST_NAME
    if not snapshot_manifest_path.exists():
        raise ConfigError(f"Registry backup snapshot manifest not found: {snapshot_manifest_path}")
    snapshot_payload = json.loads(snapshot_manifest_path.read_text(encoding="utf-8"))

    knowledge_root.mkdir(parents=True, exist_ok=True)
    for entry in snapshot_payload["files"]:
        relative_path = Path(entry["relative_path"])
        source_path = snapshot_dir / relative_path
        destination_path = knowledge_root / relative_path
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination_path)
        if compute_file_checksum(destination_path) != entry["sha256"]:
            raise ConfigError(f"Restored file hash mismatch for {relative_path}; restore aborted mid-way, registry may be inconsistent.")

    # "then re-reads/verifies the same manifest"
    restored_manifest = load_registry_manifest(knowledge_root)
    verified = restored_manifest is not None and restored_manifest.generation_id == snapshot_payload["generation_id"]
    if restored_manifest is None or not verified:
        raise ConfigError(
            f"Restored registry manifest generation "
            f"({restored_manifest.generation_id if restored_manifest is not None else '<missing>'}) does not match the "
            f"backup snapshot's recorded generation ({snapshot_payload['generation_id']})."
        )

    return RegistryRestoreResult(
        destination_root=knowledge_root,
        generation_id=restored_manifest.generation_id,
        file_count=len(snapshot_payload["files"]),
        verified=verified,
    )


def _write_atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)
