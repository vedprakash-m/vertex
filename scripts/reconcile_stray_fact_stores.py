"""ADF-W1.9 (specs/arch-data-fix.md Section 8.12.3 / QG-37): operator
reconciliation tool for stray program fact-store databases (the PS-14
split-brain hazard -- see ``src/core/quality_gates/state_authority.py``).

Archives (moves, never deletes) any stray ``vertex.sqlite3`` found besides
the canonical path, writing a rollback manifest so the move can be undone.
Read-only by default: without ``--execute`` this only reports what would
happen. This script does not touch database CONTENTS, only file locations
-- it never merges, dedupes, or reconciles row-level data between the
canonical and stray databases (that is a separate, higher-risk decision an
operator must make explicitly after inspecting both).

Usage::

    python scripts/reconcile_stray_fact_stores.py --program xpf --dry-run
    python scripts/reconcile_stray_fact_stores.py --program xpf --execute
    python scripts/reconcile_stray_fact_stores.py --program xpf --rollback <manifest_path>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.migration_log import append_migration_log
from src.core.quality_gates.state_authority import find_stray_fact_store_databases

_ARCHIVE_SUBDIR = ".archive/stray_fact_stores"
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm")


@dataclass(frozen=True, slots=True)
class ArchivedDatabase:
    label: str
    original_path: str
    archived_path: str
    sha256: str
    row_count: int


@dataclass(frozen=True, slots=True)
class RollbackManifest:
    schema_version: str
    program_id: str
    created_at: str
    archived: tuple[ArchivedDatabase, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "program_id": self.program_id,
            "created_at": self.created_at,
            "archived": [asdict(entry) for entry in self.archived],
        }


def _count_rows(db_path: Path) -> int:
    # sqlite3's context manager only commits/rolls back -- it does not close
    # the connection, so the OS file handle would still be open when a
    # subsequent shutil.move runs (fails with PermissionError on Windows).
    # Close explicitly.
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'program_fact_revisions'"
        ).fetchone()
        if row is None:
            return 0
        count_row = connection.execute("SELECT COUNT(*) FROM program_fact_revisions").fetchone()
        return int(count_row[0]) if count_row is not None else 0
    except sqlite3.DatabaseError:
        return 0
    finally:
        connection.close()


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def plan_reconciliation(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    db_root: Path | None = None,
) -> dict[str, dict[str, object]]:
    """Read-only: returns each stray candidate's path and row count. Never
    touches the filesystem beyond reading."""
    stray = find_stray_fact_store_databases(program_id, programs_root=programs_root, db_root=db_root)
    return {label: {"path": str(path), "row_count": _count_rows(path)} for label, path in stray.items()}


def execute_reconciliation(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    db_root: Path | None = None,
    now: datetime | None = None,
) -> Path | None:
    """Archives every stray database found for *program_id* and writes a
    rollback manifest. Returns the manifest path, or ``None`` if there was
    nothing to reconcile. Moves (``shutil.move``) each stray db plus any
    ``-wal``/``-shm`` sidecar to
    ``programs/<id>/.archive/stray_fact_stores/<timestamp>/<label>/``.
    """
    stray = find_stray_fact_store_databases(program_id, programs_root=programs_root, db_root=db_root)
    if not stray:
        return None

    resolved_now = now or datetime.now(timezone.utc)
    timestamp = resolved_now.strftime("%Y%m%dT%H%M%SZ")
    archive_root = programs_root / program_id / _ARCHIVE_SUBDIR / timestamp

    archived: list[ArchivedDatabase] = []
    files_touched: list[str] = []
    for label, original_path in sorted(stray.items()):
        row_count = _count_rows(original_path)
        checksum = _sha256_of(original_path)
        target_dir = archive_root / label
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / original_path.name
        shutil.move(str(original_path), str(target_path))
        files_touched.append(str(original_path))
        files_touched.append(str(target_path))
        for suffix in _SQLITE_SIDECAR_SUFFIXES:
            sidecar = original_path.with_name(original_path.name + suffix)
            if sidecar.exists():
                sidecar_target = target_dir / sidecar.name
                shutil.move(str(sidecar), str(sidecar_target))
                files_touched.append(str(sidecar))
                files_touched.append(str(sidecar_target))
        archived.append(
            ArchivedDatabase(
                label=label,
                original_path=str(original_path),
                archived_path=str(target_path),
                sha256=checksum,
                row_count=row_count,
            )
        )

    manifest = RollbackManifest(
        schema_version="1.0",
        program_id=program_id,
        created_at=resolved_now.isoformat(),
        archived=tuple(archived),
    )
    manifest_path = archive_root / "rollback_manifest.json"
    manifest_path.write_text(json.dumps(manifest.to_dict(), indent=2, sort_keys=True), encoding="utf-8")

    append_migration_log(
        program_id=program_id,
        kind="stray_fact_store_reconciliation",
        source_id=";".join(entry.original_path for entry in archived),
        target_id=str(archive_root),
        files_touched=tuple(files_touched) + (str(manifest_path),),
        dry_run=False,
        operator="scripts/reconcile_stray_fact_stores.py",
        programs_root=programs_root,
    )
    return manifest_path


def rollback_reconciliation(manifest_path: Path) -> None:
    """Moves every archived database back to its original path exactly as
    recorded in the manifest, verifying the sha256 checksum first. Raises
    if any archived file's checksum no longer matches (the file was
    modified after archiving -- do not silently restore it)."""
    manifest_doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest_doc["archived"]:
        archived_path = Path(entry["archived_path"])
        original_path = Path(entry["original_path"])
        if not archived_path.exists():
            raise FileNotFoundError(f"archived file missing, cannot roll back: {archived_path}")
        current_checksum = _sha256_of(archived_path)
        if current_checksum != entry["sha256"]:
            raise ValueError(
                f"checksum mismatch for {archived_path}: expected {entry['sha256']}, got {current_checksum} "
                "-- refusing to restore a file that changed since archiving."
            )
        original_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(archived_path), str(original_path))
        for suffix in _SQLITE_SIDECAR_SUFFIXES:
            sidecar = archived_path.with_name(archived_path.name + suffix)
            if sidecar.exists():
                shutil.move(str(sidecar), str(original_path.with_name(original_path.name + suffix)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--program", required=True, help="Program id, e.g. xpf.")
    parser.add_argument("--programs-root", type=Path, default=PROGRAMS_ROOT)
    parser.add_argument("--db-root", type=Path, default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Report stray databases without moving anything (default).")
    mode.add_argument("--execute", action="store_true", help="Archive stray databases and write a rollback manifest.")
    mode.add_argument("--rollback", type=Path, default=None, metavar="MANIFEST_PATH", help="Undo a prior --execute run.")
    args = parser.parse_args(argv)

    if args.rollback is not None:
        rollback_reconciliation(args.rollback)
        print(f"Rolled back {args.rollback}.")
        return 0

    if args.execute:
        manifest_path = execute_reconciliation(args.program, programs_root=args.programs_root, db_root=args.db_root)
        if manifest_path is None:
            print(f"No stray fact-store databases found for {args.program!r}; nothing to do.")
            return 0
        print(f"Archived stray database(s) for {args.program!r}. Rollback manifest: {manifest_path}")
        return 0

    plan = plan_reconciliation(args.program, programs_root=args.programs_root, db_root=args.db_root)
    if not plan:
        print(f"No stray fact-store databases found for {args.program!r}.")
        return 0
    print(f"{len(plan)} stray database(s) found for {args.program!r} (dry run -- nothing changed):")
    for label, info in sorted(plan.items()):
        print(f"  {label}: {info['path']} ({info['row_count']} rows)")
    print("Re-run with --execute to archive them (never deletes; writes a rollback manifest).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
