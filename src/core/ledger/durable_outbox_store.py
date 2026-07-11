"""Generic durable outbox engine (arch-fix.md Phase 1, CPK).

Table-agnostic lease/attempt/dead-letter mechanics for at-least-once /
effectively-once actuation. This is the *engine* only: AF-7 (Phase 3) will
give the ADO/Graph/Teams actuation surface its OWN table via a distinct
``domain``/``db_path`` — it must NOT reuse REV's ``projection_outbox``
(a different bounded context; see arch-fix.md §0.5). Any caller that needs
outbox semantics (lease, retry, dead-letter) can open one of these against
its own SQLite file.

State machine (arch-fix.md §A.7): ``pending -> leased -> dispatched ->
succeeded -> audited -> completed``. Non-happy-path terminals:
``failed_retryable`` (transient; re-leasable once due — this is the retry
loop, not a dead end), ``uncertain_remote_state`` | ``failed_terminal`` |
``compensation_required`` | ``dead_letter`` (all require an operator to
call ``mark_manually_resolved`` to close out — no automatic transition
out of these). A stale lease (``lease_expires_at`` in the past) is
reclaimable by a *different* owner via ``lease_next`` — the previous
owner's fencing token becomes invalid the moment that happens, so it must
re-check its own lease before continuing to act (callers pass their own
lease and get an error if it's stale — see ``verify_lease_current``).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core._db import open_program_db_with_retry as open_program_db

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS outbox (
    outbox_id           TEXT PRIMARY KEY,
    domain              TEXT NOT NULL,
    correlation_id      TEXT NOT NULL,
    payload_json        TEXT NOT NULL,
    status              TEXT NOT NULL,
    lease_owner         TEXT,
    lease_fencing_token  INTEGER NOT NULL DEFAULT 0,
    lease_expires_at    TEXT,
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    next_attempt_at     TEXT,
    remote_request_hash TEXT,
    remote_response_hash TEXT,
    failure_reason      TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_outbox_domain_status ON outbox (domain, status);
CREATE INDEX IF NOT EXISTS ix_outbox_correlation ON outbox (correlation_id);
"""

_TRUE_TERMINALS = frozenset(
    {"uncertain_remote_state", "failed_terminal", "compensation_required", "dead_letter", "manually_resolved", "completed"}
)
_RELEASABLE_STATUSES = ("pending", "failed_retryable")


class OutboxError(Exception):
    pass


class LeaseNoLongerCurrent(OutboxError):
    def __init__(self, outbox_id: str) -> None:
        self.outbox_id = outbox_id
        super().__init__(f"lease on outbox row {outbox_id!r} is no longer current")


@dataclass(frozen=True, slots=True)
class OutboxEntry:
    outbox_id: str
    domain: str
    correlation_id: str
    payload_json: str
    status: str
    lease_owner: str | None
    lease_fencing_token: int
    lease_expires_at: datetime | None
    attempt_count: int
    next_attempt_at: datetime | None
    remote_request_hash: str | None
    remote_response_hash: str | None
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _wire(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))


def _entry_from_row(row: tuple) -> OutboxEntry:  # type: ignore[type-arg]
    (
        outbox_id, domain, correlation_id, payload_json, status,
        lease_owner, lease_fencing_token, lease_expires_at, attempt_count,
        next_attempt_at, remote_request_hash, remote_response_hash,
        failure_reason, created_at, updated_at,
    ) = row
    return OutboxEntry(
        outbox_id=str(outbox_id),
        domain=str(domain),
        correlation_id=str(correlation_id),
        payload_json=str(payload_json),
        status=str(status),
        lease_owner=lease_owner,
        lease_fencing_token=int(lease_fencing_token),
        lease_expires_at=_parse(lease_expires_at),
        attempt_count=int(attempt_count),
        next_attempt_at=_parse(next_attempt_at),
        remote_request_hash=remote_request_hash,
        remote_response_hash=remote_response_hash,
        failure_reason=failure_reason,
        created_at=_parse(created_at),  # type: ignore[arg-type]
        updated_at=_parse(updated_at),  # type: ignore[arg-type]
    )


def enqueue(
    db_path: Path,
    outbox_id: str,
    *,
    domain: str,
    correlation_id: str,
    payload_json: str,
) -> OutboxEntry:
    now = _now()
    with open_program_db(db_path, durability="strict") as connection:
        connection.executescript(_SCHEMA_SQL)
        connection.execute(
            """
            INSERT INTO outbox (
                outbox_id, domain, correlation_id, payload_json, status,
                lease_fencing_token, attempt_count, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'pending', 0, 0, ?, ?)
            """,
            (outbox_id, domain, correlation_id, payload_json, _wire(now), _wire(now)),
        )
    entry = load_entry(db_path, outbox_id)
    assert entry is not None
    return entry


def load_entry(db_path: Path, outbox_id: str) -> OutboxEntry | None:
    if not db_path.exists():
        return None
    with open_program_db(db_path, read_only=True) as connection:
        row = connection.execute(
            "SELECT outbox_id, domain, correlation_id, payload_json, status, lease_owner, "
            "lease_fencing_token, lease_expires_at, attempt_count, next_attempt_at, "
            "remote_request_hash, remote_response_hash, failure_reason, created_at, updated_at "
            "FROM outbox WHERE outbox_id = ?",
            (outbox_id,),
        ).fetchone()
    return None if row is None else _entry_from_row(row)


def list_by_status(db_path: Path, domain: str, status: str) -> tuple[OutboxEntry, ...]:
    if not db_path.exists():
        return ()
    with open_program_db(db_path, read_only=True) as connection:
        rows = connection.execute(
            "SELECT outbox_id, domain, correlation_id, payload_json, status, lease_owner, "
            "lease_fencing_token, lease_expires_at, attempt_count, next_attempt_at, "
            "remote_request_hash, remote_response_hash, failure_reason, created_at, updated_at "
            "FROM outbox WHERE domain = ? AND status = ? ORDER BY created_at",
            (domain, status),
        ).fetchall()
    return tuple(_entry_from_row(row) for row in rows)


def lease_next(
    db_path: Path,
    domain: str,
    *,
    owner: str,
    ttl_seconds: int = 300,
) -> OutboxEntry | None:
    """Atomically lease the oldest eligible row for ``domain``: a ``pending``
    row, a due ``failed_retryable`` row, or any row whose previous lease has
    expired (stale-worker reclaim). Returns None if nothing is eligible.
    """
    now = _now()
    with open_program_db(db_path, durability="strict") as connection:
        connection.executescript(_SCHEMA_SQL)
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT outbox_id, lease_fencing_token FROM outbox
            WHERE domain = ? AND (
                status IN ('pending', 'failed_retryable')
                AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                OR (status = 'leased' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?)
            )
            ORDER BY created_at
            LIMIT 1
            """,
            (domain, _wire(now), _wire(now)),
        ).fetchone()
        if row is None:
            return None
        outbox_id, current_token = row
        next_token = int(current_token) + 1
        expires_at = now + timedelta(seconds=ttl_seconds)
        connection.execute(
            """
            UPDATE outbox SET status = 'leased', lease_owner = ?, lease_fencing_token = ?,
                lease_expires_at = ?, attempt_count = attempt_count + 1, updated_at = ?
            WHERE outbox_id = ?
            """,
            (owner, next_token, _wire(expires_at), _wire(now), outbox_id),
        )
    return load_entry(db_path, str(outbox_id))


def _transition(
    db_path: Path,
    outbox_id: str,
    *,
    owner: str,
    fencing_token: int,
    new_status: str,
    extra_columns: dict[str, object] | None = None,
) -> OutboxEntry:
    now = _now()
    columns = dict(extra_columns or {})
    columns["status"] = new_status
    columns["updated_at"] = _wire(now)
    set_clause = ", ".join(f"{key} = ?" for key in columns)
    values = list(columns.values())

    with open_program_db(db_path, durability="strict") as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT lease_owner, lease_fencing_token FROM outbox WHERE outbox_id = ?", (outbox_id,)
        ).fetchone()
        if row is None:
            raise OutboxError(f"unknown outbox row {outbox_id!r}")
        current_owner, current_token = row
        if current_owner != owner or int(current_token) != fencing_token:
            raise LeaseNoLongerCurrent(outbox_id)
        connection.execute(f"UPDATE outbox SET {set_clause} WHERE outbox_id = ?", (*values, outbox_id))
    entry = load_entry(db_path, outbox_id)
    assert entry is not None
    return entry


def mark_dispatched(db_path: Path, outbox_id: str, *, owner: str, fencing_token: int, remote_request_hash: str | None = None) -> OutboxEntry:
    return _transition(db_path, outbox_id, owner=owner, fencing_token=fencing_token, new_status="dispatched", extra_columns={"remote_request_hash": remote_request_hash})


def mark_succeeded(db_path: Path, outbox_id: str, *, owner: str, fencing_token: int, remote_response_hash: str | None = None) -> OutboxEntry:
    return _transition(db_path, outbox_id, owner=owner, fencing_token=fencing_token, new_status="succeeded", extra_columns={"remote_response_hash": remote_response_hash})


def mark_audited(db_path: Path, outbox_id: str, *, owner: str, fencing_token: int) -> OutboxEntry:
    return _transition(db_path, outbox_id, owner=owner, fencing_token=fencing_token, new_status="audited")


def mark_completed(db_path: Path, outbox_id: str, *, owner: str, fencing_token: int) -> OutboxEntry:
    return _transition(db_path, outbox_id, owner=owner, fencing_token=fencing_token, new_status="completed")


def mark_failed_retryable(
    db_path: Path, outbox_id: str, *, owner: str, fencing_token: int, reason: str, next_attempt_at: datetime
) -> OutboxEntry:
    return _transition(
        db_path, outbox_id, owner=owner, fencing_token=fencing_token, new_status="failed_retryable",
        extra_columns={"failure_reason": reason, "next_attempt_at": _wire(next_attempt_at)},
    )


def mark_failed_terminal(db_path: Path, outbox_id: str, *, owner: str, fencing_token: int, reason: str) -> OutboxEntry:
    return _transition(db_path, outbox_id, owner=owner, fencing_token=fencing_token, new_status="failed_terminal", extra_columns={"failure_reason": reason})


def mark_uncertain_remote_state(db_path: Path, outbox_id: str, *, owner: str, fencing_token: int, reason: str) -> OutboxEntry:
    return _transition(db_path, outbox_id, owner=owner, fencing_token=fencing_token, new_status="uncertain_remote_state", extra_columns={"failure_reason": reason})


def mark_compensation_required(db_path: Path, outbox_id: str, *, owner: str, fencing_token: int, reason: str) -> OutboxEntry:
    return _transition(db_path, outbox_id, owner=owner, fencing_token=fencing_token, new_status="compensation_required", extra_columns={"failure_reason": reason})


def mark_dead_letter(db_path: Path, outbox_id: str, *, owner: str, fencing_token: int, reason: str) -> OutboxEntry:
    return _transition(db_path, outbox_id, owner=owner, fencing_token=fencing_token, new_status="dead_letter", extra_columns={"failure_reason": reason})


def mark_manually_resolved(db_path: Path, outbox_id: str, *, resolved_by: str, reason: str) -> OutboxEntry:
    """Operator close-out from any true-terminal state. Does not require a
    live lease — by the time an operator intervenes, no automated owner is
    expected to still hold one."""
    now = _now()
    with open_program_db(db_path, durability="strict") as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT status FROM outbox WHERE outbox_id = ?", (outbox_id,)).fetchone()
        if row is None:
            raise OutboxError(f"unknown outbox row {outbox_id!r}")
        (current_status,) = row
        if current_status not in _TRUE_TERMINALS or current_status == "manually_resolved":
            raise OutboxError(
                f"outbox row {outbox_id!r} is in status {current_status!r}; "
                "manually_resolved only applies from a true-terminal failure state"
            )
        connection.execute(
            "UPDATE outbox SET status = 'manually_resolved', lease_owner = ?, "
            "failure_reason = ?, updated_at = ? WHERE outbox_id = ?",
            (resolved_by, reason, _wire(now), outbox_id),
        )
    entry = load_entry(db_path, outbox_id)
    assert entry is not None
    return entry
