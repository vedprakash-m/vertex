"""Migrate nudge artefacts from legacy layout to canonical layout.

Legacy layout:
    programs/<id>/nudge_state.json
    programs/<id>/output/<id>_nudge/
        nudge_audit.jsonl
        title_cache.json
        .run.lock
        preview/<run_id>.preview.eml
        full/<run_id>.full.eml

Canonical layout:
    programs/<id>/nudge/
        nudge_state.json
        nudge_audit.jsonl
        title_cache.json
        .run.lock
        drafts/<run_id>.eml          (EML files from full/ and preview/ merged)
        published_eml/               (empty — user populates with --mark-sent)
            index.json

Usage:
    python scripts/migrate_nudge_layout.py [--dry-run] [--program <id>]

The script is idempotent — re-running after a partial migration is safe.
It refuses to run if a live .run.lock exists at EITHER the old or new location.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
_PROGRAMS_ROOT = REPO_ROOT / "programs"

# EML rename map: old suffix → new suffix (strip dry-run distinction, use .eml)
_EML_SUFFIX_STRIP = (".preview.eml", ".full.eml")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_locked(lock_path: Path) -> bool:
    if not lock_path.exists():
        return False
    try:
        import portalocker  # type: ignore[import-untyped]
        lock = portalocker.Lock(str(lock_path), mode="a+", timeout=0, encoding="utf-8")
        lock.acquire()
        lock.release()
        return False
    except Exception:
        return True


def migrate_program(
    program_id: str,
    *,
    programs_root: Path,
    dry_run: bool,
    verbose: bool = True,
) -> dict[str, object]:
    """Migrate one program. Returns a summary dict."""
    program_dir = programs_root / program_id

    # Paths
    legacy_state = program_dir / "nudge_state.json"
    legacy_output = program_dir / "output" / f"{program_id}_nudge"
    legacy_audit = legacy_output / "nudge_audit.jsonl"
    legacy_title_cache = legacy_output / "title_cache.json"
    legacy_lock = legacy_output / ".run.lock"
    legacy_preview_dir = legacy_output / "preview"
    legacy_full_dir = legacy_output / "full"

    nudge_root = program_dir / "nudge"
    new_state = nudge_root / "nudge_state.json"
    new_audit = nudge_root / "nudge_audit.jsonl"
    new_title_cache = nudge_root / "title_cache.json"
    new_lock = nudge_root / ".run.lock"
    new_drafts = nudge_root / "drafts"
    new_published = nudge_root / "published_eml"
    new_index = new_published / "index.json"

    log: list[str] = []
    errors: list[str] = []

    def _log(msg: str) -> None:
        if verbose:
            print(f"  [{program_id}] {msg}")
        log.append(msg)

    def _err(msg: str) -> None:
        print(f"  [{program_id}] ERROR: {msg}", file=sys.stderr)
        errors.append(msg)

    # Refuse to migrate if a live run lock exists at either location
    for lock_path in (legacy_lock, new_lock):
        if _is_locked(lock_path):
            _err(f"Active run lock detected: {lock_path}. Wait for the active nudge run to complete.")
            return {"program_id": program_id, "status": "skipped", "reason": "active-lock", "log": log, "errors": errors}

    # Check if there is anything to migrate
    has_legacy = legacy_state.exists() or legacy_output.exists()
    if not has_legacy:
        _log("No legacy paths found — nothing to migrate.")
        return {"program_id": program_id, "status": "no-op", "log": log, "errors": errors}

    # Check if already migrated
    already_migrated = nudge_root.exists() and new_state.exists()
    if already_migrated and not legacy_state.exists() and not legacy_output.exists():
        _log("Already migrated — canonical layout present, legacy absent.")
        return {"program_id": program_id, "status": "no-op", "log": log, "errors": errors}

    eml_moved = 0
    files_moved: list[dict[str, str]] = []

    if not dry_run:
        nudge_root.mkdir(parents=True, exist_ok=True)

    # 1. Migrate state file
    if legacy_state.exists() and not new_state.exists():
        src_hash = _sha256(legacy_state)
        _log(f"Moving nudge_state.json → nudge/nudge_state.json (sha256={src_hash[:12]})")
        if not dry_run:
            shutil.copy2(str(legacy_state), str(new_state))
            dest_hash = _sha256(new_state)
            if src_hash != dest_hash:
                _err("Checksum mismatch on nudge_state.json after copy!")
            else:
                legacy_state.unlink()
                files_moved.append({"src": str(legacy_state), "dst": str(new_state)})
    elif new_state.exists():
        _log("nudge_state.json already at new location — skipping.")

    # 2. Migrate audit JSONL
    if legacy_audit.exists() and not new_audit.exists():
        src_hash = _sha256(legacy_audit)
        _log(f"Moving nudge_audit.jsonl (sha256={src_hash[:12]})")
        if not dry_run:
            shutil.copy2(str(legacy_audit), str(new_audit))
            dest_hash = _sha256(new_audit)
            if src_hash != dest_hash:
                _err("Checksum mismatch on nudge_audit.jsonl after copy!")
            else:
                legacy_audit.unlink()
                files_moved.append({"src": str(legacy_audit), "dst": str(new_audit)})
    elif new_audit.exists():
        _log("nudge_audit.jsonl already at new location — skipping.")

    # 3. Migrate title cache
    if legacy_title_cache.exists() and not new_title_cache.exists():
        _log("Moving title_cache.json")
        if not dry_run:
            shutil.copy2(str(legacy_title_cache), str(new_title_cache))
            legacy_title_cache.unlink()
            files_moved.append({"src": str(legacy_title_cache), "dst": str(new_title_cache)})
    elif new_title_cache.exists():
        _log("title_cache.json already at new location — skipping.")

    # 4. Migrate EML files (full/ + preview/ → drafts/)
    if not dry_run:
        new_drafts.mkdir(parents=True, exist_ok=True)

    for src_dir in (legacy_full_dir, legacy_preview_dir):
        if not src_dir.exists():
            continue
        suffix_tag = "full" if src_dir == legacy_full_dir else "preview"
        for eml in sorted(src_dir.glob("*.eml")):
            # Normalize filename: strip .full.eml / .preview.eml → .eml
            new_name = eml.name
            for suffix in _EML_SUFFIX_STRIP:
                if new_name.endswith(suffix):
                    new_name = new_name[: -len(suffix)] + ".eml"
                    break
            dest = new_drafts / new_name
            if dest.exists():
                _log(f"Draft {new_name} already in drafts/ — skipping ({suffix_tag}).")
                continue
            _log(f"Moving EML {eml.name} -> drafts/{new_name} ({suffix_tag})")
            if not dry_run:
                shutil.copy2(str(eml), str(dest))
                eml.unlink()
                eml_moved += 1
                files_moved.append({"src": str(eml), "dst": str(dest)})
            else:
                eml_moved += 1

    # 5. Scaffold published_eml/ with empty index if not present
    if not dry_run and not new_index.exists():
        new_published.mkdir(parents=True, exist_ok=True)
        new_index.write_text(
            json.dumps([], indent=2),
            encoding="utf-8",
        )
        _log("Scaffolded published_eml/index.json (empty — populate with --mark-sent).")

    # 6. Clean up empty legacy directories
    if not dry_run:
        for d in (legacy_preview_dir, legacy_full_dir, legacy_output):
            if d.exists() and not any(d.iterdir()):
                try:
                    d.rmdir()
                    _log(f"Removed empty legacy dir: {d.name}")
                except OSError:
                    pass

    _log(f"Done. EML files migrated: {eml_moved}.")
    return {
        "program_id": program_id,
        "status": "migrated" if not dry_run else "dry-run",
        "eml_moved": eml_moved,
        "files_moved": files_moved,
        "log": log,
        "errors": errors,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Migrate nudge artefacts from legacy output/ layout to canonical nudge/ layout.",
    )
    parser.add_argument("--programs-root", type=Path, default=_PROGRAMS_ROOT)
    parser.add_argument("--program", dest="program_ids", action="append", default=None,
                        help="Migrate only this program (repeatable). Default: all programs.")
    parser.add_argument("--dry-run", action="store_true", help="Print what would happen; make no changes.")
    parser.add_argument("--verbose", action="store_true", default=True)
    parser.add_argument("--quiet", action="store_true", help="Suppress per-file output.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    programs_root: Path = args.programs_root
    verbose = not args.quiet
    dry_run: bool = args.dry_run

    if dry_run:
        print("DRY RUN — no changes will be made.")

    program_ids: list[str] = args.program_ids or []
    if not program_ids:
        if not programs_root.exists():
            print(f"Programs root not found: {programs_root}", file=sys.stderr)
            return 1
        program_ids = [
            d.name for d in sorted(programs_root.iterdir())
            if d.is_dir() and not d.name.startswith("_")
        ]

    all_errors: list[str] = []
    for pid in program_ids:
        print(f"Migrating program: {pid}")
        result = migrate_program(pid, programs_root=programs_root, dry_run=dry_run, verbose=verbose)
        if result.get("errors"):
            all_errors.extend(result["errors"])

    if all_errors:
        print(f"\n{len(all_errors)} error(s) encountered:", file=sys.stderr)
        for e in all_errors:
            print(f"  {e}", file=sys.stderr)
        return 1

    print("\nMigration complete." if not dry_run else "\nDry run complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
