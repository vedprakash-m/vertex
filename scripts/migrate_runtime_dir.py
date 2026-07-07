#!/usr/bin/env python3
"""Migrate runtime-tier files from the program root into ``runtime/`` (declutter.md §6 Phase 1-C).

Runtime artifacts (T-3 platform-internal files) currently live at
``programs/<id>/<file>``. After Phase 1-B flips the canonical write getters to
``runtime/<file>``, this script moves the legacy root copies into the canonical
``runtime/`` subdir so an already-onboarded program matches the post-1-B layout a
freshly-onboarded program inherits from the scaffold (Phase 2-A).

The artifact set is sourced from the single Zone-A registry
(``src.core.program_paths.RUNTIME_ARTIFACTS``); this script never re-declares a
filename. ``platform_proof_log.yaml`` is deliberately NOT in the set — it is
classified T-4 and stays at root (not purgeable ``runtime/``).

Usage::

    python scripts/migrate_runtime_dir.py --verify
    python scripts/migrate_runtime_dir.py --all                 # dry-run: show moves
    python scripts/migrate_runtime_dir.py --all --execute       # do the moves
    python scripts/migrate_runtime_dir.py --rollback [--force]
    python scripts/migrate_runtime_dir.py --cleanup-legacy [--program <id>]

Flags:
    --program <id>   Operate on a single program (default with --execute/--rollback/--cleanup-legacy: all).
    --all             Operate on every program under PROGRAMS_ROOT that has legacy runtime files at root.
    --execute         Actually move files (default is dry-run). Implied by --rollback/--cleanup-legacy.
    --dry-run         Show what would happen; no writes (default unless --execute/--rollback/--cleanup-legacy).
    --verify          Report which programs have runtime files at root; no moves (read-only).
    --rollback        Reverse the migration using the root manifest.
    --force           --rollback: bypass ONLY the 30-day age check (never the mtime/hash guard).
    --cleanup-legacy  Delete legacy root runtime files that still match the manifest and whose canonical
                      counterpart exists (after 2 clean DC-02 runs). Runtime-legacy cleanup only.

Safety:
    * The runtime manifest is written to the program **root** at
      ``.runtime_migration_manifest.json`` (NOT inside ``runtime/``) so it
      survives an operator purge of ``runtime/`` (R-11, R-12).
    * SQLite artifacts (``channel_registry.sqlite3``, ``vertex_analytics.sqlite3``)
      are WAL-checkpointed (``PRAGMA wal_checkpoint(TRUNCATE)``) before the move,
      and their ``-wal``/``-shm`` sidecars move together. Moving only the
      ``.sqlite3`` while leaving a meaningful ``-wal`` behind causes data loss on
      the next open (R-2).
    * The script is idempotent: a partial move is safe to re-run.
    * Rollback refuses (without ``--force``) when ``first_migrated_at`` is older
      than 30 days OR any canonical file's current mtime/hash differs from the
      recorded value — a stale rollback would lose live writes (R-13).
    * Run while all ``gather``/``doctor``/analytics processes are idle (R-2′).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.program_paths import (  # noqa: E402
    RUNTIME_ARTIFACTS,
    RUNTIME_ARTIFACTS_BY_NAME,
    RUNTIME_SUBDIR,
    get_runtime_dir,
)

# Manifest schema + identity. Bumped only on a breaking manifest-shape change.
MANIFEST_SCHEMA_VERSION = "1.0"
MANIFEST_FILENAME = ".runtime_migration_manifest.json"
# A-11: vertex_analytics rebuild is NOT lossless today (dri_response_log,
# contradiction_state). It is reclassified T-3b checkpointed, so it IS migrated —
# but never relied on as disposable. Keep it in RUNTIME_FILES unconditionally;
# the registry's ``checkpointed`` flag carries the safety contract.
SQLITE_ARTIFACT_NAMES = frozenset(
    name for name, art in RUNTIME_ARTIFACTS_BY_NAME.items() if art.filename.endswith(".sqlite3")
)
ROLLBACK_WINDOW_DAYS = 30


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
        return h.hexdigest()


def _file_record(path: Path) -> dict[str, object]:
    return {
        "size": path.stat().st_size,
        "mtime": path.stat().st_mtime_ns,
        "sha256": _sha256_file(path),
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_locked_by_sqlite(db_path: Path) -> bool:
    """Best-effort probe for an active SQLite writer.

    Sidecar presence (``-wal``/``-shm``) alone is NOT proof of an active writer —
    SQLite may leave them behind when idle (R-2). We treat a leftover sidecar as
    benign *after* a successful checkpoint; an actual lock surfaces as a failed
    checkpoint or an ``OperationalError("database is locked")`` on connect.
    """
    try:
        conn = sqlite3.connect(str(db_path), timeout=2.0)
    except sqlite3.OperationalError:
        return True
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        conn.commit()
    except sqlite3.OperationalError:
        return True
    finally:
        try:
            conn.close()
        except sqlite3.OperationalError:
            return True
    return False


def _checkpoint_sqlite(db_path: Path) -> tuple[bool, str]:
    """Flush WAL pages into the main file. Returns (ok, reason)."""
    if not db_path.exists():
        return True, "absent"
    try:
        conn = sqlite3.connect(str(db_path), timeout=3.0)
    except sqlite3.OperationalError as exc:
        return False, f"connect failed: {exc}"
    try:
        conn.execute("PRAGMA busy_timeout=3000;")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        conn.commit()
    except sqlite3.OperationalError as exc:
        return False, f"wal_checkpoint failed: {exc}"
    finally:
        try:
            conn.close()
        except sqlite3.OperationalError:
            return False, "close failed (locked on close)"
    return True, "ok"


def _sqlite_sidecars(db_path: Path) -> list[Path]:
    """Return the existing ``-wal``/``-shm`` sidecars for ``db_path``.

    SQLite names sidecars ``<dbfilename>-wal`` / ``<dbfilename>-shm`` (appended
    to the full db filename, not a suffix swap), so construct via ``with_name``.
    """
    candidates = (db_path.with_name(db_path.name + "-wal"), db_path.with_name(db_path.name + "-shm"))
    return [p for p in candidates if p.exists()]


def _move_db_with_sidecars(src_db: Path, dest_db: Path) -> None:
    """Move a SQLite db and its sidecars together (R-2). ``src_db`` must exist."""
    dest_db.parent.mkdir(parents=True, exist_ok=True)
    # Capture sidecar list BEFORE moving the db, so existence is unambiguous.
    sidecars = _sqlite_sidecars(src_db)
    src_db.replace(dest_db)
    for sidecar in sidecars:
        sidecar.replace(dest_db.parent / sidecar.name)


def _list_programs(programs_root: Path) -> list[Path]:
    if not programs_root.exists():
        return []
    return sorted(p for p in programs_root.iterdir() if p.is_dir() and not p.name.startswith("."))


def _legacy_runtime_files(program_dir: Path) -> list[tuple[str, Path]]:
    """Return ``(artifact_name, legacy_path)`` for runtime artifacts still at root."""
    out: list[tuple[str, Path]] = []
    for art in RUNTIME_ARTIFACTS:
        legacy = program_dir / art.filename
        if legacy.exists() and legacy.is_file():
            out.append((art.name, legacy))
    return out


def _manifest_path(program_dir: Path) -> Path:
    return program_dir / MANIFEST_FILENAME


def _read_manifest(program_dir: Path) -> dict | None:
    mp = _manifest_path(program_dir)
    if not mp.exists():
        return None
    try:
        return json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_manifest(program_dir: Path, manifest: dict) -> None:
    _manifest_path(program_dir).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _verify(programs_root: Path, program_id: str | None) -> int:
    print(f"[verify] runtime legacy-at-root report (programs_root={programs_root})")
    dirty = 0
    for program_dir in _list_programs(programs_root):
        if program_id and program_dir.name != program_id:
            continue
        files = _legacy_runtime_files(program_dir)
        runtime_dir = get_runtime_dir(program_dir.name, programs_root=programs_root)
        canonical = [a.filename for a in RUNTIME_ARTIFACTS if (runtime_dir / a.filename).exists()]
        status = "clean" if not files else "legacy-at-root"
        print(f"  {program_dir.name}: {status} ({len(files)} legacy, {len(canonical)} canonical)")
        for name, path in files:
            print(f"      legacy  {path.relative_to(programs_root)}")
        for fn in canonical:
            print(f"      canonical runtime/{fn}")
        if files:
            dirty += 1
    print(f"[verify] {dirty} program(s) with legacy runtime files at root")
    return 0


def _plan(programs_root: Path, program_id: str | None) -> list[Path]:
    targets = []
    for program_dir in _list_programs(programs_root):
        if program_id and program_dir.name != program_id:
            continue
        if _legacy_runtime_files(program_dir):
            targets.append(program_dir)
    return targets


def _migrate_program(program_dir: Path, programs_root: Path, execute: bool) -> list[str]:
    """Move legacy runtime files for one program into runtime/. Returns log lines."""
    log: list[str] = []
    pid = program_dir.name
    files = _legacy_runtime_files(program_dir)
    if not files:
        log.append(f"  {pid}: nothing to move (already canonical or absent)")
        return log

    runtime_dir = get_runtime_dir(pid, programs_root=programs_root)
    # Pre-flight SQLite safety for DB artifacts still at root.
    sqlite_ok = True
    for name, legacy in files:
        art = RUNTIME_ARTIFACTS_BY_NAME[name]
        if name in SQLITE_ARTIFACT_NAMES:
            ok, reason = _checkpoint_sqlite(legacy)
            if not ok:
                log.append(f"  {pid}: ABORT sqlite checkpoint failed for {legacy.name}: {reason}")
                sqlite_ok = False
            if _is_locked_by_sqlite(legacy):
                log.append(f"  {pid}: ABORT {legacy.name} appears locked (active writer?)")
                sqlite_ok = False
    if not sqlite_ok:
        return log

    # Build/extend the root manifest. Preserve first_migrated_at on re-runs.
    existing = _read_manifest(program_dir) or {}
    first_migrated_at = existing.get("first_migrated_at") or _now_iso()
    files_record: dict[str, dict] = existing.get("files", {})

    if not execute:
        log.append(f"  {pid}: (dry-run) would create {runtime_dir.relative_to(programs_root)}/ and move:")
        for name, legacy in files:
            log.append(f"      {legacy.relative_to(programs_root)} -> runtime/{legacy.name}")
        return log

    runtime_dir.mkdir(parents=True, exist_ok=True)

    moved: list[tuple[str, Path, Path]] = []
    for name, legacy in files:
        art = RUNTIME_ARTIFACTS_BY_NAME[name]
        dest = runtime_dir / art.filename
        # Record the LEGACY mtime+hash observed before/while moving (R-8: stamp
        # at move time so a verify/execute race is detected on the next run).
        files_record[name] = {
            "legacy_rel": str(legacy.relative_to(programs_root)),
            "canonical_rel": str(dest.relative_to(programs_root)),
            "legacy_before_move": _file_record(legacy),
        }
        # Move the file plus SQLite sidecars together (R-2).
        if name in SQLITE_ARTIFACT_NAMES:
            _move_db_with_sidecars(legacy, dest)
        else:
            legacy.replace(dest)
        # Stamp canonical mtime+hash immediately after moving.
        files_record[name]["canonical_after_move"] = _file_record(dest)
        moved.append((name, legacy, dest))
        log.append(f"  {pid}: moved {legacy.name} -> runtime/{art.filename}")

    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "migration_id": f"runtime-dir-{first_migrated_at}",
        "program_id": pid,
        "first_migrated_at": first_migrated_at,
        "last_migrated_at": _now_iso(),
        "files": files_record,
    }
    _write_manifest(program_dir, manifest)
    log.append(f"  {pid}: wrote {MANIFEST_FILENAME} ({len(moved)} file(s))")
    return log


def _rollback_program(program_dir: Path, programs_root: Path, force: bool, execute: bool) -> list[str]:
    log: list[str] = []
    pid = program_dir.name
    manifest = _read_manifest(program_dir)
    if not manifest:
        log.append(f"  {pid}: ABORT no manifest at {MANIFEST_FILENAME} (cannot rollback)")
        return log
    files = manifest.get("files", {})
    if not files:
        log.append(f"  {pid}: manifest has no recorded files")
        return log

    first_migrated = manifest.get("first_migrated_at")
    if first_migrated:
        try:
            migrated_dt = datetime.fromisoformat(first_migrated)
        except ValueError:
            log.append(f"  {pid}: ABORT manifest first_migrated_at unparseable: {first_migrated!r}")
            return log
        age_days = (_utcnow() - migrated_dt).days
        if age_days > ROLLBACK_WINDOW_DAYS and not force:
            log.append(
                f"  {pid}: ABORT first_migrated_at is {age_days}d old (> {ROLLBACK_WINDOW_DAYS}d). "
                f"Use --force to bypass ONLY the age check (mtime/hash guard still enforced)."
            )
            return log

    runtime_dir = get_runtime_dir(pid, programs_root=programs_root)
    # Guard: any canonical file changed since migration -> refuse (R-13).
    changed: list[str] = []
    for name, rec in files.items():
        art = RUNTIME_ARTIFACTS_BY_NAME.get(name)
        if not art:
            continue
        canonical = runtime_dir / art.filename
        if not canonical.exists():
            changed.append(f"{name} (canonical missing)")
            continue
        recorded = rec.get("canonical_after_move", {})
        cur = _file_record(canonical)
        if cur["sha256"] != recorded.get("sha256") or cur["mtime"] != recorded.get("mtime"):
            changed.append(f"{name} (canonical changed since migration)")
    if changed:
        log.append(
            f"  {pid}: ABORT canonical file(s) changed since migration — a stale rollback would lose live writes:"
        )
        for c in changed:
            log.append(f"      {c}")
        log.append("  (the mtime/hash guard is never bypassed; --force only bypasses the 30-day age check)")
        return log

    if not execute:
        log.append(f"  {pid}: (dry-run) would move {len(files)} file(s) back to root")
        return log

    moved_back = 0
    for name, rec in files.items():
        art = RUNTIME_ARTIFACTS_BY_NAME.get(name)
        if not art:
            continue
        canonical = runtime_dir / art.filename
        legacy = program_dir / art.filename
        if not canonical.exists():
            continue
        if name in SQLITE_ARTIFACT_NAMES:
            _move_db_with_sidecars(canonical, legacy)
        else:
            canonical.replace(legacy)
        moved_back += 1
        log.append(f"  {pid}: rolled back runtime/{art.filename} -> {art.filename}")
    # Keep the manifest (operator may re-rollback or audit); it now reflects root again.
    log.append(f"  {pid}: rolled back {moved_back} file(s); manifest retained at {MANIFEST_FILENAME}")
    return log


def _cleanup_legacy_program(program_dir: Path, programs_root: Path, execute: bool) -> list[str]:
    log: list[str] = []
    pid = program_dir.name
    manifest = _read_manifest(program_dir)
    if not manifest:
        log.append(f"  {pid}: skip (no manifest — run --all --execute first)")
        return log
    files = manifest.get("files", {})
    runtime_dir = get_runtime_dir(pid, programs_root=programs_root)
    deleted = 0
    skipped = 0
    for name, rec in files.items():
        art = RUNTIME_ARTIFACTS_BY_NAME.get(name)
        if not art:
            continue
        legacy = program_dir / art.filename
        canonical = runtime_dir / art.filename
        # R-15: cleanup validates only the LEGACY file's recorded hash/mtime PLUS
        # canonical existence. It MUST NOT require the canonical file to still
        # match its migration-time hash — active canonical changes are expected
        # and are evidence that writers moved successfully.
        if not legacy.exists():
            skipped += 1
            continue
        if not canonical.exists():
            log.append(f"  {pid}: skip {art.filename} (canonical missing — would orphan data)")
            skipped += 1
            continue
        recorded = rec.get("legacy_before_move", {})
        cur = _file_record(legacy)
        if cur["sha256"] != recorded.get("sha256") or cur["mtime"] != recorded.get("mtime"):
            log.append(f"  {pid}: skip {art.filename} (legacy changed since migration — not stale)")
            skipped += 1
            continue
        if execute:
            legacy.unlink()
            for sidecar in _sqlite_sidecars(legacy):
                sidecar.unlink(missing_ok=True)
            deleted += 1
            log.append(f"  {pid}: deleted stale legacy {art.filename}")
        else:
            deleted += 1
            log.append(f"  {pid}: (dry-run) would delete stale legacy {art.filename}")
    log.append(f"  {pid}: cleanup {deleted} deleted, {skipped} skipped")
    return log


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate runtime-tier files into runtime/ (declutter.md §6 Phase 1-C).")
    parser.add_argument("--program", help="Operate on a single program id.")
    parser.add_argument("--all", action="store_true", help="Operate on every program with legacy runtime files.")
    parser.add_argument("--execute", action="store_true", help="Actually perform writes (default is dry-run).")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen; no writes.")
    parser.add_argument("--verify", action="store_true", help="Report programs with runtime files at root (read-only).")
    parser.add_argument("--rollback", action="store_true", help="Reverse the migration using the root manifest.")
    parser.add_argument("--force", action="store_true", help="--rollback: bypass ONLY the 30-day age check.")
    parser.add_argument("--cleanup-legacy", action="store_true", help="Delete stale legacy runtime files (after 2 clean DC-02 runs).")
    parser.add_argument(
        "--programs-root",
        default=os.environ.get("VERTEX_PROGRAMS_ROOT", str(REPO_ROOT / "programs")),
        help="Programs root (default: $VERTEX_PROGRAMS_ROOT or <repo>/programs).",
    )
    args = parser.parse_args(argv)

    programs_root = Path(args.programs_root)
    if not programs_root.exists():
        print(f"programs_root does not exist: {programs_root}", file=sys.stderr)
        return 2

    if args.verify:
        return _verify(programs_root, args.program)

    if args.rollback:
        execute = args.execute or not args.dry_run
        targets = [programs_root / args.program] if args.program else _list_programs(programs_root)
        rc = 0
        for program_dir in targets:
            if not program_dir.exists():
                continue
            for line in _rollback_program(program_dir, programs_root, args.force, execute):
                print(line)
                if "ABORT" in line:
                    rc = 1
        return rc

    if args.cleanup_legacy:
        execute = args.execute or not args.dry_run
        targets = [programs_root / args.program] if args.program else _list_programs(programs_root)
        for program_dir in targets:
            if not program_dir.exists():
                continue
            for line in _cleanup_legacy_program(program_dir, programs_root, execute):
                print(line)
        return 0

    # Default action: migrate (dry-run unless --execute).
    if not args.all and not args.program:
        parser.error("specify --all or --program <id> (or --verify/--rollback/--cleanup-legacy).")
    execute = args.execute and not args.dry_run
    targets = _plan(programs_root, args.program)
    if not targets:
        print("[migrate] no programs with legacy runtime files at root — nothing to do.")
        return 0
    print(f"[migrate] {'EXECUTE' if execute else 'DRY-RUN'} for {len(targets)} program(s):")
    rc = 0
    for program_dir in targets:
        for line in _migrate_program(program_dir, programs_root, execute):
            print(line)
            if "ABORT" in line:
                rc = 1
    if not execute:
        print("[migrate] dry-run only — re-run with --execute to move files.")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())