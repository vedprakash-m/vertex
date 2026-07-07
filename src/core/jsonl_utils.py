"""Shared JSONL sidecar utilities: quarantine, checksum, rotation, and corruption recovery."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import portalocker


ROTATED_DIRNAME = "rotated"
DEFAULT_MAX_ROTATED_FILES = 5


def parse_jsonl_line(line: str) -> Any:
    """Parse a single JSONL record line.

    This is the sanctioned seam for parsing an individual JSONL line outside of
    the bulk readers below (Phase 7 / D-18): sidecar owners that need bespoke
    per-line filtering, record construction, or error handling keep their own
    loop but route the actual decode through here, so ``json.loads`` on a JSONL
    record lives only in this module. ``json.JSONDecodeError`` propagates
    unchanged so callers' existing try/except and quarantine logic are
    preserved.
    """
    return json.loads(line)


def compute_file_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_checksum_file(path: Path) -> None:
    checksum_path = path.with_suffix(".sha256")
    checksum_path.write_text(compute_file_checksum(path) + "\n", encoding="utf-8")


def quarantine_and_rewrite_jsonl(path: Path, valid_lines: list[str]) -> None:
    quarantine_dir = path.parent / "quarantine"
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    quarantine_path = quarantine_dir / f"{path.stem}.{timestamp}.jsonl"
    suffix = 1
    while quarantine_path.exists():
        suffix += 1
        quarantine_path = quarantine_dir / f"{path.stem}.{timestamp}.{suffix}.jsonl"

    os.replace(path, quarantine_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.writelines(valid_lines)
        handle.flush()
        os.fsync(handle.fileno())
    write_checksum_file(path)


def list_jsonl_quarantine_paths(quarantine_dir: Path, stem: str = "*") -> tuple[Path, ...]:
    if not quarantine_dir.exists():
        return ()
    return tuple(sorted(path for path in quarantine_dir.glob(f"{stem}.*.jsonl") if path.is_file()))


def jsonl_checksum_matches(path: Path, checksum_path: Path) -> bool | None:
    if not path.exists():
        return None
    if not checksum_path.exists():
        return False
    stored = checksum_path.read_text(encoding="utf-8").strip()
    if not stored:
        return False
    return stored == compute_file_checksum(path)


def append_jsonl_line(path: Path, line: str, *, max_bytes: int | None = None) -> bool:
    """Atomic append of a single JSONL line with file locking and checksum.

    When ``max_bytes`` is set and the current file size already exceeds the
    threshold, the existing file is rotated to ``<dir>/rotated/<stem>.<ts>.<n>.jsonl``
    and a fresh empty file is opened for the append.  Returns ``True`` if a
    rotation was performed, ``False`` otherwise.  Rotation is per-file
    (no global coordination) — concurrent writers can race and both decide
    to rotate; the loser of the race appends to a fresh file the winner
    already created, which is acceptable because JSONL append-only files
    are normally written from a single-process pipeline.
    """
    rotated = False
    if max_bytes is not None:
        rotated = rotate_jsonl_if_oversize(path, max_bytes)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        portalocker.lock(handle, portalocker.LOCK_EX)
        try:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            portalocker.unlock(handle)
    write_checksum_file(path)
    return rotated


def append_jsonl_lines(
    path: Path,
    lines: Iterable[str],
    *,
    max_bytes: int | None = None,
) -> bool:
    """Atomic append of multiple JSONL lines under a single portalocker
    lock. The contract (PB-37): every JSONL append in the governance
    surface routes through this helper or ``append_jsonl_line``, OR
    carries a ``# noqa: PB37`` annotation. The test
    ``tests/contracts/test_concurrency_locking_contract.py`` enforces
    that.

    Returns ``True`` if a rotation was performed, ``False`` otherwise.
    """
    rotated = False
    if max_bytes is not None:
        rotated = rotate_jsonl_if_oversize(path, max_bytes)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        portalocker.lock(handle, portalocker.LOCK_EX)
        try:
            for line in lines:
                handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            portalocker.unlock(handle)
    write_checksum_file(path)
    return rotated


def list_rotated_jsonl_paths(
    rotated_dir: Path, stem: str = "*"
) -> tuple[Path, ...]:
    """Return all rotated JSONL files for ``stem`` sorted oldest → newest.

    Used by tests and operators to inspect rotation history.  ``stem`` is a
    glob pattern (default ``*`` matches every stem).
    """
    if not rotated_dir.exists():
        return ()
    return tuple(sorted(path for path in rotated_dir.glob(f"{stem}.*.jsonl") if path.is_file()))


def rotate_jsonl_if_oversize(
    path: Path,
    max_bytes: int,
    *,
    retain: int = DEFAULT_MAX_ROTATED_FILES,
) -> bool:
    """Rotate ``path`` to ``<dir>/rotated/<stem>.<ts>.<n>.jsonl`` if oversize.

    Returns ``True`` if a rotation was performed, ``False`` otherwise.  When
    the number of existing rotated files for the same stem already meets
    ``retain``, the oldest one is removed first so the on-disk footprint is
    bounded.

    The rotation is atomic at the filesystem level (``os.replace``), and the
    rotated file is the *original* with all of its appended lines, so the
    next write starts from an empty file and previous data is preserved for
    forensics / replay.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer.")
    if retain <= 0:
        raise ValueError("retain must be a positive integer.")
    if not path.exists():
        return False
    if path.stat().st_size < max_bytes:
        return False

    rotated_dir = path.parent / ROTATED_DIRNAME
    rotated_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = 1
    rotated_path = rotated_dir / f"{path.stem}.{timestamp}.{suffix}.jsonl"
    while rotated_path.exists():
        suffix += 1
        rotated_path = rotated_dir / f"{path.stem}.{timestamp}.{suffix}.jsonl"
    os.replace(path, rotated_path)
    # Prune oldest rotated files for this stem so the on-disk footprint is
    # bounded by ``retain``.  Sorted oldest → newest; the head is dropped.
    rotated_for_stem = list_rotated_jsonl_paths(rotated_dir, stem=path.stem)
    if len(rotated_for_stem) > retain:
        for stale in rotated_for_stem[: len(rotated_for_stem) - retain]:
            stale.unlink(missing_ok=True)
    return True


def read_jsonl_records(path: Path) -> tuple[dict[str, Any], ...]:
    """Read JSONL records with corruption quarantine."""
    if not path.exists():
        return ()
    entries: list[dict[str, Any]] = []
    valid_lines: list[str] = []
    invalid_found = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            invalid_found = True
            continue
        if not isinstance(payload, dict):
            invalid_found = True
            continue
        entries.append(payload)
        valid_lines.append(raw_line if raw_line.endswith("\n") else raw_line + "\n")
    if invalid_found:
        quarantine_and_rewrite_jsonl(path, valid_lines)
    return tuple(entries)


def validate_jsonl_row(
    row: dict[str, Any],
    required_fields: Iterable[str],
    field_name: str = "row",
) -> None:
    """Strict-validate a decoded JSONL row has the required field set with non-None values.

    Raises ValueError listing the first missing field. Designed to be called from
    sidecar loaders that already trust row shape but want field-presence assertions.
    """
    for field in required_fields:
        if field not in row or row[field] is None:
            raise ValueError(f"{field_name} missing required field: {field!r}")
