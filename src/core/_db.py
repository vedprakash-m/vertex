from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import random
import sqlite3
import time
from typing import Literal

from src.core.fs_utils import _is_network_filesystem_path


@dataclass(slots=True)
class SQLiteUnitOfWork:
    connection: sqlite3.Connection
    read_only: bool = False

    def __enter__(self) -> sqlite3.Connection:
        return self.connection

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is not None:
                self.connection.rollback()
            elif not self.read_only:
                self.connection.commit()
        finally:
            self.connection.close()


def open_program_db(
    path: Path,
    *,
    read_only: bool = False,
    durability: Literal["balanced", "strict"] = "balanced",
    busy_timeout_ms: int = 5000,
) -> SQLiteUnitOfWork:
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    connection_target = f"file:{path.as_posix()}?mode=ro" if read_only else str(path)
    connection = sqlite3.connect(connection_target, uri=read_only)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    if not read_only:
        is_network_path = _is_network_filesystem_path(path)
        journal_mode = "DELETE" if is_network_path else "WAL"
        synchronous = "FULL" if is_network_path or durability == "strict" else "NORMAL"
        connection.execute(f"PRAGMA journal_mode = {journal_mode}")
        connection.execute(f"PRAGMA synchronous = {synchronous}")
    return SQLiteUnitOfWork(connection=connection, read_only=read_only)


def open_program_db_with_retry(
    path: Path,
    *,
    read_only: bool = False,
    durability: Literal["balanced", "strict"] = "balanced",
    busy_timeout_ms: int = 5000,
    max_attempts: int = 8,
    base_delay_s: float = 0.02,
) -> SQLiteUnitOfWork:
    """Like ``open_program_db()``, but retries the connect+PRAGMA setup on
    ``sqlite3.OperationalError: database is locked``/``database is busy``.

    ``PRAGMA busy_timeout`` (set inside ``open_program_db``) only covers lock
    contention on statements issued *after* it takes effect. Converting a
    brand-new database file to WAL mode (the very next statement) needs a
    brief exclusive lock, and multiple connections racing to open the SAME
    not-yet-existing file for the first time (arch-fix.md's workspace-lease
    "multiple hosts boot simultaneously against a shared, empty lease file"
    scenario — reproduced by ``tests/unit/test_workspace_lease.py``'s
    concurrent-acquisition test) can hit "database is locked" on that one
    statement even with a generous busy_timeout. Bounded, jittered
    exponential backoff; re-raises any non-lock/busy ``OperationalError``
    immediately, and the last error after ``max_attempts`` is exhausted.
    """
    last_error: sqlite3.OperationalError | None = None
    for attempt in range(max_attempts):
        try:
            return open_program_db(
                path,
                read_only=read_only,
                durability=durability,
                busy_timeout_ms=busy_timeout_ms,
            )
        except sqlite3.OperationalError as error:
            message = str(error).lower()
            if "locked" not in message and "busy" not in message:
                raise
            last_error = error
            time.sleep(base_delay_s * (2**attempt) + random.uniform(0, base_delay_s))
    assert last_error is not None
    raise last_error
