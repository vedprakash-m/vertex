from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
from datetime import datetime, timezone
import logging
from pathlib import Path
import sqlite3
from threading import Barrier
from typing import Literal
from unittest.mock import Mock

import pytest

from src.core import _db, channel_registry_store, source_candidate_store, sqlite_stores
from src.core.channel_registry_store import ChannelRegistryStore
from src.core.integration_types import RegistrationStatus
from src.core.models import Confidence
from src.core.models_v2 import Signal, SignalThreadLink
from src.core.source_candidate_store import SourceCandidateStore
from src.core.sqlite_stores import (
    SQLiteSignalStore,
    _connect_program_db,
    get_program_sqlite_store_path,
)


@pytest.mark.parametrize(
    ("read_only", "durability", "network", "failed_statement"),
    [
        (False, "balanced", False, "PRAGMA foreign_keys = ON"),
        (False, "balanced", False, "PRAGMA busy_timeout = 5000"),
        (False, "balanced", False, "PRAGMA journal_mode = WAL"),
        (False, "balanced", False, "PRAGMA synchronous = NORMAL"),
        (False, "strict", False, "PRAGMA synchronous = FULL"),
        (False, "balanced", True, "PRAGMA journal_mode = DELETE"),
        (False, "balanced", True, "PRAGMA synchronous = FULL"),
        (True, "balanced", False, "PRAGMA foreign_keys = ON"),
        (True, "balanced", False, "PRAGMA busy_timeout = 5000"),
    ],
)
def test_program_db_closes_connection_when_configuration_fails(
    monkeypatch,
    tmp_path,
    read_only: bool,
    durability: Literal["balanced", "strict"],
    network: bool,
    failed_statement: str,
) -> None:
    connection = Mock(spec=sqlite3.Connection)
    failure = sqlite3.OperationalError("synthetic configuration failure")

    def execute(statement: str):
        if statement == failed_statement:
            raise failure
        return Mock()

    connection.execute.side_effect = execute
    monkeypatch.setattr(_db.sqlite3, "connect", Mock(return_value=connection))
    monkeypatch.setattr(_db, "_is_network_filesystem_path", lambda _path: network)

    with pytest.raises(sqlite3.OperationalError) as caught:
        _db.open_program_db(tmp_path / "fixture.sqlite3", read_only=read_only, durability=durability)

    assert caught.value is failure
    connection.close.assert_called_once_with()
    connection.commit.assert_not_called()
    connection.rollback.assert_not_called()


def test_program_db_closes_connection_when_filesystem_classification_fails(monkeypatch, tmp_path) -> None:
    connection = Mock(spec=sqlite3.Connection)
    failure = OSError("synthetic filesystem failure")
    monkeypatch.setattr(_db.sqlite3, "connect", Mock(return_value=connection))

    def fail_classification(_path):
        raise failure

    monkeypatch.setattr(_db, "_is_network_filesystem_path", fail_classification)

    with pytest.raises(OSError) as caught:
        _db.open_program_db(tmp_path / "fixture.sqlite3")

    assert caught.value is failure
    connection.close.assert_called_once_with()


def test_program_db_closes_connection_when_setup_is_interrupted(monkeypatch, tmp_path) -> None:
    connection = Mock(spec=sqlite3.Connection)
    connection.execute.side_effect = KeyboardInterrupt()
    monkeypatch.setattr(_db.sqlite3, "connect", Mock(return_value=connection))

    with pytest.raises(KeyboardInterrupt):
        _db.open_program_db(tmp_path / "fixture.sqlite3")

    connection.close.assert_called_once_with()


def test_program_db_surfaces_close_failure_with_setup_error_chained(monkeypatch, tmp_path) -> None:
    connection = Mock(spec=sqlite3.Connection)
    failure = sqlite3.OperationalError("synthetic configuration failure")
    close_failure = sqlite3.ProgrammingError("synthetic close failure")
    connection.execute.side_effect = failure
    connection.close.side_effect = close_failure
    monkeypatch.setattr(_db.sqlite3, "connect", Mock(return_value=connection))

    with pytest.raises(sqlite3.ProgrammingError) as caught:
        _db.open_program_db(tmp_path / "fixture.sqlite3")

    assert caught.value is close_failure
    assert caught.value.__context__ is failure
    connection.close.assert_called_once_with()


def test_program_db_retry_closes_failed_connections_before_next_attempt(monkeypatch, tmp_path) -> None:
    original_connect = sqlite3.connect
    connections = []
    closed_before_attempt = []

    class TrackingConnection(sqlite3.Connection):
        was_closed = False

        def execute(self, statement, *args, **kwargs):
            if statement == "PRAGMA journal_mode = WAL" and len(connections) <= 2:
                raise sqlite3.OperationalError("database is locked")
            return super().execute(statement, *args, **kwargs)

        def close(self):
            super().close()
            self.was_closed = True

    def connect(*args, **kwargs):
        closed_before_attempt.append(tuple(connection.was_closed for connection in connections))
        connection = original_connect(*args, **kwargs, factory=TrackingConnection)
        connections.append(connection)
        return connection

    monkeypatch.setattr(_db.sqlite3, "connect", connect)
    monkeypatch.setattr(_db, "_is_network_filesystem_path", lambda _path: False)
    monkeypatch.setattr(_db.time, "sleep", lambda _delay: None)
    database = tmp_path / "retry.sqlite3"

    try:
        with _db.open_program_db_with_retry(database, max_attempts=3, base_delay_s=0) as connection:
            assert connection is connections[-1]
            assert not connection.was_closed
            connection.execute("CREATE TABLE sample (value INTEGER)")
            connection.execute("INSERT INTO sample VALUES (42)")

        assert closed_before_attempt == [(), (True,), (True, True)]
        assert all(connection.was_closed for connection in connections)
        verification = original_connect(database)
        try:
            assert verification.execute("SELECT value FROM sample").fetchone()[0] == 42
        finally:
            verification.close()
    finally:
        for connection in connections:
            if not connection.was_closed:
                connection.close()


def test_program_db_retry_does_not_retry_other_setup_errors(monkeypatch, tmp_path) -> None:
    connection = Mock(spec=sqlite3.Connection)
    failure = sqlite3.OperationalError("synthetic configuration failure")
    connection.execute.side_effect = failure
    connect = Mock(return_value=connection)
    sleep = Mock()
    monkeypatch.setattr(_db.sqlite3, "connect", connect)
    monkeypatch.setattr(_db.time, "sleep", sleep)

    with pytest.raises(sqlite3.OperationalError) as caught:
        _db.open_program_db_with_retry(tmp_path / "retry.sqlite3", max_attempts=3, base_delay_s=0)

    assert caught.value is failure
    connect.assert_called_once()
    connection.close.assert_called_once_with()
    sleep.assert_not_called()


@pytest.mark.parametrize("message", ["database is locked", "database is busy"])
def test_program_db_retry_closes_every_exhausted_attempt(monkeypatch, tmp_path, message: str) -> None:
    connections = []
    failures = []

    def connect(*_args, **_kwargs):
        connection = Mock(spec=sqlite3.Connection)
        failure = sqlite3.OperationalError(message)
        connection.execute.side_effect = failure
        connections.append(connection)
        failures.append(failure)
        return connection

    sleep = Mock()
    monkeypatch.setattr(_db.sqlite3, "connect", connect)
    monkeypatch.setattr(_db.time, "sleep", sleep)

    with pytest.raises(sqlite3.OperationalError, match=message) as caught:
        _db.open_program_db_with_retry(tmp_path / "retry.sqlite3", max_attempts=3, base_delay_s=0)

    assert len(connections) == 3
    assert caught.value is failures[-1]
    assert sleep.call_count == 3
    for connection in connections:
        connection.close.assert_called_once_with()


def test_program_db_successful_read_only_connection_closes_without_writes(tmp_path) -> None:
    database = tmp_path / "fixture.sqlite3"
    initial = sqlite3.connect(database)
    try:
        initial.execute("CREATE TABLE sample (value INTEGER)")
        initial.execute("INSERT INTO sample VALUES (42)")
        initial.commit()
    finally:
        initial.close()
    before = database.read_bytes()

    with _db.open_program_db(database, read_only=True) as connection:
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == 42
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("INSERT INTO sample VALUES (43)")

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT value FROM sample")
    assert database.read_bytes() == before


@pytest.mark.parametrize("filename", ["fixture #.sqlite3", "fixture%25.sqlite3", "fixture ü.sqlite3"])
@pytest.mark.parametrize("retry", [False, True])
def test_program_db_read_only_uses_literal_relative_path(tmp_path, monkeypatch, filename: str, retry: bool) -> None:
    database = tmp_path / filename
    with closing(sqlite3.connect(database)) as setup:
        setup.execute("CREATE TABLE sample (value INTEGER)")
        setup.execute("INSERT INTO sample VALUES (42)")
        setup.commit()
    before = database.read_bytes()
    monkeypatch.chdir(tmp_path)
    opener = _db.open_program_db_with_retry if retry else _db.open_program_db

    with opener(Path(filename), read_only=True) as connection:
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == 42
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("INSERT INTO sample VALUES (43)")

    assert database.read_bytes() == before


def test_program_db_read_only_does_not_create_missing_encoded_path(tmp_path) -> None:
    database = tmp_path / "missing%20file.sqlite3"
    decoy = tmp_path / "missing file.sqlite3"
    with closing(sqlite3.connect(decoy)) as setup:
        setup.execute("CREATE TABLE sample (value INTEGER)")
        setup.commit()
    with pytest.raises(sqlite3.OperationalError):
        _db.open_program_db(database, read_only=True)
    assert not database.exists()
    assert decoy.exists()


@pytest.mark.parametrize("store_type", [ChannelRegistryStore, SourceCandidateStore])
@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("commit", [42]),
        ("rollback", []),
        ("commit_then_error", [42]),
        ("explicit_rollback", []),
        ("sql_error", []),
    ],
)
def test_registry_connections_close_after_transaction(
    tmp_path, store_type, outcome: str, expected: list[int],
) -> None:
    database = tmp_path / "registry.sqlite3"
    store = store_type(database, "demo")
    with closing(sqlite3.connect(database)) as setup:
        setup.execute("CREATE TABLE connection_test (value INTEGER UNIQUE)")
        setup.commit()

    if outcome in {"rollback", "commit_then_error"}:
        expectation = pytest.raises(RuntimeError, match="synthetic interruption")
    elif outcome == "sql_error":
        expectation = pytest.raises(sqlite3.IntegrityError)
    else:
        expectation = nullcontext()

    connection = None
    try:
        with expectation:
            with store._connect() as connection:
                assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
                connection.execute("INSERT INTO connection_test VALUES (42)")
                if outcome == "sql_error":
                    connection.execute("INSERT INTO connection_test VALUES (42)")
                if outcome == "explicit_rollback":
                    connection.rollback()
                if outcome == "commit_then_error":
                    connection.commit()
                if outcome in {"rollback", "commit_then_error"}:
                    raise RuntimeError("synthetic interruption")

        with closing(sqlite3.connect(database)) as check:
            assert [row[0] for row in check.execute("SELECT value FROM connection_test")] == expected
        assert connection is not None
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    finally:
        if connection is not None:
            connection.close()


@pytest.mark.parametrize("store_type", [ChannelRegistryStore, SourceCandidateStore])
def test_registry_connection_closes_and_rolls_back_after_commit_failure(tmp_path, store_type) -> None:
    database = tmp_path / "registry.sqlite3"
    store = store_type(database, "demo")
    with closing(sqlite3.connect(database)) as setup:
        setup.executescript(
            "CREATE TABLE parent (id INTEGER PRIMARY KEY);"
            "CREATE TABLE child (parent_id INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED);"
        )

    connection = None
    try:
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            with store._connect() as connection:
                connection.execute("INSERT INTO child VALUES (42)")
        with closing(sqlite3.connect(database)) as check:
            assert check.execute("SELECT COUNT(*) FROM child").fetchone()[0] == 0
        assert connection is not None
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    finally:
        if connection is not None:
            connection.close()


def test_registry_helpers_leave_caller_owned_connection_open(tmp_path) -> None:
    database = tmp_path / "registry.sqlite3"
    registry = ChannelRegistryStore(database, "demo")
    candidates = SourceCandidateStore(database, "demo", ensure_schema=False)

    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        assert candidates.get_intent_with_conn(connection, "missing") is None
        registry._set_status("ado", "missing", "work_item", RegistrationStatus.RETIRED, conn=connection)
        assert connection.in_transaction
        assert connection.execute("SELECT 1").fetchone()[0] == 1
        connection.rollback()


@pytest.mark.parametrize("store_type", [ChannelRegistryStore, SourceCandidateStore])
def test_registry_public_reads_close_owned_connections(monkeypatch, tmp_path, store_type) -> None:
    store = store_type(tmp_path / "registry.sqlite3", "demo")
    module = channel_registry_store if store_type is ChannelRegistryStore else source_candidate_store
    connections = []

    def record_open(*args, **kwargs):
        unit = _db.open_program_db(*args, **kwargs)
        connections.append(unit.connection)
        return unit

    monkeypatch.setattr(module, "open_program_db", record_open)
    try:
        if store_type is ChannelRegistryStore:
            assert store.registered_channels() == ()
        else:
            assert store.get_intent("missing") is None
        assert len(connections) == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connections[0].execute("SELECT 1")
    finally:
        for connection in connections:
            connection.close()


def test_registry_schema_failure_closes_connection(monkeypatch, tmp_path) -> None:
    original_connect = sqlite3.connect
    connections = []

    class SchemaFailureConnection(sqlite3.Connection):
        def executescript(self, sql):
            raise sqlite3.OperationalError("synthetic schema failure")

    def connect(*args, **kwargs):
        connection = original_connect(*args, factory=SchemaFailureConnection, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(_db.sqlite3, "connect", connect)
    try:
        with pytest.raises(sqlite3.OperationalError, match="synthetic schema failure"):
            ChannelRegistryStore(tmp_path / "registry.sqlite3", "demo")
        assert len(connections) == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connections[0].execute("SELECT 1")
    finally:
        for connection in connections:
            connection.close()


@pytest.mark.parametrize("with_schema", [False, True])
def test_candidate_schema_probe_preserves_normal_results(tmp_path, caplog, with_schema: bool) -> None:
    store = SourceCandidateStore(tmp_path / "registry.sqlite3", "demo", ensure_schema=with_schema)

    assert store.has_discovery_schema() is with_schema
    assert not [record for record in caplog.records if record.name == source_candidate_store.__name__]


def test_candidate_schema_probe_closes_connection_after_database_error(monkeypatch, tmp_path, caplog) -> None:
    original_connect = sqlite3.connect
    connections = []

    class SchemaProbeFailureConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if "FROM sqlite_master" in sql:
                raise sqlite3.OperationalError("synthetic schema probe failure")
            return super().execute(sql, *args, **kwargs)

    def connect(*args, **kwargs):
        connection = original_connect(*args, factory=SchemaProbeFailureConnection, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(_db.sqlite3, "connect", connect)
    store = SourceCandidateStore(tmp_path / "registry.sqlite3", "demo", ensure_schema=False)
    try:
        assert store.has_discovery_schema() is False
        assert (
            source_candidate_store.__name__,
            logging.WARNING,
            "Source candidate schema check unavailable due to a database error.",
        ) in caplog.record_tuples
        assert "synthetic schema probe failure" not in caplog.text
        assert len(connections) == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connections[0].execute("SELECT 1")
    finally:
        for connection in connections:
            connection.close()


def test_candidate_schema_probe_warns_on_open_failure(monkeypatch, tmp_path, caplog) -> None:
    store = SourceCandidateStore(tmp_path / "registry.sqlite3", "demo", ensure_schema=False)

    def fail_open(*_args, **_kwargs):
        raise sqlite3.OperationalError("synthetic private open failure")

    monkeypatch.setattr(source_candidate_store, "open_program_db", fail_open)
    assert store.has_discovery_schema() is False
    assert (
        source_candidate_store.__name__,
        logging.WARNING,
        "Source candidate schema check unavailable due to a database error.",
    ) in caplog.record_tuples
    assert "synthetic private open failure" not in caplog.text


def test_candidate_schema_probe_warns_on_close_failure(monkeypatch, tmp_path, caplog) -> None:
    store = SourceCandidateStore(tmp_path / "registry.sqlite3", "demo")
    original_connect = sqlite3.connect
    connections = []

    class CloseFailureConnection(sqlite3.Connection):
        def close(self):
            raise sqlite3.OperationalError("synthetic private close failure")

    def connect(*args, **kwargs):
        connection = original_connect(*args, factory=CloseFailureConnection, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(_db.sqlite3, "connect", connect)
    try:
        assert store.has_discovery_schema() is False
        assert (
            source_candidate_store.__name__,
            logging.WARNING,
            "Source candidate schema check unavailable due to a database error.",
        ) in caplog.record_tuples
        assert "synthetic private close failure" not in caplog.text
    finally:
        for connection in connections:
            sqlite3.Connection.close(connection)


@pytest.mark.parametrize("store_type", [ChannelRegistryStore, SourceCandidateStore])
@pytest.mark.parametrize("body_fails", [False, True])
def test_registry_connection_close_failure_is_visible(
    monkeypatch, tmp_path, store_type, body_fails: bool,
) -> None:
    store = store_type(tmp_path / "registry.sqlite3", "demo")
    original_connect = sqlite3.connect
    connections = []
    body_error = RuntimeError("synthetic body failure")

    class CloseFailureConnection(sqlite3.Connection):
        def close(self):
            raise sqlite3.OperationalError("synthetic close failure")

    def connect(*args, **kwargs):
        connection = original_connect(*args, factory=CloseFailureConnection, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(_db.sqlite3, "connect", connect)
    try:
        with pytest.raises(sqlite3.OperationalError, match="synthetic close failure") as caught:
            with store._connect() as connection:
                connection.execute("SELECT 1")
                if body_fails:
                    raise body_error
        assert caught.value.__context__ is (body_error if body_fails else None)
    finally:
        for connection in connections:
            sqlite3.Connection.close(connection)


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
        with closing(sqlite3.connect(database_path)) as connection:
            with connection:
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


def _seed_legacy_signal_database(tmp_path: Path) -> Path:
    program_id = "samplealpha"
    database_path = get_program_sqlite_store_path(program_id, programs_root=tmp_path)
    database_path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE signals (
                program_id TEXT NOT NULL, signal_id TEXT NOT NULL, timestamp TEXT NOT NULL,
                source TEXT NOT NULL, workstream_id TEXT, entity_refs_json TEXT NOT NULL,
                text TEXT NOT NULL, raw_ref TEXT, confidence TEXT NOT NULL, metadata_json TEXT,
                thread_id TEXT, review_policy TEXT, PRIMARY KEY (program_id, signal_id)
            );
            CREATE TABLE signal_threads (
                program_id TEXT NOT NULL, signal_id TEXT NOT NULL, thread_id TEXT NOT NULL,
                linked_at TEXT NOT NULL, linked_by TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO signals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (program_id, "legacy", "2026-01-01T00:00:00+00:00", "manual", "core", "[]", "legacy", None, "high", None, None, None),
        )
    return database_path


def test_sqlite_signal_legacy_schema_reads_without_writes_and_upgrades_on_default_connection(tmp_path: Path) -> None:
    program_id = "samplealpha"
    database_path = _seed_legacy_signal_database(tmp_path)
    before = database_path.read_bytes()
    store = SQLiteSignalStore(programs_root=tmp_path)

    legacy = store.read(program_id, read_only=True)

    assert len(legacy) == 1
    assert legacy[0].gather_run_id is None
    assert legacy[0].workstream_ids == ("core",)
    assert database_path.read_bytes() == before
    with closing(sqlite3.connect(database_path)) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(signals)")}
    assert "gather_run_id" not in columns
    assert "workstream_ids_json" not in columns

    assert store.read(program_id) == legacy
    with closing(sqlite3.connect(database_path)) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(signals)")}
    assert {"gather_run_id", "workstream_ids_json"} <= columns
    assert store.read(program_id, read_only=True) == legacy


@pytest.mark.parametrize(
    ("existing_column", "existing_value", "expected_run", "expected_workstreams"),
    [
        ("gather_run_id", "run-legacy", "run-legacy", ("core",)),
        ("workstream_ids_json", '["shared"]', None, ("shared",)),
    ],
)
def test_sqlite_signal_partially_upgraded_schema_preserves_existing_values(
    tmp_path: Path,
    existing_column: str,
    existing_value: str,
    expected_run: str | None,
    expected_workstreams: tuple[str, ...],
) -> None:
    database_path = _seed_legacy_signal_database(tmp_path)
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute(f"ALTER TABLE signals ADD COLUMN {existing_column} TEXT")
        connection.execute(f"UPDATE signals SET {existing_column} = ?", (existing_value,))
    before = database_path.read_bytes()
    store = SQLiteSignalStore(programs_root=tmp_path)

    signals = store.read("samplealpha", read_only=True)

    assert len(signals) == 1
    assert signals[0].gather_run_id == expected_run
    assert signals[0].workstream_ids == expected_workstreams
    assert store.read("samplealpha", read_only=True, workstream_id=expected_workstreams[0]) == signals
    assert database_path.read_bytes() == before
    with closing(sqlite3.connect(database_path)) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(signals)")}
    assert ({"gather_run_id", "workstream_ids_json"} & columns) == {existing_column}

    assert store.read("samplealpha") == signals
    with closing(sqlite3.connect(database_path)) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(signals)")}
    assert {"gather_run_id", "workstream_ids_json"} <= columns
    assert store.read("samplealpha", read_only=True) == signals


def test_sqlite_signal_concurrent_legacy_schema_upgrade(tmp_path: Path) -> None:
    database_path = _seed_legacy_signal_database(tmp_path)
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
    gate = Barrier(4)

    def read_legacy_signal(_index: int) -> tuple[Signal, ...]:
        gate.wait(timeout=10)
        return SQLiteSignalStore(programs_root=tmp_path).read("samplealpha")

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(read_legacy_signal, range(4)))

    assert all(len(signals) == 1 and signals[0].workstream_ids == ("core",) for signals in results)
    with closing(sqlite3.connect(database_path)) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(signals)")}
    assert {"gather_run_id", "workstream_ids_json"} <= columns


@pytest.mark.parametrize("invalid", ["synthetic-invalid-private", '{"private":"synthetic"}', "null"])
def test_sqlite_signal_rejects_malformed_workstream_ids_without_disclosing_content(
    tmp_path: Path, invalid: str,
) -> None:
    store = SQLiteSignalStore(programs_root=tmp_path)
    signal = Signal(
        id="signal-1",
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source="manual",
        program_id="samplealpha",
        workstream_id="core",
        entity_refs=(),
        text="Synthetic signal",
        raw_ref=None,
        confidence=Confidence.HIGH,
    )
    store.append(signal)
    database_path = get_program_sqlite_store_path(signal.program_id, programs_root=tmp_path)
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute("UPDATE signals SET workstream_ids_json = ?", (invalid,))

    with pytest.raises(ValueError, match="Invalid workstream IDs") as error:
        store.read(signal.program_id, read_only=True)

    assert "synthetic" not in str(error.value)
