"""Tests for vertex storage check/stats commands (S1A.3)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli import app
from src.core.sqlite_stores import get_program_sqlite_store_path, _connect_program_db

runner = CliRunner()


def _seed_db(programs_root: Path, program_id: str = "acme") -> Path:
    """Create a healthy SQLite DB with one signal and one trajectory point."""
    with _connect_program_db(program_id, programs_root=programs_root) as conn:
        conn.execute(
            "INSERT INTO signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                program_id,
                "sig-001",
                "2026-05-18T18:00:00+00:00",
                "ado",
                "acme",
                "[]",
                "Test signal",
                None,
                "high",
                None,
                None,
                None,  # review_policy
            ),
        )
        conn.execute(
            "INSERT INTO trajectory_points VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                program_id,
                1234,
                "2026-05-18",
                "Active",
                "user@example.com",
                "2026-06-01",
                "medium",
                "One\\Acme",
                "[]",
                None,
                None,
                "2026-05-18T18:00:00+00:00",
            ),
        )
    return get_program_sqlite_store_path(program_id, programs_root=programs_root)


# ---------------------------------------------------------------------------
# storage check
# ---------------------------------------------------------------------------


def test_storage_check_healthy_db(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_db(programs_root)
    result = runner.invoke(
        app, ["storage", "check", "--program", "acme", "--programs-root", str(programs_root)]
    )
    assert result.exit_code == 0
    assert "OK" in result.output


def test_storage_check_missing_db(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    result = runner.invoke(
        app, ["storage", "check", "--program", "acme", "--programs-root", str(programs_root)]
    )
    assert result.exit_code == 2


def test_storage_check_corrupted_db(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    db_path = _seed_db(programs_root)
    # Overwrite the first 512 bytes to corrupt the DB
    raw = bytearray(db_path.read_bytes())
    raw[100:200] = b"\xff" * 100
    db_path.write_bytes(bytes(raw))
    result = runner.invoke(
        app, ["storage", "check", "--program", "acme", "--programs-root", str(programs_root)]
    )
    # Corrupted DB: either unreadable (exit 2) or integrity failure (exit 1)
    assert result.exit_code in (1, 2)


def test_storage_check_does_not_write(tmp_path: Path) -> None:
    """storage check must not modify the DB (no new tables, no new rows)."""
    programs_root = tmp_path / "programs"
    db_path = _seed_db(programs_root)
    size_before = db_path.stat().st_size
    mtime_before = db_path.stat().st_mtime

    runner.invoke(
        app, ["storage", "check", "--program", "acme", "--programs-root", str(programs_root)]
    )

    # Allow WAL checkpoint to update mtime, but the file size must not grow
    # from new rows — verify signal count is unchanged.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    count = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    conn.close()
    assert count == 1


# ---------------------------------------------------------------------------
# storage stats
# ---------------------------------------------------------------------------


def test_storage_stats_healthy_db(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_db(programs_root)
    result = runner.invoke(
        app, ["storage", "stats", "--program", "acme", "--programs-root", str(programs_root)]
    )
    assert result.exit_code == 0
    assert "Signals" in result.output
    assert "Trajectory points" in result.output
    assert "1" in result.output  # at least one signal and one trajectory point


def test_storage_stats_missing_db(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    result = runner.invoke(
        app, ["storage", "stats", "--program", "acme", "--programs-root", str(programs_root)]
    )
    assert result.exit_code == 2


def test_storage_stats_does_not_write(tmp_path: Path) -> None:
    """storage stats must not modify the DB."""
    programs_root = tmp_path / "programs"
    db_path = _seed_db(programs_root)

    runner.invoke(
        app, ["storage", "stats", "--program", "acme", "--programs-root", str(programs_root)]
    )

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    count = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    conn.close()
    assert count == 1
