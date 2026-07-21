"""specs/people.md Phase 1, PPL-W1.2: workspace-global fenced registry lease.

Mirrors `src/core/workspace_lease.py`'s proven fencing-token/TTL/owner
design (arch-data-fix.md ADF-W1.13's CPK primitive) -- same `BEGIN
IMMEDIATE`-guarded read-decide-write sequence, same SQLite-backed
fencing-token increment, same crash-safety story -- deliberately NOT
reused directly, because its lease key is `(program_id, mutation_domain)`
and its DB path is program-scoped (`programs/<id>/ledger/...`). The
people registry is one shared root across every program; specs/people.md
§3.2 gap #18 names exactly this mismatch: "Existing shared-write
coordination is program-scoped, while the new registry is
workspace-global; separate program commands could otherwise mutate the
same shared files concurrently." This module is that missing
workspace-scoped primitive, not a redesign of the pattern.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from sqlite3 import Connection

from src.core._db import open_program_db_with_retry as open_registry_lease_db
from src.core.exceptions import ConfigError
from src.core.jsonl_utils import append_jsonl_line, read_jsonl_records
from src.core.people_registry_identity import load_registry_config

_DB_FILENAME = "registry_lease.sqlite3"
_AUDIT_FILENAME = "registry_lease_audit.jsonl"
_DEFAULT_TTL_SECONDS = 300
_AUDIT_MAX_BYTES = 10 * 1024 * 1024  # matches proposal_audit.jsonl's rotation cap

#: Mirrors workspace_lease.py's DEFAULT_MUTATION_DOMAIN convention -- most
#: registry commits use this single domain; a future caller may pass a
#: distinct one if independent registry mutation classes ever need to
#: proceed concurrently (workspace_lease.py's own precedent for that).
DEFAULT_MUTATION_DOMAIN = "registry"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS registry_lease (
    mutation_domain TEXT PRIMARY KEY,
    owner           TEXT NOT NULL,
    fencing_token   INTEGER NOT NULL,
    acquired_at     TEXT NOT NULL,
    expires_at      TEXT NOT NULL
);
"""


def _ensure_schema(connection: Connection) -> None:
    connection.executescript(_SCHEMA_SQL)


class RegistryLeaseHeldByAnotherOwner(Exception):
    def __init__(self, holder: str, expires_at: datetime, *, mutation_domain: str = DEFAULT_MUTATION_DOMAIN) -> None:
        self.holder = holder
        self.expires_at = expires_at
        self.mutation_domain = mutation_domain
        super().__init__(f"registry lease for domain {mutation_domain!r} held by {holder!r} until {expires_at.isoformat()}")


class RegistryLeaseFencingTokenStale(Exception):
    def __init__(self, presented: int, current: int, *, mutation_domain: str = DEFAULT_MUTATION_DOMAIN) -> None:
        self.presented = presented
        self.current = current
        self.mutation_domain = mutation_domain
        super().__init__(f"stale fencing token {presented} for registry domain {mutation_domain!r} (current is {current})")


@dataclass(frozen=True, slots=True)
class RegistryLeaseHandle:
    owner: str
    fencing_token: int
    acquired_at: datetime
    expires_at: datetime
    mutation_domain: str = DEFAULT_MUTATION_DOMAIN


def get_registry_lease_db_path(knowledge_root: Path) -> Path:
    return knowledge_root / ".state" / _DB_FILENAME


def _registry_lease_audit_path(knowledge_root: Path) -> Path:
    return knowledge_root / ".state" / _AUDIT_FILENAME


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _wire(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def acquire_registry_lease(
    owner: str,
    *,
    mutation_domain: str = DEFAULT_MUTATION_DOMAIN,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    knowledge_root: Path,
) -> RegistryLeaseHandle:
    """Acquire (or renew, if `owner` already holds it) the workspace-global
    registry lease for `mutation_domain`. Raises
    `RegistryLeaseHeldByAnotherOwner` if a different, non-expired owner
    currently holds it."""
    path = get_registry_lease_db_path(knowledge_root)
    with open_registry_lease_db(path, durability="strict") as connection:
        _ensure_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT owner, fencing_token, acquired_at, expires_at FROM registry_lease WHERE mutation_domain = ?",
            (mutation_domain,),
        ).fetchone()

        now = _now()
        if row is not None:
            current_owner, current_token, _acquired_at, expires_at_raw = row
            expires_at = _parse(expires_at_raw)
            if current_owner != owner and now < expires_at:
                raise RegistryLeaseHeldByAnotherOwner(current_owner, expires_at, mutation_domain=mutation_domain)
            next_token = int(current_token) + 1
        else:
            next_token = 1

        expires_at = now + timedelta(seconds=ttl_seconds)
        connection.execute(
            "INSERT INTO registry_lease (mutation_domain, owner, fencing_token, acquired_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(mutation_domain) DO UPDATE SET owner = excluded.owner, fencing_token = excluded.fencing_token, "
            "acquired_at = excluded.acquired_at, expires_at = excluded.expires_at",
            (mutation_domain, owner, next_token, _wire(now), _wire(expires_at)),
        )
        return RegistryLeaseHandle(owner=owner, fencing_token=next_token, acquired_at=now, expires_at=expires_at, mutation_domain=mutation_domain)


def renew_registry_lease(
    handle: RegistryLeaseHandle,
    *,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    knowledge_root: Path,
) -> RegistryLeaseHandle:
    path = get_registry_lease_db_path(knowledge_root)
    with open_registry_lease_db(path, durability="strict") as connection:
        _ensure_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT owner, fencing_token FROM registry_lease WHERE mutation_domain = ?", (handle.mutation_domain,)
        ).fetchone()
        if row is None:
            raise RegistryLeaseFencingTokenStale(handle.fencing_token, 0, mutation_domain=handle.mutation_domain)
        current_owner, current_token = row
        if int(current_token) != handle.fencing_token or current_owner != handle.owner:
            raise RegistryLeaseFencingTokenStale(handle.fencing_token, int(current_token), mutation_domain=handle.mutation_domain)

        now = _now()
        expires_at = now + timedelta(seconds=ttl_seconds)
        connection.execute(
            "UPDATE registry_lease SET acquired_at = ?, expires_at = ? WHERE mutation_domain = ?",
            (_wire(now), _wire(expires_at), handle.mutation_domain),
        )
        return RegistryLeaseHandle(
            owner=handle.owner, fencing_token=handle.fencing_token, acquired_at=now, expires_at=expires_at, mutation_domain=handle.mutation_domain
        )


def release_registry_lease(handle: RegistryLeaseHandle, *, knowledge_root: Path) -> None:
    """Idempotent no-op if the lease is already gone or superseded."""
    path = get_registry_lease_db_path(knowledge_root)
    with open_registry_lease_db(path, durability="strict") as connection:
        _ensure_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT owner, fencing_token FROM registry_lease WHERE mutation_domain = ?", (handle.mutation_domain,)
        ).fetchone()
        if row is None:
            return
        current_owner, current_token = row
        if int(current_token) != handle.fencing_token or current_owner != handle.owner:
            return
        connection.execute("DELETE FROM registry_lease WHERE mutation_domain = ?", (handle.mutation_domain,))


def read_registry_lease_state(
    *, mutation_domain: str = DEFAULT_MUTATION_DOMAIN, knowledge_root: Path
) -> RegistryLeaseHandle | None:
    path = get_registry_lease_db_path(knowledge_root)
    if not path.exists():
        return None
    with open_registry_lease_db(path, read_only=True) as connection:
        row = connection.execute(
            "SELECT owner, fencing_token, acquired_at, expires_at FROM registry_lease WHERE mutation_domain = ?",
            (mutation_domain,),
        ).fetchone()
        if row is None:
            return None
        owner, fencing_token, acquired_at_raw, expires_at_raw = row
        return RegistryLeaseHandle(
            owner=str(owner),
            fencing_token=int(fencing_token),
            acquired_at=_parse(acquired_at_raw),
            expires_at=_parse(expires_at_raw),
            mutation_domain=mutation_domain,
        )


def is_registry_lease_expired(handle: RegistryLeaseHandle, *, at: datetime | None = None) -> bool:
    return (at or _now()) >= handle.expires_at


def force_release_registry_lease(
    *,
    authorized_principal: str,
    reason: str,
    mutation_domain: str = DEFAULT_MUTATION_DOMAIN,
    knowledge_root: Path,
) -> None:
    """specs/people.md §6.7: "Stale lease: wait for TTL or use `vertex kb
    registry lease release --force --reason <text>`, which requires an
    authorized authenticated principal, increments fencing state, and
    appends an audit record."

    Authorization checks `authorized_principal` against
    `registry.yaml`'s `directory_steward_principals` (§6.6/§8.1's binding
    authenticated-actor rule, §5.6 item 20). Fencing state is
    INCREMENTED, never reset to a fresh sequence -- the row is UPDATEd in
    place (owner marked force-released, token bumped, already-expired) so
    any stale holder's OLD token is permanently rejected by a subsequent
    renew/release, and the next genuine `acquire_registry_lease` call
    continues the monotonic token sequence rather than restarting it.
    """
    if not reason or not reason.strip():
        raise ConfigError("--reason is required to force-release the registry lease.")

    config = load_registry_config(knowledge_root)
    if config is None or authorized_principal not in config.directory_steward_principals:
        raise ConfigError(
            f"Principal {authorized_principal!r} is not an authorized directory steward "
            f"(registry.yaml's directory_steward_principals). Force-release refused."
        )

    path = get_registry_lease_db_path(knowledge_root)
    now = _now()
    with open_registry_lease_db(path, durability="strict") as connection:
        _ensure_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT owner, fencing_token FROM registry_lease WHERE mutation_domain = ?", (mutation_domain,)
        ).fetchone()
        if row is None:
            old_owner, old_token, new_token = None, None, 1
            connection.execute(
                "INSERT INTO registry_lease (mutation_domain, owner, fencing_token, acquired_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (mutation_domain, f"force_released_by:{authorized_principal}", new_token, _wire(now), _wire(now)),
            )
        else:
            old_owner, old_token = row
            new_token = int(old_token) + 1
            connection.execute(
                "UPDATE registry_lease SET owner = ?, fencing_token = ?, acquired_at = ?, expires_at = ? WHERE mutation_domain = ?",
                (f"force_released_by:{authorized_principal}", new_token, _wire(now), _wire(now), mutation_domain),
            )

    _append_force_release_audit_record(
        knowledge_root,
        mutation_domain=mutation_domain,
        prior_owner=old_owner,
        prior_fencing_token=old_token,
        new_fencing_token=new_token,
        authorized_principal=authorized_principal,
        reason=reason.strip(),
        recorded_at=now,
    )


def _append_force_release_audit_record(
    knowledge_root: Path,
    *,
    mutation_domain: str,
    prior_owner: str | None,
    prior_fencing_token: int | None,
    new_fencing_token: int,
    authorized_principal: str,
    reason: str,
    recorded_at: datetime,
) -> None:
    audit_path = _registry_lease_audit_path(knowledge_root)
    record = {
        "recorded_at": _wire(recorded_at),
        "mutation_domain": mutation_domain,
        "prior_owner": prior_owner,
        "prior_fencing_token": prior_fencing_token,
        "new_fencing_token": new_fencing_token,
        "authorized_principal": authorized_principal,
        "reason": reason,
    }
    append_jsonl_line(audit_path, json.dumps(record, sort_keys=True) + "\n", max_bytes=_AUDIT_MAX_BYTES)


def read_force_release_audit_records(knowledge_root: Path) -> tuple[dict, ...]:
    return read_jsonl_records(_registry_lease_audit_path(knowledge_root))
