from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

import pytest

from src.core import _db, sqlite_stores
from src.core.models import Confidence
from src.core.models_v2 import Signal, SignalThreadLink
from src.core.sqlite_stores import (
    SQLiteSignalStore,
    _connect_program_db,
    get_program_sqlite_store_path,
)


def test_connect_program_db_uses_strict_local_shared_db_policy(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(_db, "_is_network_filesystem_path", lambda _path: False)

    with _connect_program_db("samplealpha", programs_root=tmp_path) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]

    assert str(journal_mode).lower() == "wal"
    assert busy_timeout == 5000
    assert synchronous == 2  # SQLite FULL


def test_connect_program_db_uses_network_safe_delete_journal(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(_db, "_is_network_filesystem_path", lambda _path: True)

    with _connect_program_db("samplealpha", programs_root=tmp_path) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]

    assert str(journal_mode).lower() == "delete"
    assert busy_timeout == 5000
    assert synchronous == 2  # SQLite FULL


def test_connect_program_db_rolls_back_failed_transaction_and_recovers(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="simulate interruption"):
        with _connect_program_db("samplealpha", programs_root=tmp_path) as connection:
            connection.execute(
                """
                INSERT INTO signals (
                    program_id, signal_id, timestamp, source, workstream_id,
                    entity_refs_json, text, raw_ref, confidence, metadata_json,
                    thread_id, review_policy
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("samplealpha", "interrupted", "2026-07-21T00:00:00+00:00", "test", None, "[]", "t", None, "high", None, None, None),
            )
            raise RuntimeError("simulate interruption")

    with _connect_program_db("samplealpha", programs_root=tmp_path) as connection:
        rows = connection.execute("SELECT signal_id FROM signals").fetchall()

    assert rows == []


def test_sqlite_signal_read_only_requires_existing_database_without_creating_it(tmp_path) -> None:
    store = SQLiteSignalStore(programs_root=tmp_path)
    database_path = get_program_sqlite_store_path("samplealpha", programs_root=tmp_path)

    with pytest.raises(sqlite3.OperationalError):
        store.read("samplealpha", read_only=True)

    assert not database_path.exists()
    assert not database_path.parent.exists()


@pytest.mark.parametrize(
    ("database_state", "expected_error"),
    [
        ("corrupt", sqlite3.DatabaseError),
        ("uninitialized", sqlite3.OperationalError),
        ("missing_thread_table", sqlite3.OperationalError),
    ],
)
def test_sqlite_signal_read_only_rejects_corrupt_or_uninitialized_database(
    tmp_path,
    database_state: str,
    expected_error: type[sqlite3.Error],
) -> None:
    store = SQLiteSignalStore(programs_root=tmp_path)
    database_path = get_program_sqlite_store_path("samplealpha", programs_root=tmp_path)
    database_path.parent.mkdir(parents=True)
    if database_state == "uninitialized":
        sqlite3.connect(database_path).close()
    elif database_state == "missing_thread_table":
        assert store.read("samplealpha") == ()
        with sqlite3.connect(database_path) as connection:
            connection.execute("DROP TABLE signal_threads")
    else:
        database_path.write_bytes(b"not a sqlite database")
    before = database_path.read_bytes()

    with pytest.raises(expected_error):
        store.read("samplealpha", read_only=True)

    assert database_path.read_bytes() == before


def test_sqlite_signal_read_only_accepts_initialized_empty_database_and_default_read_creates_it(
    tmp_path,
) -> None:
    store = SQLiteSignalStore(programs_root=tmp_path)
    database_path = get_program_sqlite_store_path("samplealpha", programs_root=tmp_path)

    assert store.read("samplealpha") == ()
    before = database_path.read_bytes()

    assert store.read("samplealpha", read_only=True) == ()

    assert database_path.read_bytes() == before


def test_sqlite_signal_read_only_reads_signals_and_thread_links_without_writes(
    monkeypatch,
    tmp_path,
) -> None:
    store = SQLiteSignalStore(programs_root=tmp_path)
    timestamp = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
    signal = Signal(
        id="signal-1",
        timestamp=timestamp,
        source="manual",
        program_id="samplealpha",
        workstream_id=None,
        entity_refs=(),
        text="Review evidence",
        raw_ref=None,
        confidence=Confidence.HIGH,
    )
    store.append(signal)
    store.append_thread(
        "samplealpha",
        SignalThreadLink(
            signal_id=signal.id,
            thread_id="thread-1",
            linked_at=timestamp,
            linked_by="reviewer",
        ),
    )
    database_path = get_program_sqlite_store_path("samplealpha", programs_root=tmp_path)
    before = database_path.read_bytes()
    opened_read_only: list[bool] = []
    original_connect = sqlite_stores._connect_program_db

    def track_connection(program_id: str, *, programs_root, read_only: bool = False):
        opened_read_only.append(read_only)
        return original_connect(program_id, programs_root=programs_root, read_only=read_only)

    monkeypatch.setattr(sqlite_stores, "_connect_program_db", track_connection)

    signals = store.read("samplealpha", read_only=True)

    assert len(signals) == 1
    assert signals[0].id == signal.id
    assert signals[0].thread_id == "thread-1"
    assert opened_read_only == [True, True]
    assert database_path.read_bytes() == before
