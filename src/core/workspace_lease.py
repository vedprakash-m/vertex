"""Coarse-grained pessimistic workspace lease (arch-fix.md Phase 1, CPK).

Resolves the single-writer paradox (H4/Gemini-A): an in-process queue
cannot serialize writers running on *other hosts* against a shared network
workspace. A mutating command acquires a ``workspace.lease`` (owner + TTL +
fencing token) before any write; other hosts refuse mutating commands until
the lease expires or is explicitly released/handed over. This serializes
the *operator session*, avoiding granular SMB/SQLite lock contention
entirely — single-host-per-program affinity is the intended operating
model, and this module is the primitive that enforces it.

This module provides the lease primitive only. Wiring it into the set of
"mutating commands" that must acquire it before writing is subsequent
integration work (Phase 2/3, once there is a concrete list of call sites
touching the CPK-backed stores).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core._db import open_program_db_with_retry as open_program_db

PROGRAMS_ROOT = Path(__file__).resolve().parents[2] / "programs"
_DB_FILENAME = "workspace_lease.sqlite3"
_DEFAULT_TTL_SECONDS = 300

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS workspace_lease (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    owner         TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    acquired_at   TEXT NOT NULL,
    expires_at    TEXT NOT NULL
);
"""


class LeaseHeldByAnotherOwner(Exception):
    """Raised when the lease is currently held by a different, non-expired owner."""

    def __init__(self, holder: str, expires_at: datetime) -> None:
        self.holder = holder
        self.expires_at = expires_at
        super().__init__(f"workspace lease held by {holder!r} until {expires_at.isoformat()}")


class LeaseFencingTokenStale(Exception):
    """Raised when a renew/release call presents a fencing token that is no
    longer current — the caller's lease was superseded (its TTL expired and
    another owner took over) while it still believed it held the lease."""

    def __init__(self, presented: int, current: int) -> None:
        self.presented = presented
        self.current = current
        super().__init__(f"stale fencing token {presented} (current is {current})")


@dataclass(frozen=True, slots=True)
class LeaseHandle:
    program_id: str
    owner: str
    fencing_token: int
    acquired_at: datetime
    expires_at: datetime


def get_workspace_lease_db_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "ledger" / _DB_FILENAME


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _wire(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def acquire_lease(
    program_id: str,
    owner: str,
    *,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    programs_root: Path = PROGRAMS_ROOT,
) -> LeaseHandle:
    """Acquire (or renew, if ``owner`` already holds it) the workspace lease.

    Raises ``LeaseHeldByAnotherOwner`` if a different, non-expired owner
    currently holds the lease. ``BEGIN IMMEDIATE`` takes the write lock
    before the read, so the read-decide-write sequence is atomic against
    concurrent acquisition attempts from other connections/hosts.
    """
    path = get_workspace_lease_db_path(program_id, programs_root=programs_root)
    with open_program_db(path, durability="strict") as connection:
        connection.executescript(_SCHEMA_SQL)
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT owner, fencing_token, acquired_at, expires_at FROM workspace_lease WHERE id = 1"
        ).fetchone()

        now = _now()
        if row is not None:
            current_owner, current_token, _acquired_at, expires_at_raw = row
            expires_at = _parse(expires_at_raw)
            if current_owner != owner and now < expires_at:
                raise LeaseHeldByAnotherOwner(current_owner, expires_at)
            next_token = int(current_token) + 1
        else:
            next_token = 1

        expires_at = now + timedelta(seconds=ttl_seconds)
        connection.execute(
            "INSERT INTO workspace_lease (id, owner, fencing_token, acquired_at, expires_at) "
            "VALUES (1, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET owner = excluded.owner, fencing_token = excluded.fencing_token, "
            "acquired_at = excluded.acquired_at, expires_at = excluded.expires_at",
            (owner, next_token, _wire(now), _wire(expires_at)),
        )
        return LeaseHandle(
            program_id=program_id,
            owner=owner,
            fencing_token=next_token,
            acquired_at=now,
            expires_at=expires_at,
        )


def renew_lease(
    handle: LeaseHandle,
    *,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    programs_root: Path = PROGRAMS_ROOT,
) -> LeaseHandle:
    """Extend an already-held lease. Requires the presented fencing token to
    still be current — a stale worker (TTL expired, superseded by another
    owner) cannot silently keep renewing."""
    path = get_workspace_lease_db_path(handle.program_id, programs_root=programs_root)
    with open_program_db(path, durability="strict") as connection:
        connection.executescript(_SCHEMA_SQL)
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT owner, fencing_token FROM workspace_lease WHERE id = 1"
        ).fetchone()
        if row is None:
            raise LeaseFencingTokenStale(handle.fencing_token, 0)
        current_owner, current_token = row
        if int(current_token) != handle.fencing_token or current_owner != handle.owner:
            raise LeaseFencingTokenStale(handle.fencing_token, int(current_token))

        now = _now()
        expires_at = now + timedelta(seconds=ttl_seconds)
        connection.execute(
            "UPDATE workspace_lease SET acquired_at = ?, expires_at = ? WHERE id = 1",
            (_wire(now), _wire(expires_at)),
        )
        return LeaseHandle(
            program_id=handle.program_id,
            owner=handle.owner,
            fencing_token=handle.fencing_token,
            acquired_at=now,
            expires_at=expires_at,
        )


def release_lease(handle: LeaseHandle, *, programs_root: Path = PROGRAMS_ROOT) -> None:
    """Release a held lease. Idempotent no-op if the lease is already gone
    or has been superseded by a newer fencing token (nothing to release)."""
    path = get_workspace_lease_db_path(handle.program_id, programs_root=programs_root)
    with open_program_db(path, durability="strict") as connection:
        connection.executescript(_SCHEMA_SQL)
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT owner, fencing_token FROM workspace_lease WHERE id = 1"
        ).fetchone()
        if row is None:
            return
        current_owner, current_token = row
        if int(current_token) != handle.fencing_token or current_owner != handle.owner:
            return  # already superseded; nothing to release
        connection.execute("DELETE FROM workspace_lease WHERE id = 1")


def read_lease_state(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> LeaseHandle | None:
    """Read the current lease state without acquiring or modifying it."""
    path = get_workspace_lease_db_path(program_id, programs_root=programs_root)
    if not path.exists():
        return None
    with open_program_db(path, read_only=True) as connection:
        row = connection.execute(
            "SELECT owner, fencing_token, acquired_at, expires_at FROM workspace_lease WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        owner, fencing_token, acquired_at_raw, expires_at_raw = row
        return LeaseHandle(
            program_id=program_id,
            owner=str(owner),
            fencing_token=int(fencing_token),
            acquired_at=_parse(acquired_at_raw),
            expires_at=_parse(expires_at_raw),
        )


def is_lease_expired(handle: LeaseHandle, *, at: datetime | None = None) -> bool:
    return (at or _now()) >= handle.expires_at
