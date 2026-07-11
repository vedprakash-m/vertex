from __future__ import annotations

from pathlib import Path

import pytest

from src.core.unit_of_work import UnitOfWorkError, open_unit_of_work


def test_requires_at_least_one_database(tmp_path: Path) -> None:
    with pytest.raises(UnitOfWorkError):
        with open_unit_of_work({}):
            pass


def test_rejects_non_identifier_alias(tmp_path: Path) -> None:
    with pytest.raises(UnitOfWorkError):
        with open_unit_of_work({"not-an-identifier": tmp_path / "a.sqlite3"}):
            pass


def test_single_database_commits(tmp_path: Path) -> None:
    db_path = tmp_path / "a.sqlite3"
    with open_unit_of_work({"main_db": db_path}) as uow:
        uow.connection.execute("CREATE TABLE t (x INTEGER)")
        uow.connection.execute("INSERT INTO t VALUES (1)")

    # Re-open independently to confirm the commit actually persisted.
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    assert conn.execute("SELECT x FROM t").fetchall() == [(1,)]
    conn.close()


def test_cross_database_writes_commit_atomically(tmp_path: Path) -> None:
    path_a = tmp_path / "a.sqlite3"
    path_b = tmp_path / "b.sqlite3"
    with open_unit_of_work({"a": path_a, "b": path_b}) as uow:
        uow.connection.execute("CREATE TABLE t (x INTEGER)")
        uow.connection.execute("CREATE TABLE b.u (y INTEGER)")
        uow.connection.execute("INSERT INTO t VALUES (1)")
        uow.connection.execute("INSERT INTO b.u VALUES (2)")

    import sqlite3

    conn_a = sqlite3.connect(str(path_a))
    conn_b = sqlite3.connect(str(path_b))
    assert conn_a.execute("SELECT x FROM t").fetchall() == [(1,)]
    assert conn_b.execute("SELECT y FROM u").fetchall() == [(2,)]
    conn_a.close()
    conn_b.close()


def test_exception_rolls_back_both_databases(tmp_path: Path) -> None:
    path_a = tmp_path / "a.sqlite3"
    path_b = tmp_path / "b.sqlite3"

    # First, seed schema in a separate, successful unit of work.
    with open_unit_of_work({"a": path_a, "b": path_b}) as uow:
        uow.connection.execute("CREATE TABLE t (x INTEGER)")
        uow.connection.execute("CREATE TABLE b.u (y INTEGER)")

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with open_unit_of_work({"a": path_a, "b": path_b}) as uow:
            uow.connection.execute("INSERT INTO t VALUES (99)")
            uow.connection.execute("INSERT INTO b.u VALUES (99)")
            raise _Boom("simulated failure mid-transaction")

    import sqlite3

    conn_a = sqlite3.connect(str(path_a))
    conn_b = sqlite3.connect(str(path_b))
    assert conn_a.execute("SELECT x FROM t").fetchall() == []
    assert conn_b.execute("SELECT y FROM u").fetchall() == []
    conn_a.close()
    conn_b.close()


def test_aliases_reflects_insertion_order(tmp_path: Path) -> None:
    with open_unit_of_work(
        {"first": tmp_path / "a.sqlite3", "second": tmp_path / "b.sqlite3"}
    ) as uow:
        assert uow.aliases == ("first", "second")
