"""Tests for ``scripts/migrate_runtime_dir.py`` (specs/declutter.md §6 Phase 1-C).

The migration script is exercised through its ``main()`` entrypoint against a
temporary ``programs_root``. These tests cover the read-only ``--verify`` path,
the dry-run vs ``--execute`` distinction, the root manifest round-trip
(SHA-256 + mtime + first/last_migrated_at), SQLite WAL sidecar co-movement
(R-2), rollback guards (R-13: age + mtime/hash), legacy cleanup (R-15), and
idempotency. No live program data is touched.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.migrate_runtime_dir as mrd  # noqa: E402
from src.core.program_paths import RUNTIME_ARTIFACTS, get_runtime_dir  # noqa: E402

MANIFEST = mrd.MANIFEST_FILENAME


def _run(argv: list[str], programs_root: Path, capsys) -> tuple[int, str]:
    rc = mrd.main([*argv, "--programs-root", str(programs_root)])
    captured = capsys.readouterr()
    return rc, captured.out + captured.err


def _seed_program(programs_root: Path, pid: str = "demo") -> Path:
    program_dir = programs_root / pid
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text("schema_version: '3.0'\n", encoding="utf-8")
    return program_dir


def _seed_wal_db(db_path: Path) -> None:
    """Create a WAL-mode SQLite db with a checkpointable page in the -wal file."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t(v) VALUES('row')")
    conn.commit()
    # Leave the connection open briefly so a -wal sidecar may exist; closing
    # normally may checkpoint it away. We do NOT explicitly checkpoint here —
    # the script is responsible for checkpointing before moving.
    conn.close()


def test_verify_reports_legacy_at_root_and_is_readonly(tmp_path, capsys) -> None:
    programs_root = tmp_path / "programs"
    program_dir = _seed_program(programs_root)
    (program_dir / "gather_state.json").write_text("{}", encoding="utf-8")

    rc, out = _run(["--verify"], programs_root, capsys)

    assert rc == 0
    assert "legacy-at-root" in out
    assert "gather_state.json" in out
    # verify moved nothing.
    assert (program_dir / "gather_state.json").exists()
    assert not (program_dir / "runtime").exists()


def test_dry_run_shows_moves_without_writing(tmp_path, capsys) -> None:
    programs_root = tmp_path / "programs"
    program_dir = _seed_program(programs_root)
    (program_dir / "gather_state.json").write_text("{}", encoding="utf-8")

    rc, out = _run(["--program", "demo"], programs_root, capsys)

    assert rc == 0
    assert "DRY-RUN" in out
    assert "would create" in out
    # dry-run moved nothing.
    assert (program_dir / "gather_state.json").exists()
    assert not (program_dir / "runtime").exists()
    assert not (program_dir / MANIFEST).exists()


def test_execute_moves_files_and_writes_root_manifest(tmp_path, capsys) -> None:
    programs_root = tmp_path / "programs"
    program_dir = _seed_program(programs_root)
    (program_dir / "gather_state.json").write_text('{"k":1}', encoding="utf-8")
    (program_dir / "run_telemetry.jsonl").write_text('{"x":1}\n', encoding="utf-8")

    rc, out = _run(["--program", "demo", "--execute"], programs_root, capsys)

    assert rc == 0
    assert "EXECUTE" in out
    runtime_dir = get_runtime_dir("demo", programs_root=programs_root)
    assert (runtime_dir / "gather_state.json").exists()
    assert (runtime_dir / "run_telemetry.jsonl").exists()
    assert not (program_dir / "gather_state.json").exists()
    # Manifest at ROOT (survives a runtime/ purge) — R-11.
    manifest_path = program_dir / MANIFEST
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == mrd.MANIFEST_SCHEMA_VERSION
    assert manifest["program_id"] == "demo"
    assert "first_migrated_at" in manifest and "last_migrated_at" in manifest
    assert "gather_state" in manifest["files"]
    rec = manifest["files"]["gather_state"]
    assert rec["legacy_before_move"]["sha256"] == rec["canonical_after_move"]["sha256"]


def test_execute_moves_sqlite_with_sidecars(tmp_path, capsys) -> None:
    programs_root = tmp_path / "programs"
    program_dir = _seed_program(programs_root)
    db_path = program_dir / "channel_registry.sqlite3"
    _seed_wal_db(db_path)

    rc, out = _run(["--program", "demo", "--execute"], programs_root, capsys)

    assert rc == 0, out
    runtime_dir = get_runtime_dir("demo", programs_root=programs_root)
    assert (runtime_dir / "channel_registry.sqlite3").exists()
    # Whatever sidecars existed at root must NOT remain at root after move.
    assert not (program_dir / "channel_registry.sqlite3-wal").exists()
    # The moved DB must still be openable and contain the row (no WAL data loss).
    conn = sqlite3.connect(str(runtime_dir / "channel_registry.sqlite3"))
    rows = conn.execute("SELECT v FROM t").fetchall()
    conn.close()
    assert rows == [("row",)]


def test_migration_is_idempotent(tmp_path, capsys) -> None:
    programs_root = tmp_path / "programs"
    program_dir = _seed_program(programs_root)
    (program_dir / "gather_state.json").write_text("{}", encoding="utf-8")

    _run(["--program", "demo", "--execute"], programs_root, capsys)
    # Second run: nothing left at root; no error, no duplicate manifest entries.
    rc, out = _run(["--program", "demo", "--execute"], programs_root, capsys)
    assert rc == 0
    assert "nothing to do" in out
    manifest = json.loads((program_dir / MANIFEST).read_text(encoding="utf-8"))
    # first_migrated_at preserved across the no-op re-run.
    assert "first_migrated_at" in manifest


def test_rollback_refuses_when_canonical_changed(tmp_path, capsys) -> None:
    programs_root = tmp_path / "programs"
    program_dir = _seed_program(programs_root)
    (program_dir / "gather_state.json").write_text("{}", encoding="utf-8")

    _run(["--program", "demo", "--execute"], programs_root, capsys)
    # Simulate a live canonical write after migration (R-13 guard).
    runtime_dir = get_runtime_dir("demo", programs_root=programs_root)
    (runtime_dir / "gather_state.json").write_text('{"k":"changed"}', encoding="utf-8")

    rc, out = _run(["--rollback", "--program", "demo", "--execute"], programs_root, capsys)
    assert rc == 1
    assert "ABORT" in out
    assert "changed since migration" in out
    # canonical file untouched by the refused rollback.
    assert (runtime_dir / "gather_state.json").read_text(encoding="utf-8") == '{"k":"changed"}'


def test_rollback_reverses_when_canonical_unchanged(tmp_path, capsys) -> None:
    programs_root = tmp_path / "programs"
    program_dir = _seed_program(programs_root)
    (program_dir / "gather_state.json").write_text('{"orig":1}', encoding="utf-8")

    _run(["--program", "demo", "--execute"], programs_root, capsys)
    rc, out = _run(["--rollback", "--program", "demo", "--execute"], programs_root, capsys)
    assert rc == 0
    assert "rolled back" in out
    # File back at root; runtime/ copy gone.
    assert (program_dir / "gather_state.json").exists()
    runtime_dir = get_runtime_dir("demo", programs_root=programs_root)
    assert not (runtime_dir / "gather_state.json").exists()


def test_rollback_refuses_without_manifest(tmp_path, capsys) -> None:
    programs_root = tmp_path / "programs"
    _seed_program(programs_root)

    rc, out = _run(["--rollback", "--program", "demo", "--execute"], programs_root, capsys)
    assert rc == 1
    assert "no manifest" in out


def _write_runtime_manifest(
    program_dir: Path, pid: str, files: dict[str, dict], first_migrated_at: str
) -> None:
    """Hand-write a root runtime manifest (simulating a prior migration run)."""
    manifest = {
        "schema_version": mrd.MANIFEST_SCHEMA_VERSION,
        "migration_id": f"runtime-dir-{first_migrated_at}",
        "program_id": pid,
        "first_migrated_at": first_migrated_at,
        "last_migrated_at": first_migrated_at,
        "files": files,
    }
    (program_dir / MANIFEST).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def test_cleanup_legacy_deletes_only_matching_legacy(tmp_path, capsys) -> None:
    """R-15: cleanup deletes a root leftover whose hash+mtime still match the
    manifest record (a genuine untouched stale duplicate), but skips one that
    changed since migration (a straggler writer — deleting would lose data)."""
    programs_root = tmp_path / "programs"
    program_dir = _seed_program(programs_root)
    runtime_dir = get_runtime_dir("demo", programs_root=programs_root)
    runtime_dir.mkdir(parents=True)

    # Stale duplicate at root: untouched original content + canonical exists.
    stale_path = program_dir / "gather_state.json"
    stale_path.write_text('{"orig":1}', encoding="utf-8")
    (runtime_dir / "gather_state.json").write_text('{"orig":1}', encoding="utf-8")
    stale_record = mrd._file_record(stale_path)

    # Changed-at-root duplicate: same name, different content → hash mismatch.
    changed_path = program_dir / "run_telemetry.jsonl"
    changed_path.write_text('{"mutated":1}\n', encoding="utf-8")
    (runtime_dir / "run_telemetry.jsonl").write_text('{"orig":1}\n', encoding="utf-8")
    # Record the ORIGINAL (pre-change) telemetry state in the manifest.
    changed_orig_record = {"size": 13, "mtime": 0, "sha256": "deadbeef" * 8}

    _write_runtime_manifest(
        program_dir,
        "demo",
        {
            "gather_state": {
                "legacy_rel": str(stale_path.relative_to(programs_root)),
                "canonical_rel": "demo/runtime/gather_state.json",
                "legacy_before_move": stale_record,
                "canonical_after_move": mrd._file_record(runtime_dir / "gather_state.json"),
            },
            "run_telemetry": {
                "legacy_rel": str(changed_path.relative_to(programs_root)),
                "canonical_rel": "demo/runtime/run_telemetry.jsonl",
                "legacy_before_move": changed_orig_record,
                "canonical_after_move": mrd._file_record(runtime_dir / "run_telemetry.jsonl"),
            },
        },
        "2026-01-01T00:00:00+00:00",
    )

    rc, out = _run(["--cleanup-legacy", "--program", "demo", "--execute"], programs_root, capsys)
    assert rc == 0
    assert "deleted stale legacy gather_state.json" in out
    assert not stale_path.exists()  # matching stale duplicate deleted
    assert changed_path.exists()  # changed-at-root file preserved (not stale)


def test_cleanup_legacy_skips_when_canonical_missing(tmp_path, capsys) -> None:
    programs_root = tmp_path / "programs"
    program_dir = _seed_program(programs_root)
    (program_dir / "gather_state.json").write_text("{}", encoding="utf-8")
    _run(["--program", "demo", "--execute"], programs_root, capsys)
    # Re-create the stale legacy duplicate.
    (program_dir / "gather_state.json").write_text("{}", encoding="utf-8")
    # Delete the canonical — cleanup must refuse to orphan data.
    runtime_dir = get_runtime_dir("demo", programs_root=programs_root)
    (runtime_dir / "gather_state.json").unlink()

    rc, out = _run(["--cleanup-legacy", "--program", "demo", "--execute"], programs_root, capsys)
    assert rc == 0
    assert "canonical missing" in out
    assert (program_dir / "gather_state.json").exists()


def test_platform_proof_log_is_not_a_runtime_file(tmp_path) -> None:
    """platform_proof_log.yaml is T-4 (root), NOT in RUNTIME_FILES (declutter.md §6 1-C)."""
    filenames = {a.filename for a in RUNTIME_ARTIFACTS}
    assert "platform_proof_log.yaml" not in filenames
    # All 7 Phase-1-C RUNTIME_FILES are present in the registry.
    expected = {
        "gather_state.json", "run_telemetry.jsonl", "dedup_drop_log.jsonl",
        "m365_registry.yaml", "channel_registry.sqlite3",
        "vertex_analytics.sqlite3", "readiness_snapshot.yaml",
    }
    assert expected <= filenames