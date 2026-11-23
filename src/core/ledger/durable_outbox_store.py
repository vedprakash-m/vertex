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

import hashlib
import json
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core._db import open_program_db_with_retry as open_program_db
from src.core.unit_of_work import open_unit_of_work
from src.core.workspace_lease import ACTUATION_DISPATCH_DOMAIN, LeaseHandle, get_workspace_lease_db_path

_transition_unit_of_work = open_unit_of_work

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
    {
        "uncertain_remote_state", "failed_terminal", "compensation_required", "dead_letter",
        "manually_resolved", "completed", "cancelled",
    }
)
_RELEASABLE_STATUSES = ("pending", "failed_retryable")
_SUCCESS_RECEIPTS_SQL = """
CREATE TABLE IF NOT EXISTS outbox_success_receipts (
    outbox_id TEXT PRIMARY KEY,
    receipt_json TEXT NOT NULL
)
"""
_RECONCILIATION_RECEIPTS_SQL = """
CREATE TABLE IF NOT EXISTS outbox_reconciliation_receipts (
    outbox_id TEXT PRIMARY KEY,
    receipt_json TEXT NOT NULL
)
"""
_STATE_RECEIPTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS outbox_state_receipts (
    receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    outbox_id TEXT NOT NULL,
    terminal_status TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    receipt_json TEXT NOT NULL
)
"""
_STATE_RECEIPTS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS ix_outbox_state_receipts_outbox
    ON outbox_state_receipts (outbox_id, receipt_id)
"""
_STATE_RECEIPTS_SQL = _STATE_RECEIPTS_TABLE_SQL + ";\n" + _STATE_RECEIPTS_INDEX_SQL + ";"
_RECEIPTED_STATUSES = _TRUE_TERMINALS | {"completed", "succeeded"}
_TRANSITION_PREDECESSORS = {
    "dispatched": frozenset({"leased"}),
    "succeeded": frozenset({"dispatched"}),
    "audited": frozenset({"succeeded"}),
    "completed": frozenset({"audited", "leased"}),
    "failed_retryable": frozenset({"leased"}),
    "failed_terminal": frozenset({"leased", "dispatched"}),
    "uncertain_remote_state": frozenset({"dispatched"}),
    "compensation_required": frozenset({"succeeded", "audited"}),
    "dead_letter": frozenset({"leased", "failed_retryable"}),
}
_KNOWN_STATUSES = frozenset({"pending", "leased"}) | frozenset(_TRANSITION_PREDECESSORS) | _TRUE_TERMINALS
_OUTBOX_COLUMNS = frozenset({
    "outbox_id", "domain", "correlation_id", "payload_json", "status", "lease_owner",
    "lease_fencing_token", "lease_expires_at", "attempt_count", "next_attempt_at",
    "remote_request_hash", "remote_response_hash", "failure_reason", "created_at", "updated_at",
})


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


def _scheduling_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise OutboxError("Outbox contains an invalid scheduling timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise OutboxError("Outbox contains an invalid scheduling timestamp") from None


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
    effect_id: str | None = None,
) -> OutboxEntry:
    now = _now()
    with open_program_db(db_path, durability="strict") as connection:
        connection.executescript(_SCHEMA_SQL)
        if effect_id is None:
            connection.execute(
                """
                INSERT INTO outbox (
                    outbox_id, domain, correlation_id, payload_json, status,
                    lease_fencing_token, attempt_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', 0, 0, ?, ?)
                """,
                (outbox_id, domain, correlation_id, payload_json, _wire(now), _wire(now)),
            )
            entry = connection.execute(
                "SELECT outbox_id, domain, correlation_id, payload_json, status, lease_owner, "
                "lease_fencing_token, lease_expires_at, attempt_count, next_attempt_at, "
                "remote_request_hash, remote_response_hash, failure_reason, created_at, updated_at "
                "FROM outbox WHERE outbox_id = ?",
                (outbox_id,),
            ).fetchone()
            assert entry is not None
            return _entry_from_row(tuple(entry))

        connection.execute(_EFFECTS_SQL)
        connection.execute("BEGIN IMMEDIATE")
        bound = connection.execute(
            "SELECT outbox_id FROM outbox_effects WHERE effect_id = ?",
            (effect_id,),
        ).fetchone()
        selected_id = str(bound[0]) if bound is not None else outbox_id
        selected = connection.execute(
            "SELECT outbox_id, domain, correlation_id, payload_json, status, lease_owner, "
            "lease_fencing_token, lease_expires_at, attempt_count, next_attempt_at, "
            "remote_request_hash, remote_response_hash, failure_reason, created_at, updated_at "
            "FROM outbox WHERE outbox_id = ?",
            (selected_id,),
        ).fetchone()
        if bound is not None:
            if selected is None:
                raise OutboxError("Effect id is bound to a missing outbox row")
            return _entry_from_row(tuple(selected))
        if selected is None:
            connection.execute(
                """
                INSERT INTO outbox (
                    outbox_id, domain, correlation_id, payload_json, status,
                    lease_fencing_token, attempt_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', 0, 0, ?, ?)
                """,
                (outbox_id, domain, correlation_id, payload_json, _wire(now), _wire(now)),
            )
            selected_id = outbox_id
        else:
            existing = _entry_from_row(tuple(selected))
            if (
                existing.domain != domain
                or existing.correlation_id != correlation_id
                or existing.payload_json != payload_json
            ):
                raise OutboxError("Existing outbox key has different domain, correlation or payload")
        connection.execute(
            "INSERT INTO outbox_effects (effect_id, outbox_id) VALUES (?, ?)",
            (effect_id, selected_id),
        )
        selected = connection.execute(
            "SELECT outbox_id, domain, correlation_id, payload_json, status, lease_owner, "
            "lease_fencing_token, lease_expires_at, attempt_count, next_attempt_at, "
            "remote_request_hash, remote_response_hash, failure_reason, created_at, updated_at "
            "FROM outbox WHERE outbox_id = ?",
            (selected_id,),
        ).fetchone()
        assert selected is not None
        return _entry_from_row(tuple(selected))


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


def read_status_counts(db_path: Path, domain: str) -> dict[str, int] | None:
    """Read one domain's status counts without creating the outbox or loading payloads."""
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            db_path.with_name(db_path.name + suffix).lstat()
        except FileNotFoundError:
            continue
        raise OutboxError("SQLite sidecars are present; refusing a read that may alter them")
    try:
        db_path.stat()
    except FileNotFoundError:
        return None
    with db_path.open("rb") as database_file:
        header = database_file.read(20)
    if header.startswith(b"SQLite format 3\x00") and 2 in header[18:20]:
        raise OutboxError("WAL-mode databases are unavailable under the strict no-write policy")
    with open_program_db(db_path, read_only=True) as connection:
        connection.execute("BEGIN")
        definition = connection.execute(
            "SELECT type, sql FROM sqlite_master WHERE name = ?", ("outbox",),
        ).fetchone()
        if (
            definition is None
            or definition["type"] != "table"
            or not isinstance(definition["sql"], str)
            or definition["sql"].upper().split(None, 2)[:2] != ["CREATE", "TABLE"]
        ):
            raise OutboxError("Outbox is not an ordinary table")
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(outbox)")}
        if not _OUTBOX_COLUMNS.issubset(columns):
            raise OutboxError("Outbox schema is incomplete")
        rows = connection.execute(
            "SELECT status, COUNT(*) AS count FROM outbox WHERE domain = ? GROUP BY status",
            (domain,),
        ).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        status = row["status"]
        if not isinstance(status, str) or status not in _KNOWN_STATUSES:
            raise OutboxError("Outbox contains an unrecognized status")
        counts[status] = int(row["count"])
    return counts


def load_success_receipt(db_path: Path, outbox_id: str) -> str | None:
    if not db_path.exists():
        return None
    with open_program_db(db_path, read_only=True) as connection:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'outbox_success_receipts'"
        ).fetchone() is None:
            return None
        row = connection.execute(
            "SELECT receipt_json FROM outbox_success_receipts WHERE outbox_id = ?", (outbox_id,),
        ).fetchone()
    return None if row is None else str(row[0])


def load_latest_receipt(db_path: Path, outbox_id: str) -> str | None:
    if not db_path.exists():
        return None
    with open_program_db(db_path, read_only=True) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'outbox_state_receipts'"
        ).fetchone()
        if table is None:
            return None
        row = connection.execute(
            "SELECT receipt_json FROM outbox_state_receipts "
            "WHERE outbox_id = ? ORDER BY receipt_id DESC LIMIT 1",
            (outbox_id,),
        ).fetchone()
    return None if row is None else str(row[0])


def record_outbox_receipt(
    db_path: Path,
    outbox_id: str,
    receipt: dict[str, object],
    *,
    expected_status: str | None = None,
    expected_entry: OutboxEntry | None = None,
) -> str:
    receipt_json = json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    status = str(receipt.get("terminal_status") or "")
    recorded_at = str(receipt.get("recorded_at") or _wire(_now()))
    with open_program_db(db_path, durability="strict") as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT status FROM outbox WHERE outbox_id = ?", (outbox_id,)).fetchone()
        if row is None:
            raise OutboxError(f"unknown outbox row {outbox_id!r}")
        if expected_status is not None and row[0] != expected_status:
            raise OutboxError(
                f"outbox row {outbox_id!r} is {row[0]!r}; "
                f"receipt expects {expected_status!r}"
            )
        if expected_entry is not None:
            current = connection.execute(
                "SELECT outbox_id, domain, correlation_id, payload_json, status, lease_owner, "
                "lease_fencing_token, lease_expires_at, attempt_count, next_attempt_at, "
                "remote_request_hash, remote_response_hash, failure_reason, created_at, updated_at "
                "FROM outbox WHERE outbox_id = ?",
                (outbox_id,),
            ).fetchone()
            if current is None or _entry_from_row(tuple(current)) != expected_entry:
                raise OutboxError(
                    f"outbox row {outbox_id!r} changed during reconciliation"
                )
        _append_state_receipt(connection, outbox_id, status, recorded_at, receipt_json)
    return receipt_json


def _append_state_receipt(
    connection,
    outbox_id: str,
    terminal_status: str,
    recorded_at: str,
    receipt_json: str,
) -> None:
    connection.execute(_STATE_RECEIPTS_TABLE_SQL)
    connection.execute(_STATE_RECEIPTS_INDEX_SQL)
    connection.execute(
        """
        INSERT INTO outbox_state_receipts (
            outbox_id, terminal_status, recorded_at, receipt_json
        ) VALUES (?, ?, ?, ?)
        """,
        (outbox_id, terminal_status, recorded_at, receipt_json),
    )


def _state_receipt(
    *,
    outbox_id: str,
    domain: str,
    correlation_id: str,
    payload_json: str,
    terminal_status: str,
    owner: str,
    fencing_token: int,
    attempt_count: int,
    reason: object,
    recorded_at: str,
    compensation_operation: str | None = None,
) -> str:
    operation: str | None = None
    try:
        payload = json.loads(payload_json)
        if isinstance(payload, dict) and isinstance(payload.get("operation"), str):
            operation = payload["operation"]
    except (TypeError, ValueError):
        pass
    return json.dumps(
        {
            "schema_version": "1",
            "outbox_id": outbox_id,
            "domain": domain,
            "correlation_id": correlation_id,
            "operation": operation,
            "payload_sha256": hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
            "terminal_status": terminal_status,
            "owner": owner,
            "fencing_token": fencing_token,
            "attempt_count": attempt_count,
            "reason": reason if isinstance(reason, str) else None,
            "compensation_operation": compensation_operation,
            "recorded_at": recorded_at,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def lease_next(
    db_path: Path,
    domain: str,
    *,
    owner: str,
    ttl_seconds: int = 300,
    eligible_operation: str | None = None,
) -> OutboxEntry | None:
    """Atomically lease the oldest eligible row for ``domain``: a ``pending``
    row, a due ``failed_retryable`` row, or any row whose previous lease has
    expired (stale-worker reclaim). Returns None if nothing is eligible.
    """
    with open_program_db(db_path, durability="strict") as connection:
        connection.executescript(_SCHEMA_SQL)
        connection.execute("BEGIN IMMEDIATE")
        now = _now()
        row = None
        oldest_created_at = None
        for candidate in connection.execute(
            """
            SELECT outbox_id, lease_fencing_token, status, lease_expires_at,
                   next_attempt_at, created_at
            FROM outbox
            WHERE domain = ? AND status IN ('pending', 'failed_retryable', 'leased')
              AND (
                  ? IS NULL
                  OR json_valid(payload_json) = 0
                  OR json_type(payload_json, '$.operation') IS NULL
                  OR json_extract(payload_json, '$.operation') = ?
              )
            """,
            (domain, eligible_operation, eligible_operation),
        ):
            created_at = _scheduling_timestamp(candidate["created_at"])
            deadline = candidate["lease_expires_at"] if candidate["status"] == "leased" else candidate["next_attempt_at"]
            if deadline is None:
                if candidate["status"] == "leased":
                    continue
            elif _scheduling_timestamp(deadline) > now:
                continue
            if oldest_created_at is None or created_at < oldest_created_at:
                row = candidate
                oldest_created_at = created_at
        if row is None:
            return None
        if row["status"] == "leased" and row["next_attempt_at"] is not None:
            _scheduling_timestamp(row["next_attempt_at"])
        outbox_id, current_token = row["outbox_id"], row["lease_fencing_token"]
        next_token = int(current_token) + 1
        now = _now()
        expires_at = now + timedelta(seconds=ttl_seconds)
        updated = connection.execute(
            """
            UPDATE outbox SET status = 'leased', lease_owner = ?, lease_fencing_token = ?,
                lease_expires_at = ?, attempt_count = attempt_count + 1, updated_at = ?
            WHERE outbox_id = ?
            RETURNING outbox_id, domain, correlation_id, payload_json, status, lease_owner,
                lease_fencing_token, lease_expires_at, attempt_count, next_attempt_at,
                remote_request_hash, remote_response_hash, failure_reason, created_at, updated_at
            """,
            (owner, next_token, _wire(expires_at), _wire(now), outbox_id),
        ).fetchone()
        assert updated is not None
        leased = _entry_from_row(tuple(updated))
    return leased


def classify_expired_dispatched(
    db_path: Path,
    observed: OutboxEntry,
    *,
    workspace_lease: LeaseHandle,
    programs_root: Path,
) -> OutboxEntry:
    with open_unit_of_work({
        "outbox": db_path,
        "fence": get_workspace_lease_db_path(workspace_lease.program_id, programs_root=programs_root),
    }) as transaction:
        now = _now()
        if (
            observed.lease_expires_at is None
            or observed.lease_expires_at.utcoffset() is None
            or workspace_lease.acquired_at.utcoffset() is None
            or workspace_lease.expires_at.utcoffset() is None
            or observed.lease_expires_at > now
            or workspace_lease.expires_at <= now
        ):
            raise LeaseNoLongerCurrent(observed.outbox_id)
        row = transaction.connection.execute(
            """
            UPDATE outbox SET status = 'uncertain_remote_state', lease_owner = ?,
                lease_fencing_token = lease_fencing_token + 1,
                failure_reason = ?, updated_at = ?
            WHERE outbox_id = ? AND domain = ? AND correlation_id = ? AND payload_json = ?
                AND status = 'dispatched' AND lease_owner = ? AND lease_owner <> ?
                AND lease_fencing_token = ? AND lease_expires_at = ?
                AND EXISTS (
                    SELECT 1 FROM fence.workspace_lease WHERE mutation_domain = ?
                        AND owner = ? AND fencing_token = ? AND acquired_at = ? AND expires_at = ?
                )
            RETURNING outbox_id, domain, correlation_id, payload_json, status, lease_owner,
                lease_fencing_token, lease_expires_at, attempt_count, next_attempt_at,
                remote_request_hash, remote_response_hash, failure_reason, created_at, updated_at
            """,
            (
                workspace_lease.owner,
                "Expired dispatched attempt; remote outcome unknown; manual reconciliation required.",
                _wire(now), observed.outbox_id, workspace_lease.mutation_domain, observed.outbox_id,
                observed.payload_json, observed.lease_owner, workspace_lease.owner,
                observed.lease_fencing_token, _wire(observed.lease_expires_at), workspace_lease.mutation_domain,
                workspace_lease.owner, workspace_lease.fencing_token,
                _wire(workspace_lease.acquired_at), _wire(workspace_lease.expires_at),
            ),
        ).fetchone()
        if row is None:
            raise LeaseNoLongerCurrent(observed.outbox_id)
        result = _entry_from_row(tuple(row))
        recorded_at = str(_wire(now))
        receipt_json = _state_receipt(
            outbox_id=result.outbox_id,
            domain=result.domain,
            correlation_id=result.correlation_id,
            payload_json=result.payload_json,
            terminal_status=result.status,
            owner=workspace_lease.owner,
            fencing_token=result.lease_fencing_token,
            attempt_count=result.attempt_count,
            reason=result.failure_reason,
            recorded_at=recorded_at,
        )
        _append_state_receipt(
            transaction.connection,
            result.outbox_id,
            result.status,
            recorded_at,
            receipt_json,
        )
    return result


def _transition(
    db_path: Path,
    outbox_id: str,
    *,
    owner: str,
    fencing_token: int,
    new_status: str,
    extra_columns: dict[str, object] | None = None,
    success_receipt_json: str | None = None,
    reconciliation_receipt_json: str | None = None,
    compensation_operation: str | None = None,
    workspace_lease: LeaseHandle | None = None,
    programs_root: Path | None = None,
) -> OutboxEntry:
    now = _now()
    columns = dict(extra_columns or {})
    columns["status"] = new_status
    columns["updated_at"] = _wire(now)
    set_clause = ", ".join(f"{key} = ?" for key in columns)
    values = list(columns.values())

    has_fence = workspace_lease is not None and programs_root is not None
    with ExitStack() as stack:
        if has_fence:
            assert workspace_lease is not None and programs_root is not None
            transaction = stack.enter_context(
                _transition_unit_of_work({
                    "outbox": db_path,
                    "fence": get_workspace_lease_db_path(
                        workspace_lease.program_id, programs_root=programs_root
                    ),
                })
            )
            connection = transaction.connection
        else:
            connection = stack.enter_context(
                open_program_db(db_path, durability="strict")
            )
            connection.execute("BEGIN IMMEDIATE")
        if has_fence:
            assert workspace_lease is not None
            now = _now()
            fence = connection.execute(
                "SELECT owner, fencing_token, acquired_at, expires_at "
                "FROM fence.workspace_lease WHERE mutation_domain = ?",
                (workspace_lease.mutation_domain,),
            ).fetchone()
            if (
                workspace_lease.mutation_domain != ACTUATION_DISPATCH_DOMAIN
                or fence is None
                or fence["owner"] != workspace_lease.owner
                or int(fence["fencing_token"]) != workspace_lease.fencing_token
                or _scheduling_timestamp(fence["expires_at"]) <= now
            ):
                raise LeaseNoLongerCurrent(outbox_id)
        row = connection.execute(
            "SELECT lease_owner, lease_fencing_token, status, domain, correlation_id, "
            "payload_json, attempt_count FROM outbox WHERE outbox_id = ?",
            (outbox_id,),
        ).fetchone()
        if row is None:
            raise OutboxError(f"unknown outbox row {outbox_id!r}")
        (
            current_owner,
            current_token,
            current_status,
            domain,
            correlation_id,
            payload_json,
            attempt_count,
        ) = row
        if current_owner != owner or int(current_token) != fencing_token:
            raise LeaseNoLongerCurrent(outbox_id)
        if current_status in _TRUE_TERMINALS:
            raise OutboxError(f"outbox row {outbox_id!r} is terminal ({current_status!r}); automatic transition refused")
        if current_status not in _TRANSITION_PREDECESSORS[new_status] or (
            new_status == "completed" and current_status == "leased" and domain == ACTUATION_DISPATCH_DOMAIN
        ):
            raise OutboxError(
                f"outbox row {outbox_id!r}: transition {current_status!r} -> {new_status!r} refused"
            )
        if success_receipt_json is not None:
            connection.execute(_SUCCESS_RECEIPTS_SQL)
            connection.execute(
                "INSERT INTO outbox_success_receipts (outbox_id, receipt_json) VALUES (?, ?)",
                (outbox_id, success_receipt_json),
            )
        if reconciliation_receipt_json is not None:
            connection.execute(_RECONCILIATION_RECEIPTS_SQL)
            connection.execute(
                "INSERT INTO outbox_reconciliation_receipts (outbox_id, receipt_json) VALUES (?, ?) "
                "ON CONFLICT(outbox_id) DO UPDATE SET receipt_json = excluded.receipt_json",
                (outbox_id, reconciliation_receipt_json),
            )
        elif new_status == "uncertain_remote_state":
            connection.execute(_RECONCILIATION_RECEIPTS_SQL)
            connection.execute(
                "DELETE FROM outbox_reconciliation_receipts WHERE outbox_id = ?",
                (outbox_id,),
            )
        connection.execute(f"UPDATE outbox SET {set_clause} WHERE outbox_id = ?", (*values, outbox_id))
        if new_status in _RECEIPTED_STATUSES:
            recorded_at = str(columns["updated_at"])
            receipt_json = _state_receipt(
                outbox_id=outbox_id,
                domain=domain,
                correlation_id=correlation_id,
                payload_json=payload_json,
                terminal_status=new_status,
                owner=owner,
                fencing_token=fencing_token,
                attempt_count=int(attempt_count),
                reason=columns.get("failure_reason"),
                recorded_at=recorded_at,
                compensation_operation=compensation_operation,
            )
            _append_state_receipt(
                connection, outbox_id, new_status, recorded_at, receipt_json,
            )
    entry = load_entry(db_path, outbox_id)
    assert entry is not None
    return entry


def mark_dispatched(db_path: Path, outbox_id: str, *, owner: str, fencing_token: int, remote_request_hash: str | None = None) -> OutboxEntry:
    return _transition(db_path, outbox_id, owner=owner, fencing_token=fencing_token, new_status="dispatched", extra_columns={"remote_request_hash": remote_request_hash})


def mark_dispatched_with_workspace_lease(
    db_path: Path,
    observed: OutboxEntry,
    *,
    workspace_lease: LeaseHandle,
    programs_root: Path,
) -> OutboxEntry:
    with open_unit_of_work({
        "outbox": db_path,
        "fence": get_workspace_lease_db_path(workspace_lease.program_id, programs_root=programs_root),
    }) as transaction:
        now = _now()
        fence = transaction.connection.execute(
            "SELECT owner, fencing_token, acquired_at, expires_at FROM fence.workspace_lease WHERE mutation_domain = ?",
            (workspace_lease.mutation_domain,),
        ).fetchone()
        if (
            workspace_lease.mutation_domain != ACTUATION_DISPATCH_DOMAIN
            or workspace_lease.acquired_at.utcoffset() is None
            or workspace_lease.expires_at.utcoffset() is None
            or fence is None
            or fence["owner"] != workspace_lease.owner
            or fence["fencing_token"] != workspace_lease.fencing_token
            or fence["acquired_at"] != _wire(workspace_lease.acquired_at)
            or fence["expires_at"] != _wire(workspace_lease.expires_at)
            or workspace_lease.expires_at <= now
        ):
            raise LeaseNoLongerCurrent(observed.outbox_id)
        row = transaction.connection.execute(
            "SELECT outbox_id, domain, correlation_id, payload_json, status, lease_owner, "
            "lease_fencing_token, lease_expires_at, attempt_count, next_attempt_at, "
            "remote_request_hash, remote_response_hash, failure_reason, created_at, updated_at "
            "FROM outbox WHERE outbox_id = ?",
            (observed.outbox_id,),
        ).fetchone()
        if row is None:
            raise LeaseNoLongerCurrent(observed.outbox_id)
        try:
            current = _entry_from_row(tuple(row))
        except (AttributeError, OverflowError, TypeError, ValueError) as error:
            raise LeaseNoLongerCurrent(observed.outbox_id) from error
        if current.lease_owner != workspace_lease.owner or current.lease_fencing_token != observed.lease_fencing_token:
            raise LeaseNoLongerCurrent(observed.outbox_id)
        if current.status in _TRUE_TERMINALS:
            raise OutboxError(f"outbox row {observed.outbox_id!r} is terminal ({current.status!r}); automatic transition refused")
        if current.status != "leased":
            raise OutboxError(f"outbox row {observed.outbox_id!r}: transition {current.status!r} -> 'dispatched' refused")
        if (
            current != observed
            or observed.domain != ACTUATION_DISPATCH_DOMAIN
            or observed.lease_expires_at is None
            or observed.lease_expires_at.utcoffset() is None
            or observed.lease_expires_at <= now
        ):
            raise LeaseNoLongerCurrent(observed.outbox_id)
        row = transaction.connection.execute(
            "UPDATE outbox SET status = 'dispatched', remote_request_hash = NULL, updated_at = ? "
            "WHERE outbox_id = ? RETURNING outbox_id, domain, correlation_id, payload_json, status, lease_owner, "
            "lease_fencing_token, lease_expires_at, attempt_count, next_attempt_at, "
            "remote_request_hash, remote_response_hash, failure_reason, created_at, updated_at",
            (_wire(now), observed.outbox_id),
        ).fetchone()
        assert row is not None
        result = _entry_from_row(tuple(row))
    return result


def mark_succeeded(
    db_path: Path, outbox_id: str, *, owner: str, fencing_token: int,
    remote_response_hash: str | None = None, success_receipt_json: str | None = None,
    workspace_lease: LeaseHandle | None = None, programs_root: Path | None = None,
) -> OutboxEntry:
    return _transition(
        db_path, outbox_id, owner=owner, fencing_token=fencing_token, new_status="succeeded",
        extra_columns={"remote_response_hash": remote_response_hash}, success_receipt_json=success_receipt_json,
        workspace_lease=workspace_lease, programs_root=programs_root,
    )


def mark_audited(
    db_path: Path, outbox_id: str, *, owner: str, fencing_token: int,
    workspace_lease: LeaseHandle | None = None, programs_root: Path | None = None,
) -> OutboxEntry:
    return _transition(
        db_path, outbox_id, owner=owner, fencing_token=fencing_token, new_status="audited",
        workspace_lease=workspace_lease, programs_root=programs_root,
    )


def mark_completed(
    db_path: Path, outbox_id: str, *, owner: str, fencing_token: int,
    workspace_lease: LeaseHandle | None = None, programs_root: Path | None = None,
) -> OutboxEntry:
    return _transition(
        db_path, outbox_id, owner=owner, fencing_token=fencing_token, new_status="completed",
        workspace_lease=workspace_lease, programs_root=programs_root,
    )


def mark_failed_retryable(
    db_path: Path, outbox_id: str, *, owner: str, fencing_token: int, reason: str, next_attempt_at: datetime
) -> OutboxEntry:
    return _transition(
        db_path, outbox_id, owner=owner, fencing_token=fencing_token, new_status="failed_retryable",
        extra_columns={"failure_reason": reason, "next_attempt_at": _wire(next_attempt_at)},
    )


def mark_failed_terminal(
    db_path: Path, outbox_id: str, *, owner: str, fencing_token: int, reason: str,
    workspace_lease: LeaseHandle | None = None, programs_root: Path | None = None,
) -> OutboxEntry:
    return _transition(
        db_path, outbox_id, owner=owner, fencing_token=fencing_token,
        new_status="failed_terminal", extra_columns={"failure_reason": reason},
        workspace_lease=workspace_lease, programs_root=programs_root,
    )


def mark_uncertain_remote_state(
    db_path: Path,
    outbox_id: str,
    *,
    owner: str,
    fencing_token: int,
    reason: str,
    reconciliation_receipt_json: str | None = None,
    workspace_lease: LeaseHandle | None = None,
    programs_root: Path | None = None,
) -> OutboxEntry:
    return _transition(
        db_path,
        outbox_id,
        owner=owner,
        fencing_token=fencing_token,
        new_status="uncertain_remote_state",
        extra_columns={"failure_reason": reason},
        reconciliation_receipt_json=reconciliation_receipt_json,
        workspace_lease=workspace_lease,
        programs_root=programs_root,
    )


def load_reconciliation_receipt(db_path: Path, outbox_id: str) -> str | None:
    if not db_path.exists():
        return None
    with open_program_db(db_path, durability="strict") as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'outbox_reconciliation_receipts'"
        ).fetchone()
        if exists is None:
            return None
        row = connection.execute(
            "SELECT receipt_json FROM outbox_reconciliation_receipts WHERE outbox_id = ?",
            (outbox_id,),
        ).fetchone()
    return None if row is None else str(row[0])


def mark_compensation_required(
    db_path: Path,
    outbox_id: str,
    *,
    owner: str,
    fencing_token: int,
    reason: str,
    compensation_operation: str | None = None,
    workspace_lease: LeaseHandle | None = None,
    programs_root: Path | None = None,
) -> OutboxEntry:
    return _transition(
        db_path,
        outbox_id,
        owner=owner,
        fencing_token=fencing_token,
        new_status="compensation_required",
        extra_columns={"failure_reason": reason},
        compensation_operation=compensation_operation,
        workspace_lease=workspace_lease,
        programs_root=programs_root,
    )


def mark_dead_letter(db_path: Path, outbox_id: str, *, owner: str, fencing_token: int, reason: str) -> OutboxEntry:
    return _transition(db_path, outbox_id, owner=owner, fencing_token=fencing_token, new_status="dead_letter", extra_columns={"failure_reason": reason})


def mark_manually_resolved(db_path: Path, outbox_id: str, *, resolved_by: str, reason: str) -> OutboxEntry:
    """Operator close-out from any true-terminal state. Does not require a
    live lease — by the time an operator intervenes, no automated owner is
    expected to still hold one."""
    now = _now()
    with open_program_db(db_path, durability="strict") as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT status, domain, correlation_id, payload_json, lease_fencing_token, "
            "attempt_count FROM outbox WHERE outbox_id = ?",
            (outbox_id,),
        ).fetchone()
        if row is None:
            raise OutboxError(f"unknown outbox row {outbox_id!r}")
        (
            current_status,
            domain,
            correlation_id,
            payload_json,
            fencing_token,
            attempt_count,
        ) = row
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
        recorded_at = str(_wire(now))
        receipt_json = _state_receipt(
            outbox_id=outbox_id,
            domain=domain,
            correlation_id=correlation_id,
            payload_json=payload_json,
            terminal_status="manually_resolved",
            owner=resolved_by,
            fencing_token=int(fencing_token),
            attempt_count=int(attempt_count),
            reason=reason,
            recorded_at=recorded_at,
        )
        _append_state_receipt(
            connection,
            outbox_id,
            "manually_resolved",
            recorded_at,
            receipt_json,
        )
    entry = load_entry(db_path, outbox_id)
    assert entry is not None
    return entry


_EFFECTS_SQL = """
CREATE TABLE IF NOT EXISTS outbox_effects (
    effect_id TEXT PRIMARY KEY,
    outbox_id TEXT NOT NULL
)
"""


def load_effect_outbox_id(db_path: Path, effect_id: str) -> str | None:
    if not db_path.exists():
        return None
    with open_program_db(db_path, read_only=True) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'outbox_effects'"
        ).fetchone()
        if table is None:
            return None
        row = connection.execute("SELECT outbox_id FROM outbox_effects WHERE effect_id = ?", (effect_id,)).fetchone()
    return None if row is None else row[0]


def has_effect_binding(db_path: Path, outbox_id: str) -> bool:
    if not db_path.exists():
        return False
    with open_program_db(db_path, read_only=True) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'outbox_effects'"
        ).fetchone()
        if table is None:
            return False
        return connection.execute(
            "SELECT 1 FROM outbox_effects WHERE outbox_id = ? LIMIT 1",
            (outbox_id,),
        ).fetchone() is not None


def bind_effect(db_path: Path, effect_id: str, outbox_id: str) -> str:
    """First binding wins; returns the outbox row that owns ``effect_id``."""
    with open_program_db(db_path, durability="strict") as connection:
        connection.execute(_EFFECTS_SQL)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT OR IGNORE INTO outbox_effects (effect_id, outbox_id) VALUES (?, ?)", (effect_id, outbox_id))
        (bound,) = connection.execute("SELECT outbox_id FROM outbox_effects WHERE effect_id = ?", (effect_id,)).fetchone()
    return bound


def enqueue_with_effect(
    db_path: Path,
    outbox_id: str,
    *,
    domain: str,
    correlation_id: str,
    payload_json: str,
    effect_id: str,
) -> tuple[OutboxEntry, bool]:
    """Atomically enqueue or recover the first outbox row bound to ``effect_id``."""
    now = _now()
    columns = (
        "outbox_id, domain, correlation_id, payload_json, status, lease_owner, "
        "lease_fencing_token, lease_expires_at, attempt_count, next_attempt_at, "
        "remote_request_hash, remote_response_hash, failure_reason, created_at, updated_at"
    )
    with open_program_db(db_path, durability="strict") as connection:
        connection.executescript(_SCHEMA_SQL)
        connection.executescript(_EFFECTS_SQL)
        connection.execute("BEGIN IMMEDIATE")
        bound_row = connection.execute(
            "SELECT outbox_id FROM outbox_effects WHERE effect_id = ?", (effect_id,),
        ).fetchone()
        if bound_row is not None:
            row = connection.execute(
                f"SELECT {columns} FROM outbox WHERE outbox_id = ?", (bound_row[0],),
            ).fetchone()
            if row is None:
                raise OutboxError("Effect id is bound to a missing outbox row")
            return _entry_from_row(row), False

        row = connection.execute(
            f"SELECT {columns} FROM outbox WHERE outbox_id = ?", (outbox_id,),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO outbox (
                    outbox_id, domain, correlation_id, payload_json, status,
                    lease_fencing_token, attempt_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', 0, 0, ?, ?)
                """,
                (outbox_id, domain, correlation_id, payload_json, _wire(now), _wire(now)),
            )
            created = True
        else:
            existing = _entry_from_row(row)
            if (
                existing.domain != domain
                or existing.correlation_id != correlation_id
                or existing.payload_json != payload_json
            ):
                raise OutboxError("Existing outbox key has different domain, correlation or payload")
            created = False
        connection.execute(
            "INSERT INTO outbox_effects (effect_id, outbox_id) VALUES (?, ?)",
            (effect_id, outbox_id),
        )
        row = connection.execute(
            f"SELECT {columns} FROM outbox WHERE outbox_id = ?", (outbox_id,),
        ).fetchone()
        assert row is not None
    return _entry_from_row(row), created


def _reconcile_uncertain(
    db_path: Path,
    outbox_id: str,
    *,
    set_sql: str,
    params: tuple,
    receipt_json: str | None,
    state_receipt: dict[str, object] | None = None,
    expected_entry: OutboxEntry,
) -> OutboxEntry:
    with open_program_db(db_path, durability="strict") as connection:
        connection.execute(_SUCCESS_RECEIPTS_SQL)
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT outbox_id, domain, correlation_id, payload_json, status, lease_owner, "
            "lease_fencing_token, lease_expires_at, attempt_count, next_attempt_at, "
            "remote_request_hash, remote_response_hash, failure_reason, created_at, updated_at "
            "FROM outbox WHERE outbox_id = ?",
            (outbox_id,),
        ).fetchone()
        if row is None:
            raise OutboxError(f"unknown outbox row {outbox_id!r}")
        current = _entry_from_row(tuple(row))
        if current != expected_entry:
            raise OutboxError(
                f"outbox row {outbox_id!r} changed during reconciliation"
            )
        if current.status != "uncertain_remote_state":
            raise OutboxError(f"outbox row {outbox_id!r} is {current.status!r}; marker reconciliation needs uncertain_remote_state")
        connection.execute(f"UPDATE outbox SET {set_sql}, updated_at = ? WHERE outbox_id = ?", (*params, _wire(_now()), outbox_id))
        if receipt_json is not None:
            connection.execute("INSERT INTO outbox_success_receipts (outbox_id, receipt_json) VALUES (?, ?)", (outbox_id, receipt_json))
        else:
            connection.execute(_RECONCILIATION_RECEIPTS_SQL)
            connection.execute(
                "DELETE FROM outbox_reconciliation_receipts WHERE outbox_id = ?",
                (outbox_id,),
            )
        if state_receipt is not None:
            state_receipt_json = json.dumps(
                state_receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            )
            _append_state_receipt(
                connection,
                outbox_id,
                str(state_receipt.get("terminal_status") or ""),
                str(state_receipt.get("recorded_at") or _wire(_now())),
                state_receipt_json,
            )
    entry = load_entry(db_path, outbox_id)
    assert entry is not None
    return entry


def mark_reconciled_confirmed(
    db_path: Path, outbox_id: str, *, reconciled_by: str, receipt_json: str,
    expected_entry: OutboxEntry,
) -> OutboxEntry:
    """uncertain_remote_state -> completed, only after a marker lookup found the remote effect."""
    return _reconcile_uncertain(
        db_path, outbox_id, set_sql="status = 'completed', lease_owner = ?, failure_reason = NULL",
        params=(reconciled_by,), receipt_json=receipt_json,
        expected_entry=expected_entry,
        state_receipt={
            **json.loads(receipt_json),
            "terminal_status": "completed",
            "reconciled_by": reconciled_by,
        },
    )


def mark_reconciled_absent(
    db_path: Path,
    outbox_id: str,
    *,
    reconciled_by: str,
    reason: str,
    state_receipt: dict[str, object] | None = None,
    expected_entry: OutboxEntry,
) -> OutboxEntry:
    """uncertain_remote_state -> pending, only after a marker lookup verified the remote effect is absent."""
    return _reconcile_uncertain(
        db_path, outbox_id,
        set_sql="status = 'pending', lease_owner = NULL, lease_expires_at = NULL, next_attempt_at = NULL, failure_reason = ?",
        params=(f"{reason} (by {reconciled_by})",), receipt_json=None,
        expected_entry=expected_entry,
        state_receipt=state_receipt or {
            "schema_version": "1",
            "outbox_id": outbox_id,
            "terminal_status": "pending",
            "reconciled_by": reconciled_by,
            "disposition": "absent",
            "reason": reason,
            "recorded_at": _wire(_now()),
        },
    )
