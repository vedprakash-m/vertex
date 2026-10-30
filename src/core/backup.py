from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from src.core.exceptions import StateError
from src.core.checkpoint_store import SQLITE_BACKUP_SUFFIXES, _copy_checkpoint_file
from src.core.reality_store import get_program_reality_db_path
from src.core.workspace_paths import is_bare_identifier


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
    capture_scope: str = "repository_subset"
    declared_scope_check: str = "not_checked"
    absent_unassessed_store_count: int | None = 0


@dataclass(frozen=True, slots=True)
class RepositoryRestoreResult:
    """WS-23: result of a backup → destination restore operation."""
    source_backup_root: Path
    destination_root: Path
    file_count: int
    preflight_verified: bool
    manifest_path: Path
    capture_scope: str = "legacy_unspecified"
    declared_scope_check: str = "not_checked"
    absent_unassessed_store_count: int | None = None


@dataclass(frozen=True, slots=True)
class BackupVerificationResult:
    checked_file_count: int
    missing_paths: tuple[str, ...]
    mismatched_paths: tuple[str, ...]
    capture_scope: str = "legacy_unspecified"
    declared_scope_check: str = "unassessed"
    absent_unassessed_store_count: int | None = None

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
    declared_scope = _capture_scope(source_root.absolute())
    resolved_source_root = source_root.resolve()
    resolved_destination_root = destination_root.resolve()
    _prepare_destination_root(resolved_destination_root)

    file_records: list[BackupFileRecord] = []
    included_roots: list[str] = []

    source_files: list[Path] = []
    for root_name in _BACKUP_ROOTS:
        source_path = resolved_source_root / root_name
        source_kind = _path_kind(source_path)
        if source_kind is None:
            continue
        if source_kind != "dir":
            raise StateError("Backup scope roots must be directories.")
        included_roots.append(root_name)
        source_files.extend(_iter_files(source_path))
    source_files.extend(
        resolved_source_root / record["relative_path"]
        for record in declared_scope["fact_reality_stores"]
        if record["source_state"] == "present"
    )
    for source_file in source_files:
        if source_file.name.endswith(("-wal", "-shm", "-journal")):
            database = source_file.with_name(source_file.name.rsplit("-", 1)[0])
            if database.suffix.lower() in SQLITE_BACKUP_SUFFIXES and database.is_file():
                continue
        relative_path = source_file.relative_to(resolved_source_root)
        destination_file = resolved_destination_root / relative_path
        destination_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            _copy_checkpoint_file(source_file, destination_file)
        except (sqlite3.Error, OSError) as error:
            raise StateError(f"Backup file capture failed (including SQLite): {relative_path}") from error
        file_records.append(
            BackupFileRecord(
                relative_path=relative_path.as_posix(),
                sha256=_hash_file(destination_file),
                size_bytes=destination_file.stat().st_size,
            )
        )

    manifest_path = resolved_destination_root / BACKUP_MANIFEST_NAME
    manifest_payload = {
        "schema_version": "1.1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_root": str(resolved_source_root),
        "included_roots": included_roots,
        "declared_scope": declared_scope,
        "files": [
            {
                "relative_path": record.relative_path,
                "sha256": record.sha256,
                "size_bytes": record.size_bytes,
            }
            for record in file_records
        ],
    }
    if not _verify_manifest(resolved_destination_root, manifest_payload).is_valid:
        raise StateError("Backup captured files do not satisfy declared capture obligations.")
    _write_atomic_json(manifest_path, manifest_payload)
    return RepositoryBackupResult(
        destination_root=resolved_destination_root,
        manifest_path=manifest_path,
        file_count=len(file_records),
        absent_unassessed_store_count=sum(
            record["source_state"] == "absent_unassessed"
            for record in declared_scope["fact_reality_stores"]
        ),
    )


def _path_kind(path: Path) -> str | None:
    try:
        for candidate in (*reversed(path.parents), path):
            try:
                metadata = candidate.lstat()
            except FileNotFoundError:
                return None
            if stat.S_ISLNK(metadata.st_mode) or (
                getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            ):
                raise StateError("Backup scope cannot traverse links or reparse points.")
            if candidate != path and not stat.S_ISDIR(metadata.st_mode):
                raise StateError("Backup scope ancestors must be directories.")
        if stat.S_ISDIR(metadata.st_mode):
            return "dir"
        if stat.S_ISREG(metadata.st_mode):
            return "file"
        raise StateError("Backup scope contains an unsupported filesystem entry.")
    except OSError as error:
        raise StateError("Backup scope metadata is inaccessible.") from error


def _capture_scope(source_root: Path) -> dict[str, Any]:
    programs_root = source_root / "programs"
    db_root = source_root / "vertex-db"
    for setting, expected in (("VERTEX_PROGRAMS_ROOT", programs_root), ("VERTEX_DB_PATH", db_root)):
        configured = os.environ.get(setting)
        if configured is not None and (
            not configured.strip() or not configured.isprintable()
            or not Path(configured).is_absolute() or Path(configured) != expected
        ):
            raise StateError(f"Unsupported backup authority mapping: {setting}.")
    kind = _path_kind(programs_root)
    if kind not in (None, "dir"):
        raise StateError("Backup programs mapping must be a directory.")
    programs: list[str] = []
    try:
        for entry in sorted(programs_root.iterdir()) if kind == "dir" else ():
            entry_kind = _path_kind(entry)
            if entry_kind != "dir" or entry.name.startswith("_"):
                continue
            metadata_kind = _path_kind(entry / "program.yaml")
            if metadata_kind is None:
                continue
            if metadata_kind != "file" or not is_bare_identifier(entry.name):
                raise StateError("Backup program metadata must be a regular file with a bare program identifier.")
            programs.append(entry.name)
    except OSError as error:
        raise StateError("Backup program enumeration failed.") from error
    stores = []
    for program_id in programs:
        database = get_program_reality_db_path(program_id, programs_root=programs_root)
        expected = db_root / program_id / "vertex.sqlite3"
        if database != expected:
            raise StateError("Unsupported backup database authority mapping.")
        database_kind = _path_kind(database)
        if database_kind not in (None, "file"):
            raise StateError("Backup fact/reality database must be a regular file.")
        stores.append({
            "program_id": program_id,
            "relative_path": database.relative_to(source_root).as_posix(),
            "source_state": "present" if database_kind == "file" else "absent_unassessed",
        })
    return {"kind": "repository_subset", "program_ids": programs, "fact_reality_stores": stores}


def _validate_manifest(manifest: dict[str, Any]) -> tuple[str, int | None]:
    version = manifest.get("schema_version", "1.0")
    if version not in ("1.0", "1.1"):
        raise StateError("Unsupported backup manifest schema_version.")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise StateError("Backup manifest is missing a valid 'files' list.")
    paths: list[str] = []
    for entry in files:
        if not isinstance(entry, dict):
            raise StateError("Backup manifest contains an invalid file entry.")
        relative = entry.get("relative_path")
        checksum = entry.get("sha256")
        if not isinstance(relative, str) or not relative:
            raise StateError("Backup manifest contains an invalid relative_path value.")
        if not isinstance(checksum, str) or not checksum.startswith("sha256:"):
            raise StateError("Backup manifest contains an invalid checksum.")
        if version == "1.1" and (
            PureWindowsPath(relative).drive or "\\" in relative or relative.startswith("/")
            or any(part in ("", ".", "..") for part in relative.split("/"))
        ):
            raise StateError("Backup manifest paths must be contained relative paths.")
        paths.append(relative)
    if version == "1.0":
        return "legacy_unspecified", None
    if len(set(path.casefold() for path in paths)) != len(paths):
        raise StateError("Backup manifest contains duplicate file paths.")
    scope = manifest.get("declared_scope")
    if not isinstance(scope, dict) or scope.get("kind") != "repository_subset":
        raise StateError("Backup manifest requires declared_scope kind repository_subset.")
    programs = scope.get("program_ids")
    stores = scope.get("fact_reality_stores")
    if not isinstance(programs, list) or not all(
        isinstance(program, str) and is_bare_identifier(program) and not program.startswith("_")
        for program in programs
    ):
        raise StateError("Backup declared_scope requires valid program_ids.")
    if len(set(program.casefold() for program in programs)) != len(programs):
        raise StateError("Backup declared_scope contains duplicate programs.")
    captured_programs = {
        parts[1] for path in paths
        if len(parts := PurePosixPath(path).parts) == 3 and parts[0] == "programs"
        and parts[2] == "program.yaml" and not parts[1].startswith("_")
    }
    if captured_programs != set(programs) or not isinstance(stores, list) or len(stores) != len(programs):
        raise StateError("Backup declared_scope program inventory does not match captured metadata.")
    seen: set[str] = set()
    absent = 0
    for store in stores:
        if not isinstance(store, dict):
            raise StateError("Backup declared_scope contains an invalid store obligation.")
        program = store.get("program_id")
        if not isinstance(program, str) or program not in programs or program in seen:
            raise StateError("Backup declared_scope store program is missing, duplicate or unknown.")
        seen.add(program)
        expected = f"vertex-db/{program}/vertex.sqlite3"
        state = store.get("source_state")
        if store.get("relative_path") != expected or state not in ("present", "absent_unassessed"):
            raise StateError("Backup declared_scope contains an invalid canonical store obligation.")
        if (state == "present") != (expected in paths):
            raise StateError("Backup declared_scope store obligation does not match captured files.")
        absent += state == "absent_unassessed"
    return "repository_subset", absent


def verify_repository_backup(destination_root: Path) -> BackupVerificationResult:
    resolved_destination_root = destination_root.resolve()
    manifest = _load_manifest(resolved_destination_root / BACKUP_MANIFEST_NAME)
    return _verify_manifest(resolved_destination_root, manifest)


def _verify_manifest(resolved_destination_root: Path, manifest: dict[str, Any]) -> BackupVerificationResult:
    capture_scope, absent = _validate_manifest(manifest)
    missing_paths: list[str] = []
    mismatched_paths: list[str] = []
    files = manifest["files"]
    for entry in files:
        relative_path = entry["relative_path"]
        expected_sha = entry["sha256"]
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
        capture_scope=capture_scope,
        declared_scope_check=(
            "unassessed" if capture_scope == "legacy_unspecified" else
            "failed" if missing_paths or mismatched_paths else "passed"
        ),
        absent_unassessed_store_count=absent,
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
    manifest = _load_manifest(resolved_backup_root / BACKUP_MANIFEST_NAME)
    capture_scope, absent = _validate_manifest(manifest)
    files = manifest["files"]

    if not skip_preflight:
        verify_result = _verify_manifest(resolved_backup_root, manifest)
        if not verify_result.is_valid:
            details = []
            if verify_result.missing_paths:
                details.append(f"missing={list(verify_result.missing_paths[:3])}")
            if verify_result.mismatched_paths:
                details.append(f"mismatched={list(verify_result.mismatched_paths[:3])}")
            raise StateError(
                f"Backup pre-flight failed; restore refused: {'; '.join(details)}"
            )

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
        relative_path = entry["relative_path"]
        source_file = resolved_backup_root / Path(relative_path)
        if not source_file.exists():
            raise StateError(
                f"Backup manifest references missing file: {relative_path}"
            )
        destination_file = resolved_destination_root / Path(relative_path)
        try:
            destination_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, destination_file)
        except OSError as error:
            raise StateError(f"Restore file copy failed: {relative_path}") from error
        if not skip_preflight:
            try:
                restored_sha = _hash_file(destination_file)
            except OSError as error:
                raise StateError(f"Restore file integrity read failed: {relative_path}") from error
            if restored_sha != entry["sha256"]:
                raise StateError(f"Restore file checksum mismatch: {relative_path}")
        restored_count += 1

    return RepositoryRestoreResult(
        source_backup_root=resolved_backup_root,
        destination_root=resolved_destination_root,
        file_count=restored_count,
        preflight_verified=not skip_preflight,
        manifest_path=resolved_backup_root / BACKUP_MANIFEST_NAME,
        capture_scope=capture_scope,
        declared_scope_check="not_checked" if skip_preflight else verify_result.declared_scope_check,
        absent_unassessed_store_count=absent,
    )


def _prepare_destination_root(destination_root: Path) -> None:
    if destination_root.exists() and not destination_root.is_dir():
        raise StateError(f"Backup destination must be a directory: {destination_root}")
    if destination_root.exists() and any(destination_root.iterdir()):
        raise StateError(f"Backup destination must be empty: {destination_root}")
    destination_root.mkdir(parents=True, exist_ok=True)


def _iter_files(root: Path) -> tuple[Path, ...]:
    files: list[Path] = []

    def visit(directory: Path) -> None:
        try:
            entries = sorted(directory.iterdir())
        except OSError as error:
            raise StateError(f"Backup scope enumeration failed: {directory}") from error
        for path in entries:
            kind = _path_kind(path)
            if kind == "dir":
                visit(path)
            elif kind == "file":
                files.append(path)
            else:
                raise StateError(f"Backup scope entry disappeared during enumeration: {path}")

    visit(root)
    return tuple(files)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _unique_manifest_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StateError("Backup manifest contains duplicate JSON properties.")
        result[key] = value
    return result


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise StateError(f"Backup manifest was not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_manifest_object)
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