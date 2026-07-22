from __future__ import annotations

import pytest

from src.core import _db
from src.core.knowledge_index import connect_knowledge_index


def test_connect_knowledge_index_uses_strict_local_shared_db_policy(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(_db, "_is_network_filesystem_path", lambda _path: False)

    with connect_knowledge_index(knowledge_root=tmp_path) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]

    assert str(journal_mode).lower() == "wal"
    assert busy_timeout == 5000
    assert synchronous == 2  # SQLite FULL


def test_connect_knowledge_index_uses_network_safe_delete_journal(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(_db, "_is_network_filesystem_path", lambda _path: True)

    with connect_knowledge_index(knowledge_root=tmp_path) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]

    assert str(journal_mode).lower() == "delete"
    assert busy_timeout == 5000
    assert synchronous == 2  # SQLite FULL


def test_connect_knowledge_index_rolls_back_failed_transaction_and_recovers(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="simulate interruption"):
        with connect_knowledge_index(knowledge_root=tmp_path) as connection:
            connection.execute(
                "INSERT INTO index_meta (schema_version, rebuilt_at) VALUES (?, ?)",
                ("interrupted", "2026-07-21T00:00:00+00:00"),
            )
            raise RuntimeError("simulate interruption")

    with connect_knowledge_index(knowledge_root=tmp_path) as connection:
        rows = connection.execute("SELECT schema_version FROM index_meta").fetchall()

    assert rows == []
