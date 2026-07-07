from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.exceptions import StateError


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKUP_MANIFEST_NAME = "backup_manifest.json"
_BACKUP_ROOTS = ("programs", "knowledge", "editions")


@dataclass(frozen=True, slots=True)
class BackupFileRecord:
    relative_path: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class RepositoryBackupResult:
    destination_root: Path
    manifest_path: Path
    file_count: int


@dataclass(frozen=True, slots=True)
class RepositoryRestoreResult:
    """WS-23: result of a backup → destination restore operation."""
    source_backup_root: Path
    destination_root: Path
    file_count: int
    preflight_verified: bool
    manifest_path: Path


@dataclass(frozen=True, slots=True)
class BackupVerificationResult:
    checked_file_count: int
    missing_paths: tuple[str, ...]
    mismatched_paths: tuple[str, ...]

    @property
    def is_valid(self) -> bool:
        return not self.missing_paths and not self.mismatched_paths


@dataclass(frozen=True, slots=True)
class BackupReferenceHit:
    backup_root: Path
    manifest_path: Path
    matching_paths: tuple[str, ...]


def create_repository_backup(
    destination_root: Path,
    *,
    source_root: Path = REPO_ROOT,
) -> RepositoryBackupResult:
    resolved_source_root = source_root.resolve()
    resolved_destination_root = destination_root.resolve()
    _prepare_destination_root(resolved_destination_root)

    file_records: list[BackupFileRecord] = []
    included_roots: list[str] = []

    for root_name in _BACKUP_ROOTS:
        source_path = resolved_source_root / root_name
        if not source_path.exists():
            continue
        included_roots.append(root_name)
        for source_file in _iter_files(source_path):
            relative_path = source_file.relative_to(resolved_source_root)
            destination_file = resolved_destination_root / relative_path
            destination_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, destination_file)
            file_records.append(
                BackupFileRecord(
                    relative_path=relative_path.as_posix(),
                    sha256=_hash_file(destination_file),
                    size_bytes=destination_file.stat().st_size,
                )
            )

    manifest_path = resolved_destination_root / BACKUP_MANIFEST_NAME
    manifest_payload = {
        "schema_version": "1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_root": str(resolved_source_root),
        "included_roots": included_roots,
        "files": [
            {
                "relative_path": record.relative_path,
                "sha256": record.sha256,
                "size_bytes": record.size_bytes,
            }
            for record in file_records
        ],
    }
    _write_atomic_json(manifest_path, manifest_payload)
    return RepositoryBackupResult(
        destination_root=resolved_destination_root,
        manifest_path=manifest_path,
        file_count=len(file_records),
    )


def verify_repository_backup(destination_root: Path) -> BackupVerificationResult:
    resolved_destination_root = destination_root.resolve()
    manifest = _load_manifest(resolved_destination_root / BACKUP_MANIFEST_NAME)

    missing_paths: list[str] = []
    mismatched_paths: list[str] = []
    files = manifest.get("files")
    if not isinstance(files, list):
        raise StateError("Backup manifest is missing a valid 'files' list.")

    for entry in files:
        if not isinstance(entry, dict):
            raise StateError("Backup manifest contains an invalid file entry.")
        relative_path = entry.get("relative_path")
        expected_sha = entry.get("sha256")
        if not isinstance(relative_path, str) or not relative_path:
            raise StateError("Backup manifest contains an invalid relative_path value.")
        if not isinstance(expected_sha, str) or not expected_sha.startswith("sha256:"):
            raise StateError(f"Backup manifest contains an invalid checksum for {relative_path}.")

        file_path = resolved_destination_root / Path(relative_path)
        if not file_path.exists():
            missing_paths.append(relative_path)
            continue

        if _hash_file(file_path) != expected_sha:
            mismatched_paths.append(relative_path)

    return BackupVerificationResult(
        checked_file_count=len(files),
        missing_paths=tuple(missing_paths),
        mismatched_paths=tuple(mismatched_paths),
    )


def find_backups_referencing_paths(search_root: Path, *, relative_paths: set[str]) -> tuple[BackupReferenceHit, ...]:
    if not relative_paths:
        return ()
    resolved_search_root = search_root.resolve()
    if not resolved_search_root.exists() or not resolved_search_root.is_dir():
        return ()
    hits: list[BackupReferenceHit] = []
    for manifest_path in sorted(resolved_search_root.glob(f"**/{BACKUP_MANIFEST_NAME}")):
        payload = _load_manifest(manifest_path)
        files = payload.get("files")
        if not isinstance(files, list):
            continue
        matching = sorted(
            str(entry.get("relative_path"))
            for entry in files
            if isinstance(entry, dict) and isinstance(entry.get("relative_path"), str) and entry["relative_path"] in relative_paths
        )
        if not matching:
            continue
        hits.append(
            BackupReferenceHit(
                backup_root=manifest_path.parent,
                manifest_path=manifest_path,
                matching_paths=tuple(matching),
            )
        )
    return tuple(hits)


# WS-23: restore a backup to a destination root. Refuses unless the backup
# pre-flight verifies (no missing/mismatched files in the backup itself).
# The destination root MUST be empty (we never overwrite a live program
# tree — that would defeat the whole purpose of the restore).
def restore_repository_backup(
    backup_root: Path,
    destination_root: Path,
    *,
    skip_preflight: bool = False,
) -> RepositoryRestoreResult:
    """Restore a backup created by `create_repository_backup` to `destination_root`.

    Args:
        backup_root: the directory holding the backup (must contain
            `BACKUP_MANIFEST_NAME` + the file tree).
        destination_root: where to copy the restored files. **Must be empty.**
        skip_preflight: if True, skip the verify step. ONLY for the clean-machine
            drill where the operator has independently verified; defaults to False
            so a corrupt backup cannot be silently restored.

    Raises:
        StateError if the backup doesn't verify, the destination isn't empty,
        or any restore copy fails.
    """
    resolved_backup_root = backup_root.resolve()
    resolved_destination_root = destination_root.resolve()

    if not skip_preflight:
        verify_result = verify_repository_backup(resolved_backup_root)
        if not verify_result.is_valid:
            details = []
            if verify_result.missing_paths:
                details.append(f"missing={list(verify_result.missing_paths[:3])}")
            if verify_result.mismatched_paths:
                details.append(f"mismatched={list(verify_result.mismatched_paths[:3])}")
            raise StateError(
                f"Backup pre-flight failed; restore refused: {'; '.join(details)}"
            )

    manifest = _load_manifest(resolved_backup_root / BACKUP_MANIFEST_NAME)
    files = manifest.get("files")
    if not isinstance(files, list):
        raise StateError("Backup manifest is missing a valid 'files' list.")

    # Refuse to restore into a non-empty destination. This is the safety
    # guarantee: a restore is a *recovery* operation, not a merge.
    if resolved_destination_root.exists():
        if not resolved_destination_root.is_dir():
            raise StateError(
                f"Restore destination must be a directory: {resolved_destination_root}"
            )
        if any(resolved_destination_root.iterdir()):
            raise StateError(
                f"Restore destination must be empty: {resolved_destination_root}"
            )
    resolved_destination_root.mkdir(parents=True, exist_ok=True)

    restored_count = 0
    for entry in files:
        if not isinstance(entry, dict):
            raise StateError("Backup manifest contains an invalid file entry.")
        relative_path = entry.get("relative_path")
        if not isinstance(relative_path, str) or not relative_path:
            raise StateError("Backup manifest contains an invalid relative_path value.")

        source_file = resolved_backup_root / Path(relative_path)
        if not source_file.exists():
            raise StateError(
                f"Backup manifest references missing file: {relative_path}"
            )
        destination_file = resolved_destination_root / Path(relative_path)
        destination_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, destination_file)
        restored_count += 1

    return RepositoryRestoreResult(
        source_backup_root=resolved_backup_root,
        destination_root=resolved_destination_root,
        file_count=restored_count,
        preflight_verified=not skip_preflight,
        manifest_path=resolved_backup_root / BACKUP_MANIFEST_NAME,
    )


def _prepare_destination_root(destination_root: Path) -> None:
    if destination_root.exists() and not destination_root.is_dir():
        raise StateError(f"Backup destination must be a directory: {destination_root}")
    if destination_root.exists() and any(destination_root.iterdir()):
        raise StateError(f"Backup destination must be empty: {destination_root}")
    destination_root.mkdir(parents=True, exist_ok=True)


def _iter_files(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(path for path in root.rglob("*") if path.is_file()))


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise StateError(f"Backup manifest was not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise StateError(f"Backup manifest is not valid JSON: {path}") from error
    if not isinstance(payload, dict):
        raise StateError("Backup manifest must be a JSON object.")
    return payload


def _write_atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)