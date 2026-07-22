from __future__ import annotations

import pytest

from src.core import _db
from src.core.sqlite_stores import _connect_program_db


def test_connect_program_db_uses_strict_local_shared_db_policy(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(_db, "_is_network_filesystem_path", lambda _path: False)

    with _connect_program_db("xpf", programs_root=tmp_path) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]

    assert str(journal_mode).lower() == "wal"
    assert busy_timeout == 5000
    assert synchronous == 2  # SQLite FULL


def test_connect_program_db_uses_network_safe_delete_journal(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(_db, "_is_network_filesystem_path", lambda _path: True)

    with _connect_program_db("xpf", programs_root=tmp_path) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]

    assert str(journal_mode).lower() == "delete"
    assert busy_timeout == 5000
    assert synchronous == 2  # SQLite FULL


def test_connect_program_db_rolls_back_failed_transaction_and_recovers(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="simulate interruption"):
        with _connect_program_db("xpf", programs_root=tmp_path) as connection:
            connection.execute(
                """
                INSERT INTO signals (
                    program_id, signal_id, timestamp, source, workstream_id,
                    entity_refs_json, text, raw_ref, confidence, metadata_json,
                    thread_id, review_policy
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("xpf", "interrupted", "2026-07-21T00:00:00+00:00", "test", None, "[]", "t", None, "high", None, None, None),
            )
            raise RuntimeError("simulate interruption")

    with _connect_program_db("xpf", programs_root=tmp_path) as connection:
        rows = connection.execute("SELECT signal_id FROM signals").fetchall()

    assert rows == []
