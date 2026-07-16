"""ADF-W1.9: unit tests for scripts/reconcile_stray_fact_stores.py.

Covers the operator reconciliation tool's three modes -- dry-run (read-only
plan), execute (archive + rollback manifest), rollback (checksum-verified
restore) -- entirely against fixture paths. This never touches a real
programs/ tree; live reconciliation of the actual XPF stray database found
during this work item is an explicit operator decision, not something this
test (or this script, unattended) performs.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts.reconcile_stray_fact_stores import (
    execute_reconciliation,
    main,
    plan_reconciliation,
    rollback_reconciliation,
)


def _write_fact_store_with_rows(path: Path, *, row_count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE program_fact_revisions (id INTEGER PRIMARY KEY, payload TEXT)")
        connection.executemany(
            "INSERT INTO program_fact_revisions (payload) VALUES (?)", [(f"row-{i}",) for i in range(row_count)]
        )
        connection.commit()
    finally:
        # sqlite3's context manager only commits/rolls back -- it does not
        # close the connection, so on Windows the file handle stays open
        # and a later shutil.move would fail with PermissionError.
        connection.close()


def _setup_ambiguous_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    programs_root = tmp_path / "programs"
    db_root = tmp_path / "vertex-db"

    canonical_path = db_root / "xpf" / "vertex.sqlite3"
    _write_fact_store_with_rows(canonical_path, row_count=5)

    stray_home = fake_home / ".vertex" / "xpf" / "vertex.sqlite3"
    _write_fact_store_with_rows(stray_home, row_count=3)

    stray_programs_root = programs_root / "xpf" / "vertex.sqlite3"
    _write_fact_store_with_rows(stray_programs_root, row_count=0)

    return programs_root, db_root, stray_home


def test_plan_reconciliation_is_read_only_and_reports_row_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    programs_root, db_root, stray_home = _setup_ambiguous_fixture(tmp_path, monkeypatch)

    plan = plan_reconciliation("xpf", programs_root=programs_root, db_root=db_root)
    assert set(plan) == {"home_fallback", "programs_root_relative"}
    assert plan["home_fallback"]["row_count"] == 3
    assert plan["programs_root_relative"]["row_count"] == 0
    # Nothing moved.
    assert stray_home.exists()


def test_execute_reconciliation_archives_and_writes_rollback_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    programs_root, db_root, stray_home = _setup_ambiguous_fixture(tmp_path, monkeypatch)

    manifest_path = execute_reconciliation("xpf", programs_root=programs_root, db_root=db_root)
    assert manifest_path is not None
    assert manifest_path.exists()
    assert not stray_home.exists()  # moved, not deleted-in-place

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["program_id"] == "xpf"
    assert len(manifest["archived"]) == 2
    for entry in manifest["archived"]:
        assert Path(entry["archived_path"]).exists()
        assert not Path(entry["original_path"]).exists()

    # The canonical database was never touched.
    canonical_path = db_root / "xpf" / "vertex.sqlite3"
    assert canonical_path.exists()

    # A second run with nothing left to reconcile is a clean no-op.
    assert execute_reconciliation("xpf", programs_root=programs_root, db_root=db_root) is None


def test_rollback_restores_archived_databases_to_original_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    programs_root, db_root, stray_home = _setup_ambiguous_fixture(tmp_path, monkeypatch)
    manifest_path = execute_reconciliation("xpf", programs_root=programs_root, db_root=db_root)
    assert manifest_path is not None
    assert not stray_home.exists()

    rollback_reconciliation(manifest_path)
    assert stray_home.exists()
    with sqlite3.connect(f"file:{stray_home}?mode=ro", uri=True) as connection:
        count = connection.execute("SELECT COUNT(*) FROM program_fact_revisions").fetchone()[0]
    assert count == 3


def test_rollback_refuses_when_archived_file_checksum_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    programs_root, db_root, stray_home = _setup_ambiguous_fixture(tmp_path, monkeypatch)
    manifest_path = execute_reconciliation("xpf", programs_root=programs_root, db_root=db_root)
    assert manifest_path is not None

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tampered_entry = next(entry for entry in manifest["archived"] if entry["label"] == "home_fallback")
    Path(tampered_entry["archived_path"]).write_bytes(b"tampered content, different checksum")

    with pytest.raises(ValueError, match="checksum mismatch"):
        rollback_reconciliation(manifest_path)


def test_main_dry_run_reports_without_moving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    programs_root, db_root, stray_home = _setup_ambiguous_fixture(tmp_path, monkeypatch)

    exit_code = main(["--program", "xpf", "--programs-root", str(programs_root), "--db-root", str(db_root), "--dry-run"])
    assert exit_code == 0
    assert stray_home.exists()
    output = capsys.readouterr().out
    assert "home_fallback" in output
    assert "--execute" in output


def test_main_execute_then_rollback_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    programs_root, db_root, stray_home = _setup_ambiguous_fixture(tmp_path, monkeypatch)

    exit_code = main(["--program", "xpf", "--programs-root", str(programs_root), "--db-root", str(db_root), "--execute"])
    assert exit_code == 0
    assert not stray_home.exists()
    output = capsys.readouterr().out
    manifest_path = Path(output.split("Rollback manifest: ")[1].strip())

    exit_code = main(["--program", "xpf", "--rollback", str(manifest_path)])
    assert exit_code == 0
    assert stray_home.exists()


def test_main_no_stray_databases_is_a_clean_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    programs_root = tmp_path / "programs"
    db_root = tmp_path / "vertex-db"
    _write_fact_store_with_rows(db_root / "xpf" / "vertex.sqlite3", row_count=1)

    exit_code = main(["--program", "xpf", "--programs-root", str(programs_root), "--db-root", str(db_root)])
    assert exit_code == 0
    assert "No stray" in capsys.readouterr().out
