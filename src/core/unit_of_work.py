"""UnitOfWork: single-transaction commit of correlated writes across
multiple SQLite-backed stores (arch-fix.md Phase 1, CPK).

SQLite supports transactional ATTACH: multiple database files can be
attached to one connection and a single BEGIN/COMMIT spans all of them
atomically — a rollback (explicit or on exception) discards uncommitted
changes in every attached database together, not just the primary one
(verified empirically against this repo's SQLite version; see
``tests/unit/test_unit_of_work.py``). This gives CPK consumers (AF-3's
run-lifecycle + release record, AF-7's approval event + outbox row) a way
to commit correlated writes across today's per-concern separate SQLite
files without merging them into one physical file.

Each attached database gets its own journal-mode selection (network path
-> DELETE, local -> WAL), matching ``open_program_db()``'s existing
per-store behavior — `PRAGMA <alias>.journal_mode` is per-database, so a
UnitOfWork spanning one local and one network-drive store does not force
the more conservative mode onto the local one.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping

from src.core.fs_utils import _is_network_filesystem_path


class UnitOfWorkError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class UnitOfWork:
    """``connection`` is a single sqlite3 connection with every requested
    database ATTACHed under its alias. Write to attached databases using
    ``<alias>.<table>`` in SQL (the primary database's tables are
    unqualified, as usual). ``aliases`` lists every alias in attach order
    (the first is the primary/main database)."""

    connection: sqlite3.Connection
    aliases: tuple[str, ...]


@contextmanager
def open_unit_of_work(databases: Mapping[str, Path]) -> Iterator[UnitOfWork]:
    """Open one connection spanning every path in ``databases`` (keyed by
    alias) as a single atomic transaction. Commits all-or-nothing on clean
    exit; rolls back all-or-nothing on any exception.

    ``databases`` must have at least one entry. The first entry (insertion
    order) becomes the connection's main/primary database; every other
    entry is ATTACHed under its alias. Aliases must be valid SQL
    identifiers (they are interpolated into ``ATTACH DATABASE ... AS
    <alias>`` and ``PRAGMA <alias>.journal_mode`` — not parameterizable in
    SQLite — so this is validated up front rather than passed through).
    """
    if not databases:
        raise UnitOfWorkError("open_unit_of_work requires at least one database.")
    for alias in databases:
        if not alias.isidentifier():
            raise UnitOfWorkError(f"alias {alias!r} must be a valid Python/SQL identifier.")

    items = list(databases.items())
    primary_alias, primary_path = items[0]
    primary_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(str(primary_path))
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        _apply_journal_mode(connection, alias=None, path=primary_path)

        for alias, path in items[1:]:
            path.parent.mkdir(parents=True, exist_ok=True)
            connection.execute(f"ATTACH DATABASE ? AS {alias}", (str(path),))
            _apply_journal_mode(connection, alias=alias, path=path)

        connection.execute("BEGIN IMMEDIATE")
        try:
            yield UnitOfWork(connection=connection, aliases=tuple(databases.keys()))
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
    finally:
        connection.close()


def _apply_journal_mode(connection: sqlite3.Connection, *, alias: str | None, path: Path) -> None:
    journal_mode = "DELETE" if _is_network_filesystem_path(path) else "WAL"
    prefix = f"{alias}." if alias else ""
    connection.execute(f"PRAGMA {prefix}journal_mode = {journal_mode}")
    connection.execute(f"PRAGMA {prefix}synchronous = FULL")
