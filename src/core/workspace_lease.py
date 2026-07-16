"""Coarse-grained pessimistic workspace lease (arch-fix.md Phase 1, CPK).

Resolves the single-writer paradox (H4/Gemini-A): an in-process queue
cannot serialize writers running on *other hosts* against a shared network
workspace. A mutating command acquires a lease (owner + TTL + fencing
token) before any write; other hosts refuse mutating commands until the
lease expires or is explicitly released/handed over. This serializes the
*operator session*, avoiding granular SMB/SQLite lock contention entirely —
single-host-per-program affinity is the intended operating model, and this
module is the primitive that enforces it.

ADF-W1.13 (specs/arch-data-fix.md Appendix A.11): the lease key is
``(program_id, mutation_domain)`` rather than one global lock per program,
so independent mutation domains (e.g. ``confirm_publish`` vs
``actuation_dispatch``) can proceed concurrently on the same program while
same-domain operations still serialize. ``mutation_domain`` defaults to
``"workspace"`` everywhere, so every pre-ADF-W1.13 caller (and a
pre-ADF-W1.13 on-disk database, migrated in place -- see ``_ensure_schema``)
keeps its exact prior single-lease behavior unchanged.

This module provides the lease primitive only. Wiring specific commands to
acquire their Appendix A.11 domain before writing is separate integration
work (ADF-W1.10).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from sqlite3 import Connection

from src.core._db import open_program_db_with_retry as open_program_db

PROGRAMS_ROOT = Path(__file__).resolve().parents[2] / "programs"
_DB_FILENAME = "workspace_lease.sqlite3"
_DEFAULT_TTL_SECONDS = 300

#: ADF-W1.13 default domain: preserves exact pre-existing single-lease-per-program behavior.
DEFAULT_MUTATION_DOMAIN = "workspace"

#: Appendix A.11 mutation domain for the actuation outbox (ADF-W1.3/W1.10):
#: dispatch of approved writes to an external provider (ADO/Graph/Teams).
ACTUATION_DISPATCH_DOMAIN = "actuation_dispatch"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS workspace_lease (
    mutation_domain TEXT PRIMARY KEY,
    owner           TEXT NOT NULL,
    fencing_token   INTEGER NOT NULL,
    acquired_at     TEXT NOT NULL,
    expires_at      TEXT NOT NULL
);
"""


def _ensure_schema(connection: Connection) -> None:
    """Create the current-shape table, migrating a pre-ADF-W1.13 singleton
    row (``id = 1``, no ``mutation_domain`` column) in place if present."""
    existing_columns = {row[1] for row in connection.execute("PRAGMA table_info(workspace_lease)").fetchall()}
    if existing_columns and "mutation_domain" not in existing_columns:
        connection.execute("ALTER TABLE workspace_lease RENAME TO workspace_lease_legacy_v1")
        connection.executescript(_SCHEMA_SQL)
        connection.execute(
            "INSERT INTO workspace_lease (mutation_domain, owner, fencing_token, acquired_at, expires_at) "
            "SELECT ?, owner, fencing_token, acquired_at, expires_at "
            "FROM workspace_lease_legacy_v1 WHERE id = 1",
            (DEFAULT_MUTATION_DOMAIN,),
        )
        connection.execute("DROP TABLE workspace_lease_legacy_v1")
        # SQLite auto-commits any open transaction when it runs DDL; force
        # Python's sqlite3 in-transaction bookkeeping back in sync so the
        # caller's subsequent ``BEGIN IMMEDIATE`` does not fail with
        # "cannot start a transaction within a transaction".
        connection.commit()
    else:
        connection.executescript(_SCHEMA_SQL)


class LeaseHeldByAnotherOwner(Exception):
    """Raised when the lease is currently held by a different, non-expired owner."""

    def __init__(self, holder: str, expires_at: datetime, *, mutation_domain: str = DEFAULT_MUTATION_DOMAIN) -> None:
        self.holder = holder
        self.expires_at = expires_at
        self.mutation_domain = mutation_domain
        super().__init__(
            f"workspace lease for domain {mutation_domain!r} held by {holder!r} until {expires_at.isoformat()}"
        )


class LeaseFencingTokenStale(Exception):
    """Raised when a renew/release call presents a fencing token that is no
    longer current — the caller's lease was superseded (its TTL expired and
    another owner took over) while it still believed it held the lease."""

    def __init__(self, presented: int, current: int, *, mutation_domain: str = DEFAULT_MUTATION_DOMAIN) -> None:
        self.presented = presented
        self.current = current
        self.mutation_domain = mutation_domain
        super().__init__(f"stale fencing token {presented} for domain {mutation_domain!r} (current is {current})")


@dataclass(frozen=True, slots=True)
class LeaseHandle:
    program_id: str
    owner: str
    fencing_token: int
    acquired_at: datetime
    expires_at: datetime
    mutation_domain: str = DEFAULT_MUTATION_DOMAIN


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
    mutation_domain: str = DEFAULT_MUTATION_DOMAIN,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    programs_root: Path = PROGRAMS_ROOT,
) -> LeaseHandle:
    """Acquire (or renew, if ``owner`` already holds it) the lease for
    ``(program_id, mutation_domain)``.

    Raises ``LeaseHeldByAnotherOwner`` if a different, non-expired owner
    currently holds that domain's lease. Independent domains on the same
    program never contend with each other. ``BEGIN IMMEDIATE`` takes the
    write lock before the read, so the read-decide-write sequence is atomic
    against concurrent acquisition attempts from other connections/hosts.
    """
    path = get_workspace_lease_db_path(program_id, programs_root=programs_root)
    with open_program_db(path, durability="strict") as connection:
        _ensure_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT owner, fencing_token, acquired_at, expires_at FROM workspace_lease WHERE mutation_domain = ?",
            (mutation_domain,),
        ).fetchone()

        now = _now()
        if row is not None:
            current_owner, current_token, _acquired_at, expires_at_raw = row
            expires_at = _parse(expires_at_raw)
            if current_owner != owner and now < expires_at:
                raise LeaseHeldByAnotherOwner(current_owner, expires_at, mutation_domain=mutation_domain)
            next_token = int(current_token) + 1
        else:
            next_token = 1

        expires_at = now + timedelta(seconds=ttl_seconds)
        connection.execute(
            "INSERT INTO workspace_lease (mutation_domain, owner, fencing_token, acquired_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(mutation_domain) DO UPDATE SET owner = excluded.owner, fencing_token = excluded.fencing_token, "
            "acquired_at = excluded.acquired_at, expires_at = excluded.expires_at",
            (mutation_domain, owner, next_token, _wire(now), _wire(expires_at)),
        )
        return LeaseHandle(
            program_id=program_id,
            owner=owner,
            fencing_token=next_token,
            acquired_at=now,
            expires_at=expires_at,
            mutation_domain=mutation_domain,
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
        _ensure_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT owner, fencing_token FROM workspace_lease WHERE mutation_domain = ?",
            (handle.mutation_domain,),
        ).fetchone()
        if row is None:
            raise LeaseFencingTokenStale(handle.fencing_token, 0, mutation_domain=handle.mutation_domain)
        current_owner, current_token = row
        if int(current_token) != handle.fencing_token or current_owner != handle.owner:
            raise LeaseFencingTokenStale(handle.fencing_token, int(current_token), mutation_domain=handle.mutation_domain)

        now = _now()
        expires_at = now + timedelta(seconds=ttl_seconds)
        connection.execute(
            "UPDATE workspace_lease SET acquired_at = ?, expires_at = ? WHERE mutation_domain = ?",
            (_wire(now), _wire(expires_at), handle.mutation_domain),
        )
        return LeaseHandle(
            program_id=handle.program_id,
            owner=handle.owner,
            fencing_token=handle.fencing_token,
            acquired_at=now,
            expires_at=expires_at,
            mutation_domain=handle.mutation_domain,
        )


def release_lease(handle: LeaseHandle, *, programs_root: Path = PROGRAMS_ROOT) -> None:
    """Release a held lease. Idempotent no-op if the lease is already gone
    or has been superseded by a newer fencing token (nothing to release)."""
    path = get_workspace_lease_db_path(handle.program_id, programs_root=programs_root)
    with open_program_db(path, durability="strict") as connection:
        _ensure_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT owner, fencing_token FROM workspace_lease WHERE mutation_domain = ?",
            (handle.mutation_domain,),
        ).fetchone()
        if row is None:
            return
        current_owner, current_token = row
        if int(current_token) != handle.fencing_token or current_owner != handle.owner:
            return  # already superseded; nothing to release
        connection.execute("DELETE FROM workspace_lease WHERE mutation_domain = ?", (handle.mutation_domain,))


def read_lease_state(
    program_id: str,
    *,
    mutation_domain: str = DEFAULT_MUTATION_DOMAIN,
    programs_root: Path = PROGRAMS_ROOT,
) -> LeaseHandle | None:
    """Read the current lease state for ``(program_id, mutation_domain)`` without acquiring or modifying it."""
    path = get_workspace_lease_db_path(program_id, programs_root=programs_root)
    if not path.exists():
        return None
    with open_program_db(path, read_only=True) as connection:
        try:
            row = connection.execute(
                "SELECT owner, fencing_token, acquired_at, expires_at FROM workspace_lease WHERE mutation_domain = ?",
                (mutation_domain,),
            ).fetchone()
        except Exception:
            # Pre-ADF-W1.13 on-disk schema (no mutation_domain column) opened
            # read-only: cannot migrate in place (read-only connection), and
            # a read-only caller has no safe write path here either. Treat as
            # absent; the next read-write call (acquire/renew/release) will
            # migrate the schema.
            return None
        if row is None:
            return None
        owner, fencing_token, acquired_at_raw, expires_at_raw = row
        return LeaseHandle(
            program_id=program_id,
            owner=str(owner),
            fencing_token=int(fencing_token),
            acquired_at=_parse(acquired_at_raw),
            expires_at=_parse(expires_at_raw),
            mutation_domain=mutation_domain,
        )


def is_lease_expired(handle: LeaseHandle, *, at: datetime | None = None) -> bool:
    return (at or _now()) >= handle.expires_at
