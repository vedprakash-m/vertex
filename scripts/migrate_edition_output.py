"""Migrate program edition workspace directories from output/ to publications/.

Usage:
    python scripts/migrate_edition_output.py [--program <id>|--all] [--dry-run]
        [--verify] [--rollback] [--force]

Flags:
    --program <id>   Migrate a single program (default: all programs)
    --all            Migrate all programs with an output/ directory
    --dry-run        Show what would happen; no writes
    --verify         Post-migration SHA-256 manifest check (default: True after rename)
    --rollback       Reverse the rename back to output/ (only safe before post-migration writes)
    --force          Skip post-migration-write check in --rollback; requires acknowledgment

Safety:
    - Checks for active run-lock files before proceeding.
    - Hard-fails if both output/ and publications/ already exist (split-brain).
    - Builds per-file SHA-256 manifest before rename; verifies after.
    - Writes per-program .edition_layout.json marker after each program verifies.
    - Writes programs/.edition_layout.json roll-up after all programs complete.
    - Run during a maintenance window when no Vertex commands are active.
      The quiescence check has a TOCTOU race window — see R-14 in the spec.

NOTE on os.rename() atomicity: os.rename() is atomic on local NTFS volumes.
On SMB/network shares (e.g. Q: drive) it may not be.  Validate with --dry-run
and review A-8 in .archive/specs/move-output-newsletter.md before using on a network share.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PROGRAMS_ROOT = Path(os.environ.get("VERTEX_PROGRAMS_ROOT", str(REPO_ROOT / "programs")))
LAYOUT_MARKER_FILENAME = ".edition_layout.json"
LEGACY_SUBDIR = "output"
CANONICAL_SUBDIR = "publications"
MAX_PATH_SAFE = 254  # conservative; Windows MAX_PATH is 260


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _build_manifest(root: Path) -> dict[str, dict[str, int | str]]:
    """Build a per-file SHA-256 manifest for all files under root."""
    manifest: dict[str, dict[str, int | str]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root))
        manifest[rel] = {"size": path.stat().st_size, "sha256": _sha256_file(path)}
    return manifest


def _verify_manifest(root: Path, manifest: dict[str, dict[str, int | str]]) -> list[str]:
    """Return list of mismatches (empty = OK)."""
    errors: list[str] = []
    for rel, meta in manifest.items():
        dest = root / rel
        if not dest.exists():
            errors.append(f"MISSING: {rel}")
            continue
        actual_size = dest.stat().st_size
        if actual_size != meta["size"]:
            errors.append(f"SIZE MISMATCH: {rel} (expected {meta['size']}, got {actual_size})")
            continue
        actual_sha256 = _sha256_file(dest)
        if actual_sha256 != meta["sha256"]:
            errors.append(f"SHA256 MISMATCH: {rel}")
    # Check for unexpected extra files
    dest_files = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
    for extra in sorted(dest_files - set(manifest.keys())):
        errors.append(f"UNEXPECTED FILE: {extra}")
    return errors


def _check_lock_files(program_dir: Path) -> list[str]:
    """Return paths of active lock files found under program_dir."""
    lock_patterns = [".run.lock", "*.run.lock"]
    locks: list[str] = []
    for pattern in lock_patterns:
        for lock in program_dir.rglob(pattern):
            locks.append(str(lock))
    return locks


def _check_max_path(src: Path, dest_name: str) -> list[str]:
    """Return paths that would exceed MAX_PATH_SAFE after rename."""
    over_limit: list[str] = []
    for path in src.rglob("*"):
        if not path.is_file():
            continue
        new_path = str(path).replace(f"{os.sep}{src.name}{os.sep}", f"{os.sep}{dest_name}{os.sep}")
        if len(new_path) > MAX_PATH_SAFE:
            over_limit.append(f"{len(new_path)} chars: {new_path}")
    return over_limit


def _scan_embedded_paths(root: Path) -> list[str]:
    """Scan for files embedding old 'output/' path strings."""
    hits: list[str] = []
    text_suffixes = {".html", ".eml", ".md", ".json", ".txt", ".yaml", ".yml"}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in text_suffixes:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if f"/{LEGACY_SUBDIR}/" in text or f"\\{LEGACY_SUBDIR}\\" in text:
            hits.append(str(path.relative_to(root)))
    return hits


def _write_program_marker(
    program_dir: Path,
    *,
    program_id: str,
    file_count: int,
    manifest_sha256: str,
    migration_id: str,
    dry_run: bool,
) -> None:
    marker_path = program_dir / LAYOUT_MARKER_FILENAME
    marker = {
        "schema_version": "1",
        "edition_workspace_layout": CANONICAL_SUBDIR,
        "migrated_at": datetime.now(timezone.utc).isoformat(),
        "migrated_by": "scripts/migrate_edition_output.py",
        "migration_id": migration_id,
        "source_file_count": file_count,
        "source_sha256_manifest": f"sha256:{manifest_sha256[:16]}...",
        "state": "complete",
    }
    if dry_run:
        print(f"  [dry-run] would write marker: {marker_path}")
    else:
        marker_path.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
        print(f"  Wrote per-program marker: {marker_path}")


def _write_root_marker(programs_root: Path, migrated: list[str], *, dry_run: bool) -> None:
    marker_path = programs_root / LAYOUT_MARKER_FILENAME
    marker = {
        "schema_version": "1",
        "edition_workspace_layout": CANONICAL_SUBDIR,
        "migrated_at": datetime.now(timezone.utc).isoformat(),
        "programs_migrated": sorted(migrated),
    }
    if dry_run:
        print(f"\n[dry-run] would write root roll-up marker: {marker_path}")
    else:
        marker_path.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
        print(f"\nWrote root roll-up marker: {marker_path}")


def _append_audit_entry(program_dir: Path, *, program_id: str, action: str, dry_run: bool) -> None:
    """Append a migration audit record to programs/<id>/journal/autonomy_audit.jsonl."""
    journal_dir = program_dir / "journal"
    audit_path = journal_dir / "autonomy_audit.jsonl"
    if not journal_dir.exists() or dry_run:
        return
    entry = {
        "event_type": "migration_audit",
        "action": action,
        "program_id": program_id,
        "source": "scripts/migrate_edition_output.py",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    with audit_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def migrate_program(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    dry_run: bool = False,
    verify: bool = True,
    force: bool = False,
) -> bool:
    """Migrate one program's output/ -> publications/. Returns True on success."""
    program_dir = programs_root / program_id
    src = program_dir / LEGACY_SUBDIR
    dest = program_dir / CANONICAL_SUBDIR

    print(f"\n{'[dry-run] ' if dry_run else ''}Program: {program_id}")

    # Idempotence: skip if marker says done and dest exists
    marker_path = program_dir / LAYOUT_MARKER_FILENAME
    if marker_path.exists() and dest.exists() and not src.exists():
        print(f"  Already migrated (marker present, {CANONICAL_SUBDIR}/ exists, {LEGACY_SUBDIR}/ absent). Skipping.")
        return True

    # Source must exist
    if not src.exists():
        print(f"  No {LEGACY_SUBDIR}/ directory found. Nothing to migrate.")
        return True

    # Split-brain check
    if dest.exists():
        print(f"  ERROR: Both {LEGACY_SUBDIR}/ and {CANONICAL_SUBDIR}/ exist (split-brain). "
              "Run --verify to diagnose, or remove the incomplete destination manually.")
        return False

    # Lock check
    locks = _check_lock_files(program_dir)
    if locks:
        print(f"  ERROR: Active lock file(s) detected. Vertex may be running. "
              "Complete or cancel active runs before migrating:")
        for lock in locks:
            print(f"    {lock}")
        return False

    # MAX_PATH check
    over_limit = _check_max_path(src, CANONICAL_SUBDIR)
    if over_limit:
        print(f"  ERROR: {len(over_limit)} path(s) would exceed {MAX_PATH_SAFE} chars after rename:")
        for p in over_limit[:5]:
            print(f"    {p}")
        if len(over_limit) > 5:
            print(f"    ... and {len(over_limit) - 5} more")
        return False

    # Build manifest
    print(f"  Building SHA-256 manifest for {src} ...")
    manifest = _build_manifest(src)
    file_count = len(manifest)
    if not manifest:
        print(f"  Source directory {src} is empty — renaming anyway.")

    # Compute manifest fingerprint
    manifest_json = json.dumps(manifest, sort_keys=True)
    manifest_sha256 = hashlib.sha256(manifest_json.encode()).hexdigest()
    print(f"  {file_count} files, manifest SHA-256: {manifest_sha256[:16]}...")

    # Scan for embedded paths
    embedded_hits = _scan_embedded_paths(src)
    if embedded_hits:
        print(f"  INFO: {len(embedded_hits)} artifact(s) embed '{LEGACY_SUBDIR}/' path strings "
              "(regenerate these after migration):")
        for hit in embedded_hits[:5]:
            print(f"    {hit}")
        if len(embedded_hits) > 5:
            print(f"    ... and {len(embedded_hits) - 5} more")

    if dry_run:
        print(f"  [dry-run] would rename: {src} -> {dest}")
        print(f"  [dry-run] would write per-program marker: {marker_path}")
        return True

    # Perform rename
    print(f"  Renaming {src} -> {dest} ...")
    try:
        os.rename(src, dest)
    except OSError as e:
        print(f"  ERROR: rename failed: {e}")
        return False

    # Verify
    if verify:
        print("  Verifying SHA-256 manifest ...")
        errors = _verify_manifest(dest, manifest)
        if errors:
            print(f"  VERIFICATION FAILED ({len(errors)} error(s)):")
            for err in errors[:10]:
                print(f"    {err}")
            print("  Attempting rollback ...")
            try:
                os.rename(dest, src)
                print("  Rollback succeeded. No changes made.")
            except OSError as re_err:
                print(f"  ROLLBACK FAILED: {re_err}. Manual intervention required.")
            return False
        print(f"  Verification passed: {file_count} files match.")

    # Write per-program marker
    migration_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{program_id}"
    _write_program_marker(
        program_dir,
        program_id=program_id,
        file_count=file_count,
        manifest_sha256=manifest_sha256,
        migration_id=migration_id,
        dry_run=dry_run,
    )

    # Audit entry
    _append_audit_entry(program_dir, program_id=program_id, action="migrate_output_to_publications", dry_run=dry_run)

    print(f"  Done: {program_id} migrated successfully.")
    return True


def rollback_program(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    dry_run: bool = False,
    force: bool = False,
) -> bool:
    """Roll back one program's publications/ -> output/. Returns True on success."""
    program_dir = programs_root / program_id
    src = program_dir / CANONICAL_SUBDIR
    dest = program_dir / LEGACY_SUBDIR

    print(f"\n{'[dry-run] ' if dry_run else ''}Rollback: {program_id}")

    if not src.exists():
        print(f"  No {CANONICAL_SUBDIR}/ directory found. Nothing to roll back.")
        return True

    if dest.exists():
        print(f"  ERROR: {LEGACY_SUBDIR}/ already exists. Cannot roll back without conflicts.")
        return False

    # Check for post-migration writes using SHA-256 manifest comparison
    marker_path = program_dir / LAYOUT_MARKER_FILENAME
    if not force and marker_path.exists():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            pre_manifest_sha = marker.get("source_sha256_manifest", "")
            print(f"  Checking for post-migration writes (pre-migration manifest: {pre_manifest_sha}) ...")
            current_manifest = _build_manifest(src)
            current_json = json.dumps(current_manifest, sort_keys=True)
            current_sha = f"sha256:{hashlib.sha256(current_json.encode()).hexdigest()[:16]}..."
            if current_sha != pre_manifest_sha:
                print(f"  WARNING: Post-migration writes detected (manifest changed: {pre_manifest_sha} -> {current_sha}).")
                print("  Use --force to proceed (data written to publications/ since migration will not be visible from output/).")
                return False
        except Exception as e:
            print(f"  WARNING: Could not read migration marker for post-write check: {e}")

    if force:
        # Require explicit acknowledgment for --force rollback
        if not dry_run:
            print("\n  CAUTION: --force rollback will proceed even if post-migration writes exist.")
            print("  Data written to publications/ since migration will NOT be accessible from output/.")
            confirm = input("  Type 'YES I UNDERSTAND' to proceed: ").strip()
            if confirm != "YES I UNDERSTAND":
                print("  Rollback aborted.")
                return False

    if dry_run:
        print(f"  [dry-run] would rename: {src} -> {dest}")
        return True

    print(f"  Renaming {src} -> {dest} ...")
    try:
        os.rename(src, dest)
    except OSError as e:
        print(f"  ERROR: rename failed: {e}")
        return False

    # Remove per-program marker
    if marker_path.exists():
        marker_path.unlink()
        print(f"  Removed per-program marker: {marker_path}")

    _append_audit_entry(program_dir, program_id=program_id, action="rollback_publications_to_output", dry_run=dry_run)
    print(f"  Rollback complete: {program_id}")
    return True


def _find_programs(programs_root: Path) -> list[str]:
    """Find all program directories that have an output/ or publications/ directory."""
    result: list[str] = []
    for entry in sorted(programs_root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_") or entry.name.startswith("."):
            continue
        if (entry / LEGACY_SUBDIR).exists() or (entry / CANONICAL_SUBDIR).exists():
            result.append(entry.name)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate program edition workspace directories from output/ to publications/.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--program", metavar="ID", help="Single program to migrate/rollback")
    parser.add_argument("--all", dest="all_programs", action="store_true", help="Migrate all programs")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen; no writes")
    parser.add_argument("--verify", action="store_true", default=True, help="SHA-256 post-migration verify (default: on)")
    parser.add_argument("--no-verify", dest="verify", action="store_false")
    parser.add_argument("--rollback", action="store_true", help="Reverse the rename (publications/ -> output/)")
    parser.add_argument("--force", action="store_true", help="Skip post-migration-write check in --rollback")
    args = parser.parse_args()

    if not args.program and not args.all_programs:
        args.all_programs = True

    if args.program:
        program_ids = [args.program]
    else:
        program_ids = _find_programs(PROGRAMS_ROOT)
        if not program_ids:
            print(f"No programs with {LEGACY_SUBDIR}/ or {CANONICAL_SUBDIR}/ found under {PROGRAMS_ROOT}.")
            sys.exit(0)
        print(f"Found {len(program_ids)} program(s): {', '.join(program_ids)}")

    successes: list[str] = []
    failures: list[str] = []

    for program_id in program_ids:
        if args.rollback:
            ok = rollback_program(
                program_id,
                programs_root=PROGRAMS_ROOT,
                dry_run=args.dry_run,
                force=args.force,
            )
        else:
            ok = migrate_program(
                program_id,
                programs_root=PROGRAMS_ROOT,
                dry_run=args.dry_run,
                verify=args.verify,
            )
        (successes if ok else failures).append(program_id)

    if failures:
        print(f"\nFailed: {', '.join(failures)}")
        sys.exit(1)

    if not args.rollback and not args.dry_run:
        _write_root_marker(PROGRAMS_ROOT, successes, dry_run=False)
    elif args.dry_run:
        _write_root_marker(PROGRAMS_ROOT, successes, dry_run=True)

    summary = "rolled back" if args.rollback else "migrated"
    print(f"\nAll {len(successes)} program(s) {summary} successfully.")


if __name__ == "__main__":
    main()
