from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
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
) -> SQLiteUnitOfWork:
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    connection_target = f"file:{path.as_posix()}?mode=ro" if read_only else str(path)
    connection = sqlite3.connect(connection_target, uri=read_only)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    if not read_only:
        is_network_path = _is_network_filesystem_path(path)
        journal_mode = "DELETE" if is_network_path else "WAL"
        synchronous = "FULL" if is_network_path or durability == "strict" else "NORMAL"
        connection.execute(f"PRAGMA journal_mode = {journal_mode}")
        connection.execute(f"PRAGMA synchronous = {synchronous}")
    return SQLiteUnitOfWork(connection=connection, read_only=read_only)
