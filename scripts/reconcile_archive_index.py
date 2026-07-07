"""WS-1 archive reconciliation script.

When verify_archive_integrity() reports inconsistencies (for example an index
entry that points at a missing snapshot, or canonical archive files that are no
longer indexed), this script is the supported remediation path.

Usage:
    python scripts/reconcile_archive_index.py \
        --program acme --edition acme_weekly \
        --issue 78 \
        --strategy readd \
        --dry-run

Strategies:
    readd : Add or refresh the archive index entry from canonical archive files
            already present on disk. Requires snapshot/html/md/manifest files.
    drop  : Remove the issue entry from the archive index. Use --wipe to also
            delete the canonical archive files for that issue.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from src.core.archive_store import _append_archive_entry, read_archive_index
from src.core.models import ArchiveEntry, ArchiveIndex
from src.core.snapshot_store import ArchiveLock, find_orphaned_staging, get_archive_root, read_snapshot

REPO_ROOT = Path(__file__).resolve().parents[1]


def _resolve_program_root(program: str) -> Path:
    return REPO_ROOT / "programs" / program


def _index_path(program_root: Path, edition: str) -> Path:
    return get_archive_root(edition, program_root / "archive") / "index.json"


def _load_index(program_root: Path, edition: str) -> ArchiveIndex:
    return read_archive_index(edition, archive_root=program_root / "archive")


def _save_index(program_root: Path, edition: str, index: ArchiveIndex) -> Path:
    index_path = _index_path(program_root, edition)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "edition": index.edition,
        "issues": [
            {
                "issue_number": entry.issue_number,
                "generated_at": entry.generated_at.isoformat(),
                "kind": entry.kind,
                "eml_path": entry.eml_path,
                "html_path": entry.html_path,
                "md_path": entry.md_path,
                "snapshot_path": entry.snapshot_path,
                "manifest_path": entry.manifest_path,
                "reason": entry.reason,
                "metadata": entry.metadata,
            }
            for entry in index.issues
        ],
    }
    index_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return index_path


def _find_issue_entry(index: ArchiveIndex, issue_number: int) -> ArchiveEntry | None:
    for entry in index.issues:
        if entry.issue_number == issue_number:
            return entry
    return None


def _canonical_issue_paths(program_root: Path, edition: str, issue_number: int) -> dict[str, Path]:
    archive_root = get_archive_root(edition, program_root / "archive")
    issue_prefix = f"issue_{issue_number:03d}"
    return {
        "snapshot": archive_root / "snapshots" / f"{issue_prefix}.snapshot.json",
        "eml": archive_root / "eml" / f"{issue_prefix}.eml",
        "published_eml": archive_root / "published_eml" / f"{issue_prefix}.published.eml",
        "html": archive_root / "html" / f"{issue_prefix}.html",
        "md": archive_root / "md" / f"{issue_prefix}.md",
        "manifest": archive_root / "manifests" / f"{issue_prefix}.json",
    }


def _build_readd_entry(
    program_root: Path,
    edition: str,
    issue_number: int,
    existing_entry: ArchiveEntry | None,
) -> tuple[ArchiveEntry, list[str], dict[str, str | None]]:
    paths = _canonical_issue_paths(program_root, edition, issue_number)
    required_labels = ("snapshot", "html", "md", "manifest")
    missing = [label for label in required_labels if not paths[label].exists()]
    candidates = {
        label: (str(path) if path.exists() else None)
        for label, path in paths.items()
    }
    if missing:
        raise RuntimeError(
            f"Cannot readd issue {issue_number:03d}; missing required archive file(s): {', '.join(missing)}"
        )

    snapshot = read_snapshot(paths["snapshot"])
    metadata = dict(existing_entry.metadata if existing_entry is not None else {})
    if paths["published_eml"].exists():
        metadata.setdefault("published_eml_path", str(paths["published_eml"]))

    entry = ArchiveEntry(
        issue_number=issue_number,
        generated_at=snapshot.generated_at,
        kind=(existing_entry.kind if existing_entry is not None else "confirmed"),
        eml_path=(str(paths["eml"]) if paths["eml"].exists() else None),
        html_path=str(paths["html"]),
        md_path=str(paths["md"]),
        snapshot_path=str(paths["snapshot"]),
        manifest_path=str(paths["manifest"]),
        reason=(existing_entry.reason if existing_entry is not None else None),
        metadata=metadata,
    )
    return entry, missing, candidates


def _plan_readd(
    program_root: Path,
    edition: str,
    issue_number: int,
    dry_run: bool,
    existing_entry: ArchiveEntry | None,
) -> dict[str, Any]:
    paths = _canonical_issue_paths(program_root, edition, issue_number)
    candidates = {
        label: (str(path) if path.exists() else None)
        for label, path in paths.items()
    }
    missing = [label for label in ("snapshot", "html", "md", "manifest") if not paths[label].exists()]
    return {
        "strategy": "readd",
        "issue_number": issue_number,
        "edition": edition,
        "dry_run": dry_run,
        "issue_present_in_index": existing_entry is not None,
        "candidates": candidates,
        "missing": missing,
        "actions": [
            "Validate that canonical snapshot/html/md/manifest files exist.",
            "Read generated_at from the canonical snapshot and upsert the archive index entry.",
            "Preserve existing metadata when an index entry already exists.",
        ],
    }


def _plan_drop(
    program_root: Path,
    edition: str,
    issue_number: int,
    dry_run: bool,
    existing_entry: ArchiveEntry | None,
    wipe: bool,
) -> dict[str, Any]:
    paths = _canonical_issue_paths(program_root, edition, issue_number)
    referenced_files = {
        label: str(path)
        for label, path in paths.items()
        if path.exists()
    }
    return {
        "strategy": "drop",
        "issue_number": issue_number,
        "edition": edition,
        "dry_run": dry_run,
        "issue_present_in_index": existing_entry is not None,
        "wipe": wipe,
        "files_at_risk": referenced_files,
        "actions": [
            "Remove the issue entry from archive index.json if present.",
            "If --wipe is set, delete canonical archive files for this issue after the index write succeeds.",
        ],
        "human_gate": wipe,
    }


def _drop_entry(index: ArchiveIndex, issue_number: int) -> ArchiveIndex:
    retained = tuple(entry for entry in index.issues if entry.issue_number != issue_number)
    return ArchiveIndex(edition=index.edition, issues=retained)


def _delete_if_exists(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
        return
    path.unlink()


def _apply_readd(program_root: Path, edition: str, issue_number: int) -> Path:
    archive_root = program_root / "archive"
    edition_root = get_archive_root(edition, archive_root)
    with ArchiveLock(edition_root):
        orphaned_staging = find_orphaned_staging(edition, archive_root)
        if orphaned_staging is not None:
            raise RuntimeError(f"Incomplete confirm detected at {orphaned_staging}")
        current_index = _load_index(program_root, edition)
        current_entry = _find_issue_entry(current_index, issue_number)
        updated_entry, _, _ = _build_readd_entry(program_root, edition, issue_number, current_entry)
        updated_index = _append_archive_entry(current_index, updated_entry)
        return _save_index(program_root, edition, updated_index)


def _apply_drop(program_root: Path, edition: str, issue_number: int, *, wipe: bool) -> tuple[Path, list[str]]:
    archive_root = program_root / "archive"
    edition_root = get_archive_root(edition, archive_root)
    removed_files: list[str] = []
    with ArchiveLock(edition_root):
        orphaned_staging = find_orphaned_staging(edition, archive_root)
        if orphaned_staging is not None:
            raise RuntimeError(f"Incomplete confirm detected at {orphaned_staging}")
        current_index = _load_index(program_root, edition)
        updated_index = _drop_entry(current_index, issue_number)
        index_path = _save_index(program_root, edition, updated_index)
        if wipe:
            for path in _canonical_issue_paths(program_root, edition, issue_number).values():
                if path.exists():
                    removed_files.append(str(path))
                    _delete_if_exists(path)
    return index_path, removed_files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[1] if __doc__ else "WS-1 archive reconciliation"
    )
    parser.add_argument("--program", required=True, help="Program id (e.g. acme)")
    parser.add_argument("--edition", required=True, help="Edition id (e.g. acme_weekly)")
    parser.add_argument("--issue", required=True, type=int, help="Issue number (e.g. 78)")
    parser.add_argument(
        "--strategy",
        required=True,
        choices=("readd", "drop"),
        help="Remediation strategy",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Print the plan without mutating (default mode).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the selected reconciliation plan.",
    )
    parser.add_argument(
        "--wipe",
        action="store_true",
        help="When used with --strategy drop --apply, delete canonical archive files after removing the index entry.",
    )
    args = parser.parse_args(argv)

    program_root = _resolve_program_root(args.program)
    if not program_root.exists():
        print(f"ERROR: program root not found: {program_root}", file=sys.stderr)
        return 2

    archive_index = _load_index(program_root, args.edition)
    entry = _find_issue_entry(archive_index, args.issue)
    dry_run = not args.apply

    if args.strategy == "readd":
        plan = _plan_readd(program_root, args.edition, args.issue, dry_run, entry)
    else:
        plan = _plan_drop(program_root, args.edition, args.issue, dry_run, entry, args.wipe)

    print(json.dumps(plan, indent=2))

    if dry_run:
        return 0

    try:
        if args.strategy == "readd":
            index_path = _apply_readd(program_root, args.edition, args.issue)
            result = {
                "applied": True,
                "strategy": "readd",
                "issue_number": args.issue,
                "index_path": str(index_path),
            }
        else:
            index_path, removed_files = _apply_drop(program_root, args.edition, args.issue, wipe=args.wipe)
            result = {
                "applied": True,
                "strategy": "drop",
                "issue_number": args.issue,
                "index_path": str(index_path),
                "removed_files": removed_files,
            }
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
