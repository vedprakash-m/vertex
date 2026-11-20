from __future__ import annotations

import json
import math
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.ai.client import BudgetExceeded
from src.core._db import open_program_db_with_retry as open_program_db
from src.core.edition_resolver import get_program_output_dir, PROGRAMS_ROOT
from src.core.exceptions import StateError


_MAX_SQLITE_INTEGER = 2**63 - 1


@dataclass(frozen=True, slots=True)
class CostRunState:
    edition: str
    run_id: str
    budget_usd: float
    spent_usd: float
    reserved_usd: float
    ai_calls: int
    started_at: datetime
    updated_at: datetime

    @property
    def within_budget(self) -> bool:
        return self.spent_usd + self.reserved_usd <= self.budget_usd


@dataclass(frozen=True, slots=True)
class CostReservation:
    """One ledger-identity-bound pre-dispatch reservation, settled at most once."""

    reservation_id: int
    estimated_cost_usd: float
    state: CostRunState
    _ledger_path: Path | None = field(default=None, repr=False)
    _ledger_id: str | None = field(default=None, repr=False)
    chain_id: int | None = None


def _enforce_run_budget(state: CostRunState, estimated_cost_usd: float, budget_usd: float) -> None:
    if state.spent_usd + state.reserved_usd + estimated_cost_usd > budget_usd:
        raise BudgetExceeded(
            f"Spent/reserved ${state.spent_usd + state.reserved_usd:.3f} of ${budget_usd:.2f}; "
            f"next AI spend ${estimated_cost_usd:.3f} would exceed the run ceiling."
        )


def _ensure_chain_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS ai_cost_chains (chain_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "edition TEXT NOT NULL, run_id TEXT NOT NULL, reserved_usd REAL NOT NULL, status TEXT NOT NULL, "
        "created_at TEXT NOT NULL, released_at TEXT)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS ai_cost_chain_draws ("
        "reservation_id INTEGER PRIMARY KEY REFERENCES ai_cost_reservations(reservation_id), "
        "chain_id INTEGER NOT NULL REFERENCES ai_cost_chains(chain_id))"
    )


class CostGuard:
    """Persists per-edition AI spend by run id under the output tree."""

    def __init__(
        self,
        *,
        edition: str,
        run_id: str,
        budget_usd: float,
        programs_root: Path | None = None,
    ) -> None:
        self._edition = edition
        self._run_id = run_id
        self._budget_usd = _validate_live_amount(budget_usd, field_name="budget_usd")
        self._programs_root = programs_root if programs_root is not None else PROGRAMS_ROOT

    @property
    def state_path(self) -> Path:
        return _cost_guard_path(self._edition, programs_root=self._programs_root)

    @property
    def ledger_path(self) -> Path:
        return _cost_guard_ledger_path(self._edition, programs_root=self._programs_root)

    def effective_budget_usd(self, state: CostRunState) -> float:
        return min(state.budget_usd, self._budget_usd)

    def current_state(self) -> CostRunState:
        sqlite_state = _load_sqlite_run_state(
            self.ledger_path,
            edition=self._edition,
            run_id=self._run_id,
        )
        if sqlite_state is not None:
            return sqlite_state
        payload = _load_payload(self.state_path, edition=self._edition)
        runs = payload.setdefault("runs", {})
        if not isinstance(runs, dict):
            raise StateError(f"Invalid cost guard format in {self.state_path}")
        run_payload = runs.get(self._run_id)
        if run_payload is None:
            return _new_state(self._edition, self._run_id, self._budget_usd)
        if not isinstance(run_payload, dict):
            raise StateError(f"Invalid cost guard run payload for {self._run_id} in {self.state_path}")
        return _state_from_payload(self._edition, self._run_id, self._budget_usd, run_payload)

    def check(self, estimated_cost_usd: float = 0.0) -> None:
        estimated_cost_usd = _validate_live_amount(estimated_cost_usd, field_name="estimated_cost_usd")
        current = self.current_state()
        _enforce_run_budget(current, estimated_cost_usd, self.effective_budget_usd(current))

    def reserve(self, estimated_cost_usd: float) -> CostRunState:
        """Durably reserve worst-case spend before one remote dispatch.

        A reservation is intentionally retained after an uncertain provider
        outcome.  That is conservative: it may block later work, but can never
        understate spend.  New provider paths use :meth:`reserve_call` for an
        individually settled reservation; this compatibility method keeps its
        state-only return shape for existing callers.
        """
        return self.reserve_call(estimated_cost_usd).state

    def reserve_call(
        self,
        estimated_cost_usd: float,
        *,
        chain_total_usd: float | None = None,
        chain: CostReservation | None = None,
    ) -> CostReservation:
        """Atomically reserve one remote call before it can be dispatched.

        SQLite serializes the budget decision and reservation insert under an
        immediate transaction.  A second concurrent caller therefore sees the
        first reservation before it can dispatch; a fallback attempt gets a
        separate reservation and cannot borrow the failed attempt's budget.

        ``chain_total_usd`` makes this first attempt also hold a retry/fallback
        chain's worst case: the run ceiling admits the whole total, the attempt
        row takes its estimate and ``ai_cost_chains`` keeps the remainder.
        ``chain=`` (a reservation returned that way) draws a later attempt from
        that remainder, and :meth:`release_chain` frees only what is undrawn.
        """
        estimated_cost_usd = _validate_live_amount(estimated_cost_usd, field_name="estimated_cost_usd")
        if chain_total_usd is not None:
            chain_total_usd = _validate_live_amount(chain_total_usd, field_name="chain_total_usd")
            if estimated_cost_usd > chain_total_usd + 1e-12:
                raise StateError("Attempt estimate exceeds its cost chain reservation")
        chain_id = chain.chain_id if chain is not None else None
        fallback_state = self.current_state()
        ledger_path = self.ledger_path.resolve()
        if chain is not None and (chain_id is None or chain._ledger_path != ledger_path):
            raise StateError("Cost chain reservation is unknown or belongs to a different ledger")
        try:
            with open_program_db(ledger_path, durability="strict") as connection:
                _ensure_sqlite_schema(connection)
                connection.execute("BEGIN IMMEDIATE")
                ledger_id = _ensure_ledger_identity(connection)
                row = connection.execute(
                    "SELECT edition, run_id, budget_usd, reserved_usd, spent_usd, ai_calls, started_at, updated_at "
                    "FROM ai_cost_runs WHERE edition = ? AND run_id = ?",
                    (self._edition, self._run_id),
                ).fetchone()
                current = _state_from_sqlite_row(row) if row is not None else fallback_state
                held_usd = estimated_cost_usd if chain_total_usd is None else chain_total_usd
                if chain is not None:
                    _ensure_chain_schema(connection)
                    chain_row = connection.execute(
                        "SELECT reserved_usd, status FROM ai_cost_chains WHERE chain_id = ? AND edition = ? AND run_id = ?",
                        (chain_id, self._edition, self._run_id),
                    ).fetchone()
                    if row is None or chain_row is None or chain_row["status"] != "reserved":
                        raise StateError("Cost chain reservation is unknown or already released")
                    _verify_ledger_identity(connection, chain)
                    remaining = _coerce_state_float(chain_row["reserved_usd"], field_name="reserved_usd")
                    if estimated_cost_usd > remaining + 1e-12:
                        raise StateError("Attempt estimate exceeds the cost chain reservation's remainder")
                    # The run already holds this amount inside the chain row.
                    held_usd = 0.0
                else:
                    _enforce_run_budget(current, held_usd, self.effective_budget_usd(current))
                _projection_payload_for_write(self.state_path, edition=self._edition)
                updated = _replace_state(current, reserved_usd=current.reserved_usd + held_usd)
                _upsert_sqlite_state(connection, updated)
                created_at = updated.updated_at.isoformat()
                if chain is not None:
                    connection.execute(
                        "UPDATE ai_cost_chains SET reserved_usd = ? WHERE chain_id = ?",
                        (max(0.0, remaining - estimated_cost_usd), chain_id),
                    )
                elif chain_total_usd is not None:
                    _ensure_chain_schema(connection)
                    chain_id = connection.execute(
                        "INSERT INTO ai_cost_chains (edition, run_id, reserved_usd, status, created_at) "
                        "VALUES (?, ?, ?, 'reserved', ?)",
                        (self._edition, self._run_id, max(0.0, chain_total_usd - estimated_cost_usd), created_at),
                    ).lastrowid
                cursor = connection.execute(
                    "INSERT INTO ai_cost_reservations (edition, run_id, reserved_usd, status, created_at) "
                    "VALUES (?, ?, ?, 'reserved', ?)",
                    (self._edition, self._run_id, estimated_cost_usd, created_at),
                )
                reservation_id = cursor.lastrowid
                assert reservation_id is not None
                if chain_id is not None:
                    connection.execute(
                        "INSERT INTO ai_cost_chain_draws (reservation_id, chain_id) VALUES (?, ?)",
                        (reservation_id, chain_id),
                    )
        except sqlite3.DatabaseError as error:
            raise StateError(f"Unable to reserve cost guard SQLite ledger at {self.ledger_path}") from error
        reservation = CostReservation(
            reservation_id=reservation_id,
            estimated_cost_usd=estimated_cost_usd,
            state=updated,
            _ledger_path=ledger_path,
            _ledger_id=ledger_id,
            chain_id=chain_id,
        )
        try:
            self._persist_projection(updated)
        except (StateError, OSError, UnicodeError):
            try:
                self._finish_call(
                    reservation, actual_cost_usd=0.0, ai_calls=0,
                    undispatched=True, refresh_projection=False,
                )
                if chain_total_usd is not None:
                    self.release_chain(reservation, refresh_projection=False)
            except (StateError, OSError, UnicodeError) as error:
                raise StateError(
                    f"Unable to release undispatched cost reservation ({type(error).__name__}); "
                    "reconcile before retrying."
                ) from None
            raise
        return reservation

    def release_chain(self, chain: CostReservation, *, refresh_projection: bool = True) -> CostRunState:
        """Release a chain's undrawn remainder; drawn attempts keep their own settle/retain outcome."""
        ledger_path = self.ledger_path.resolve()
        if chain.chain_id is None or chain._ledger_path != ledger_path:
            raise StateError("Cost chain reservation is unknown or belongs to a different ledger")
        try:
            with open_program_db(ledger_path, durability="strict") as connection:
                _ensure_sqlite_schema(connection)
                _ensure_chain_schema(connection)
                connection.execute("BEGIN IMMEDIATE")
                _verify_ledger_identity(connection, chain)
                row = connection.execute(
                    "SELECT reserved_usd, status FROM ai_cost_chains WHERE chain_id = ? AND edition = ? AND run_id = ?",
                    (chain.chain_id, self._edition, self._run_id),
                ).fetchone()
                if row is None or row["status"] != "reserved":
                    raise StateError("Cost chain reservation is unknown or already released")
                remaining = _coerce_state_float(row["reserved_usd"], field_name="reserved_usd")
                state_row = connection.execute(
                    "SELECT edition, run_id, budget_usd, reserved_usd, spent_usd, ai_calls, started_at, updated_at "
                    "FROM ai_cost_runs WHERE edition = ? AND run_id = ?",
                    (self._edition, self._run_id),
                ).fetchone()
                if state_row is None:
                    raise StateError("Cost chain reservation has no run state")
                current = _state_from_sqlite_row(state_row)
                if remaining > current.reserved_usd + 1e-12:
                    raise StateError("Cost chain reservation exceeds the run's reserved balance")
                reserved_usd = current.reserved_usd - remaining
                outstanding = math.fsum(
                    _coerce_state_float(open_row["reserved_usd"], field_name="reserved_usd")
                    for open_row in connection.execute(
                        "SELECT reserved_usd FROM ai_cost_reservations WHERE edition = ? AND run_id = ? AND status = 'reserved' "
                        "UNION ALL SELECT reserved_usd FROM ai_cost_chains "
                        "WHERE edition = ? AND run_id = ? AND status = 'reserved' AND chain_id != ?",
                        (self._edition, self._run_id, self._edition, self._run_id, chain.chain_id),
                    )
                )
                # Snap draw/settle float drift back to the exact sum of the rows still held.
                if abs(reserved_usd - outstanding) < 1e-9:
                    reserved_usd = outstanding
                updated = _replace_state(current, reserved_usd=reserved_usd if reserved_usd > 1e-12 else 0.0)
                _upsert_sqlite_state(connection, updated)
                connection.execute(
                    "UPDATE ai_cost_chains SET status = 'released', released_at = ? WHERE chain_id = ?",
                    (updated.updated_at.isoformat(), chain.chain_id),
                )
        except sqlite3.DatabaseError as error:
            raise StateError(f"Unable to release cost chain in SQLite ledger at {self.ledger_path}") from error
        if refresh_projection:
            self._persist_projection(updated)
        return updated

    def settle_reservation(self, reserved_cost_usd: float, actual_cost_usd: float, *, ai_calls: int = 1) -> CostRunState:
        """Convert a known reservation into actual spend after a response."""
        reserved_cost_usd = _validate_live_amount(reserved_cost_usd, field_name="reserved_cost_usd")
        actual_cost_usd = _validate_live_amount(actual_cost_usd, field_name="actual_cost_usd")
        ai_calls = _validate_live_call_count(ai_calls)
        reservation = self._find_legacy_reservation(reserved_cost_usd)
        if reservation is not None:
            return self._finish_call(
                reservation, actual_cost_usd=actual_cost_usd, ai_calls=ai_calls,
                require_unique_legacy_match=True,
            )
        return self._record_spend(actual_cost_usd, ai_calls=ai_calls, reserved_cost_usd=reserved_cost_usd)

    def _find_legacy_reservation(self, reserved_cost_usd: float) -> CostReservation | None:
        """Bridge state-only callers onto the new per-call ledger when possible."""
        ledger_path = self.ledger_path.resolve()
        if not ledger_path.exists():
            return None
        try:
            with open_program_db(ledger_path, durability="strict") as connection:
                _ensure_sqlite_schema(connection)
                connection.execute("BEGIN IMMEDIATE")
                ledger_id = _ensure_ledger_identity(connection)
                row = connection.execute(
                    "SELECT reservation_id FROM ai_cost_reservations WHERE edition = ? AND run_id = ? "
                    "AND status = 'reserved' AND ABS(reserved_usd - ?) < 0.000000000001 ORDER BY reservation_id LIMIT 1",
                    (self._edition, self._run_id, reserved_cost_usd),
                ).fetchone()
        except sqlite3.OperationalError as error:
            if "no such table" in str(error).lower():
                return None
            raise StateError(f"Invalid cost guard SQLite ledger in {self.ledger_path}") from error
        except sqlite3.DatabaseError as error:
            raise StateError(f"Invalid cost guard SQLite ledger in {self.ledger_path}") from error
        if row is None:
            return None
        return CostReservation(
            reservation_id=int(row["reservation_id"]),
            estimated_cost_usd=reserved_cost_usd,
            state=self.current_state(),
            _ledger_path=ledger_path,
            _ledger_id=ledger_id,
        )

    def load_reservation(self, reservation_id: int) -> CostReservation:
        """Reconstruct one reserved attempt handle for a new process.

        This is a read-only reconciliation boundary. Settled rows are not
        reloaded, and an uncertain provider attempt remains reserved until a
        caller explicitly reconciles actual cost with :meth:`reconcile_reservation`.
        """
        return self._load_reservation(reservation_id, require_reserved=True)

    def _load_reservation(
        self,
        reservation_id: int,
        *,
        require_reserved: bool,
    ) -> CostReservation:
        reservation_id = _validate_reservation_id(reservation_id)
        ledger_path = self.ledger_path.resolve()
        try:
            with open_program_db(ledger_path, read_only=True) as connection:
                ledger_id = _read_ledger_identity(connection)
                row = connection.execute(
                    "SELECT reserved_usd, status FROM ai_cost_reservations "
                    "WHERE reservation_id = ? AND edition = ? AND run_id = ?",
                    (reservation_id, self._edition, self._run_id),
                ).fetchone()
                if row is None:
                    raise StateError("Cost reservation is not associated with this run")
                if require_reserved and row["status"] != "reserved":
                    raise StateError("Cost reservation is not reserved for this run")
                draw_table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'ai_cost_chain_draws'"
                ).fetchone()
                chain_row = (
                    connection.execute(
                        "SELECT chain_id FROM ai_cost_chain_draws WHERE reservation_id = ?",
                        (reservation_id,),
                    ).fetchone()
                    if draw_table is not None
                    else None
                )
                state_row = connection.execute(
                    "SELECT edition, run_id, budget_usd, reserved_usd, spent_usd, ai_calls, "
                    "started_at, updated_at FROM ai_cost_runs "
                    "WHERE edition = ? AND run_id = ?",
                    (self._edition, self._run_id),
                ).fetchone()
                if state_row is None:
                    raise StateError("Cost reservation has no run state")
                return CostReservation(
                    reservation_id=reservation_id,
                    estimated_cost_usd=_coerce_state_float(
                        row["reserved_usd"],
                        field_name="reserved_usd",
                    ),
                    state=_state_from_sqlite_row(state_row),
                    _ledger_path=ledger_path,
                    _ledger_id=ledger_id,
                    chain_id=(
                        _coerce_state_int(
                            chain_row["chain_id"],
                            field_name="chain_id",
                        )
                        if chain_row is not None
                        else None
                    ),
                )
        except sqlite3.OperationalError as error:
            raise StateError("Cost reservation ledger is unavailable or has no durable identity") from error
        except sqlite3.DatabaseError as error:
            raise StateError("Cost reservation ledger is invalid") from error

    def reconcile_reservation(
        self,
        reservation_id: int,
        *,
        actual_cost_usd: float,
        ai_calls: int = 1,
    ) -> CostRunState:
        """Settle one retained uncertain attempt using a reservation ID from telemetry."""
        reservation = self.load_reservation(reservation_id)
        return self.settle_call(
            reservation,
            actual_cost_usd,
            ai_calls=ai_calls,
        )

    def reconcile_chain_remainder(self, reservation_id: int) -> CostRunState:
        """Release only the undrawn remainder for a retained reservation's chain.

        The reservation may already be settled. Drawn attempt rows remain
        reserved until each provider outcome is reconciled explicitly.
        """
        chain = self._load_reservation(reservation_id, require_reserved=False)
        if chain.chain_id is None:
            raise StateError("Cost reservation is not associated with a cost chain")
        return self.release_chain(chain)

    def settle_call(self, reservation: CostReservation, actual_cost_usd: float, *, ai_calls: int = 1) -> CostRunState:
        """Settle exactly the call reservation that preceded a response."""
        actual_cost_usd = _validate_live_amount(actual_cost_usd, field_name="actual_cost_usd")
        ai_calls = _validate_live_call_count(ai_calls)
        return self._finish_call(reservation, actual_cost_usd=actual_cost_usd, ai_calls=ai_calls)

    def _release_undispatched_call(self, reservation: CostReservation) -> CostRunState:
        return self._finish_call(reservation, actual_cost_usd=0.0, ai_calls=0, undispatched=True)

    def _finish_call(
        self,
        reservation: CostReservation,
        *,
        actual_cost_usd: float,
        ai_calls: int,
        undispatched: bool = False,
        refresh_projection: bool = True,
        require_unique_legacy_match: bool = False,
    ) -> CostRunState:
        ledger_path = self.ledger_path.resolve()
        if reservation._ledger_path is None:
            raise StateError("Cost reservation is not bound to a ledger")
        if reservation._ledger_id is None:
            raise StateError("Cost reservation is not bound to a ledger identity")
        if reservation._ledger_path != ledger_path:
            raise StateError("Cost reservation belongs to a different ledger")
        try:
            with open_program_db(ledger_path, durability="strict") as connection:
                _ensure_sqlite_schema(connection)
                connection.execute("BEGIN IMMEDIATE")
                _verify_ledger_identity(connection, reservation)
                reservation_row = connection.execute(
                    "SELECT reserved_usd, status FROM ai_cost_reservations WHERE reservation_id = ? AND edition = ? AND run_id = ?",
                    (reservation.reservation_id, self._edition, self._run_id),
                ).fetchone()
                if reservation_row is None:
                    raise StateError("Unknown cost reservation")
                if reservation_row["status"] != "reserved":
                    raise StateError("Cost reservation has already been settled")
                if require_unique_legacy_match:
                    matches = []
                    for active in connection.execute(
                        "SELECT reservation_id, reserved_usd, "
                        "ABS(reserved_usd - ?) < 0.000000000001 AS amount_matches "
                        "FROM ai_cost_reservations WHERE edition = ? AND run_id = ? AND status = 'reserved'",
                        (reservation.estimated_cost_usd, self._edition, self._run_id),
                    ):
                        _coerce_state_float(active["reserved_usd"], field_name="reserved_usd")
                        if active["amount_matches"]:
                            matches.append(active["reservation_id"])
                            if len(matches) == 2:
                                break
                    if len(matches) != 1 or matches[0] != reservation.reservation_id:
                        raise StateError("Legacy cost reservation match is ambiguous or changed; reconcile before retrying.")
                reserved_cost_usd = _coerce_state_float(reservation_row["reserved_usd"], field_name="reserved_usd")
                state_row = connection.execute(
                    "SELECT edition, run_id, budget_usd, reserved_usd, spent_usd, ai_calls, started_at, updated_at "
                    "FROM ai_cost_runs WHERE edition = ? AND run_id = ?",
                    (self._edition, self._run_id),
                ).fetchone()
                if state_row is None:
                    raise StateError("Cost reservation has no run state")
                current = _state_from_sqlite_row(state_row)
                if reserved_cost_usd > current.reserved_usd + 1e-12:
                    raise StateError("Cost reservation exceeds the run's reserved balance")
                updated = _replace_state(
                    current,
                    spent_usd=_coerce_state_float(current.spent_usd + actual_cost_usd, field_name="spent_usd"),
                    reserved_usd=max(0.0, current.reserved_usd - reserved_cost_usd),
                    ai_calls=_coerce_state_int(current.ai_calls + ai_calls, field_name="ai_calls"),
                )
                _upsert_sqlite_state(connection, updated)
                connection.execute(
                    "UPDATE ai_cost_reservations SET status = 'settled', actual_usd = ?, settled_at = ? WHERE reservation_id = ?",
                    (actual_cost_usd, updated.updated_at.isoformat(), reservation.reservation_id),
                )
                if undispatched:
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS ai_cost_non_dispatch_releases ("
                        "reservation_id INTEGER PRIMARY KEY REFERENCES ai_cost_reservations(reservation_id), "
                        "released_at TEXT NOT NULL)"
                    )
                    connection.execute(
                        "INSERT INTO ai_cost_non_dispatch_releases (reservation_id, released_at) VALUES (?, ?)",
                        (reservation.reservation_id, updated.updated_at.isoformat()),
                    )
        except sqlite3.DatabaseError as error:
            raise StateError(f"Unable to settle cost guard SQLite ledger at {self.ledger_path}") from error
        if refresh_projection:
            self._persist_projection(updated)
        return updated

    def record(self, cost_usd: float, *, ai_calls: int = 1) -> CostRunState:
        ai_calls = _validate_live_call_count(ai_calls)
        self.check(cost_usd)
        cost_usd = _validate_live_amount(cost_usd, field_name="cost_usd")
        return self._record_spend(cost_usd, ai_calls=ai_calls, check_budget=True)

    def record_actual(self, cost_usd: float, *, ai_calls: int = 1) -> CostRunState:
        cost_usd = _validate_live_amount(cost_usd, field_name="cost_usd")
        ai_calls = _validate_live_call_count(ai_calls)
        return self._record_spend(cost_usd, ai_calls=ai_calls)

    def _record_spend(
        self,
        cost_usd: float,
        *,
        ai_calls: int,
        reserved_cost_usd: float | None = None,
        check_budget: bool = False,
    ) -> CostRunState:
        fallback_state = self.current_state()
        try:
            with open_program_db(self.ledger_path, durability="strict") as connection:
                _ensure_sqlite_schema(connection)
                connection.execute("BEGIN IMMEDIATE")
                _ensure_ledger_identity(connection)
                row = connection.execute(
                    "SELECT edition, run_id, budget_usd, reserved_usd, spent_usd, ai_calls, started_at, updated_at "
                    "FROM ai_cost_runs WHERE edition = ? AND run_id = ?",
                    (self._edition, self._run_id),
                ).fetchone()
                current = _state_from_sqlite_row(row) if row is not None else fallback_state
                if check_budget:
                    _enforce_run_budget(current, cost_usd, self.effective_budget_usd(current))
                remaining_reserved = current.reserved_usd
                if reserved_cost_usd is not None:
                    tracked_reserved = 0.0
                    for reservation_row in connection.execute(
                        "SELECT reserved_usd FROM ai_cost_reservations "
                        "WHERE edition = ? AND run_id = ? AND status = 'reserved'",
                        (self._edition, self._run_id),
                    ):
                        tracked_reserved = _coerce_state_float(
                            tracked_reserved + _coerce_state_float(reservation_row["reserved_usd"], field_name="reserved_usd"),
                            field_name="reserved_usd",
                        )
                    if reserved_cost_usd > current.reserved_usd - tracked_reserved + 1e-12:
                        raise StateError("Cannot settle a cost reservation that is not present in the run state")
                    remaining_reserved = max(0.0, current.reserved_usd - reserved_cost_usd)
                updated = _replace_state(
                    current,
                    spent_usd=_coerce_state_float(current.spent_usd + cost_usd, field_name="spent_usd"),
                    reserved_usd=remaining_reserved,
                    ai_calls=_coerce_state_int(current.ai_calls + ai_calls, field_name="ai_calls"),
                )
                _upsert_sqlite_state(connection, updated)
        except sqlite3.DatabaseError as error:
            raise StateError(f"Unable to persist cost guard SQLite ledger at {self.ledger_path}") from error
        self._persist_projection(updated)
        return updated

    def _persist_projection(self, state: CostRunState) -> None:
        try:
            with open_program_db(self.ledger_path, durability="strict") as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT edition, run_id, budget_usd, reserved_usd, spent_usd, ai_calls, started_at, updated_at "
                    "FROM ai_cost_runs WHERE edition = ? AND run_id = ?",
                    (state.edition, state.run_id),
                ).fetchone()
                if row is None:
                    raise StateError("Cannot project cost guard state that is not present in the ledger")
                payload = _projection_payload_for_write(self.state_path, edition=self._edition)
                runs = payload["runs"]
                runs[state.run_id] = _state_to_payload(_state_from_sqlite_row(row))
                _write_atomic_json(self.state_path, payload)
        except sqlite3.DatabaseError as error:
            raise StateError(f"Unable to project cost guard SQLite ledger at {self.ledger_path}") from error


def load_run_states(
    edition: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[CostRunState, ...]:
    sqlite_states = _load_sqlite_run_states(_cost_guard_ledger_path(edition, programs_root=programs_root), edition=edition)
    if sqlite_states is not None:
        return sqlite_states

    path = _cost_guard_path(edition, programs_root=programs_root)
    payload = _load_payload(path, edition=edition)
    runs = payload.get("runs")
    if not isinstance(runs, dict):
        raise StateError(f"Invalid cost guard format in {path}")
    states: list[CostRunState] = []
    for run_id, run_payload in runs.items():
        if not isinstance(run_payload, dict):
            raise StateError(f"Invalid cost guard run payload for {run_id} in {path}")
        states.append(_state_from_payload(edition, str(run_id), 0.0, run_payload))
    return tuple(sorted(states, key=lambda state: (state.updated_at, state.run_id), reverse=True))


def load_latest_run_state(
    edition: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> CostRunState | None:
    states = load_run_states(edition, programs_root=programs_root)
    return states[0] if states else None


def _cost_guard_path(edition: str, *, programs_root: Path) -> Path:
    return get_program_output_dir(edition, programs_root=programs_root) / "ai" / "cost_guard.json"


def _cost_guard_ledger_path(edition: str, *, programs_root: Path) -> Path:
    return get_program_output_dir(edition, programs_root=programs_root) / "ai" / "cost_guard.sqlite3"


def _load_payload(path: Path, *, edition: str) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": "1.0",
            "edition": edition,
            "runs": {},
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise StateError(f"Invalid cost guard JSON in {path}") from error
    if not isinstance(payload, dict):
        raise StateError(f"Invalid cost guard payload in {path}")
    stored_edition = _required_state_field(payload, field_name="edition", message=f"Cost guard payload must include edition in {path}")
    if stored_edition != edition:
        raise StateError(f"Cost guard {path} belongs to {stored_edition}, not {edition}")
    payload.setdefault("schema_version", "1.0")
    payload["runs"] = _required_state_field(payload, field_name="runs", message=f"Cost guard payload must include runs in {path}")
    return payload


def _projection_payload_for_write(path: Path, *, edition: str) -> dict[str, Any]:
    try:
        payload = _load_payload(path, edition=edition)
    except StateError:
        raise StateError("Unable to read cost guard projection; preserve it and reconcile before retrying.") from None
    if not isinstance(payload["runs"], dict):
        raise StateError("Unable to read cost guard projection; preserve it and reconcile before retrying.")
    return payload


def _new_state(edition: str, run_id: str, budget_usd: float) -> CostRunState:
    now = datetime.now(timezone.utc)
    return CostRunState(
        edition=edition,
        run_id=run_id,
        budget_usd=budget_usd,
        spent_usd=0.0,
        reserved_usd=0.0,
        ai_calls=0,
        started_at=now,
        updated_at=now,
    )


def _state_from_payload(edition: str, run_id: str, budget_usd: float, payload: dict[str, Any]) -> CostRunState:
    started_at = _parse_state_datetime(
        _required_state_field(payload, field_name="started_at", message="Cost guard payload must include started_at."),
        field_name="started_at",
    )
    updated_at = _parse_state_datetime(
        _required_state_field(payload, field_name="updated_at", message="Cost guard payload must include updated_at."),
        field_name="updated_at",
    )
    stored_budget = _coerce_state_float(
        _required_state_field(payload, field_name="budget_usd", message="Cost guard payload must include budget_usd."),
        field_name="budget_usd",
    )
    return CostRunState(
        edition=edition,
        run_id=run_id,
        budget_usd=stored_budget,
        spent_usd=_coerce_state_float(
            _required_state_field(payload, field_name="spent_usd", message="Cost guard payload must include spent_usd."),
            field_name="spent_usd",
        ),
        reserved_usd=_coerce_state_float(payload.get("reserved_usd", 0.0), field_name="reserved_usd"),
        ai_calls=_coerce_state_int(
            _required_state_field(payload, field_name="ai_calls", message="Cost guard payload must include ai_calls."),
            field_name="ai_calls",
        ),
        started_at=started_at or _raise_missing_state_datetime("started_at"),
        updated_at=updated_at or _raise_missing_state_datetime("updated_at"),
    )


def _required_state_field(payload: dict[str, Any], *, field_name: str, message: str) -> Any:
    if field_name not in payload:
        raise StateError(message)
    return payload.get(field_name)


def _raise_missing_state_datetime(field_name: str) -> datetime:
    raise StateError(f"Invalid cost guard {field_name} value: None")


def _validate_live_amount(value: float, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StateError(f"Invalid cost guard {field_name} value")
    return _coerce_state_float(value, field_name=field_name)


def _validate_live_call_count(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StateError("Invalid cost guard ai_calls value")
    return _coerce_state_int(value, field_name="ai_calls")


def _coerce_state_float(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise StateError(f"Invalid cost guard {field_name} value")
    try:
        amount = float(value)
    except (OverflowError, TypeError, ValueError):
        raise StateError(f"Invalid cost guard {field_name} value") from None
    if not math.isfinite(amount) or amount < 0:
        raise StateError(f"Invalid cost guard {field_name} value")
    return amount


def _coerce_state_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise StateError(f"Invalid cost guard {field_name} value")
    try:
        result = int(value)
    except (OverflowError, TypeError, ValueError):
        raise StateError(f"Invalid cost guard {field_name} value") from None
    if (isinstance(value, float) and not value.is_integer()) or not 0 <= result <= _MAX_SQLITE_INTEGER:
        raise StateError(f"Invalid cost guard {field_name} value")
    return result


def _state_to_payload(state: CostRunState) -> dict[str, Any]:
    return {
        "budget_usd": state.budget_usd,
        "spent_usd": state.spent_usd,
        "reserved_usd": state.reserved_usd,
        "ai_calls": state.ai_calls,
        "started_at": state.started_at.isoformat(),
        "updated_at": state.updated_at.isoformat(),
    }


def _load_sqlite_run_state(
    path: Path,
    *,
    edition: str,
    run_id: str,
) -> CostRunState | None:
    rows = _read_sqlite_run_rows(
        path,
        """
        SELECT edition, run_id, budget_usd, reserved_usd, spent_usd, ai_calls, started_at, updated_at
        FROM ai_cost_runs
        WHERE edition = ? AND run_id = ?
        LIMIT 1
        """,
        (edition, run_id),
    )
    if not rows:
        return None
    return _state_from_sqlite_row(rows[0])


def _load_sqlite_run_states(path: Path, *, edition: str) -> tuple[CostRunState, ...] | None:
    rows = _read_sqlite_run_rows(
        path,
        """
        SELECT edition, run_id, budget_usd, reserved_usd, spent_usd, ai_calls, started_at, updated_at
        FROM ai_cost_runs
        WHERE edition = ?
        ORDER BY updated_at DESC, run_id DESC
        """,
        (edition,),
    )
    if not rows:
        return None
    states = (_state_from_sqlite_row(row) for row in rows)
    return tuple(sorted(states, key=lambda state: (state.updated_at, state.run_id), reverse=True))


def _read_sqlite_run_rows(path: Path, query: str, parameters: tuple[str, ...]) -> list[sqlite3.Row] | None:
    if not path.exists():
        return None
    attempts = 0
    while True:
        try:
            with open_program_db(path, read_only=True) as connection:
                return connection.execute(query, parameters).fetchall()
        except sqlite3.OperationalError as error:
            # A newly created SQLite file can be visible before its schema exists.
            if str(error) != "no such table: ai_cost_runs" or attempts >= 7:
                raise StateError(f"Invalid cost guard SQLite ledger in {path}") from error
            time.sleep(0.02 * 2**attempts)
            attempts += 1
        except sqlite3.DatabaseError as error:
            raise StateError(f"Invalid cost guard SQLite ledger in {path}") from error


def _ensure_sqlite_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_cost_runs (
            edition TEXT NOT NULL,
            run_id TEXT NOT NULL,
            budget_usd REAL NOT NULL,
            reserved_usd REAL NOT NULL DEFAULT 0.0,
            spent_usd REAL NOT NULL DEFAULT 0.0,
            ai_calls INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (edition, run_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_cost_reservations (
            reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            edition TEXT NOT NULL,
            run_id TEXT NOT NULL,
            reserved_usd REAL NOT NULL,
            actual_usd REAL,
            status TEXT NOT NULL CHECK (status IN ('reserved', 'settled')),
            created_at TEXT NOT NULL,
            settled_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_cost_ledger_identity (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            ledger_id TEXT NOT NULL
        )
        """
    )


def _ensure_ledger_identity(connection: sqlite3.Connection) -> str:
    connection.execute(
        "INSERT OR IGNORE INTO ai_cost_ledger_identity (singleton, ledger_id) VALUES (1, ?)",
        (uuid.uuid4().hex,),
    )
    return _read_ledger_identity(connection)


def _read_ledger_identity(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        "SELECT ledger_id FROM ai_cost_ledger_identity WHERE singleton = 1"
    ).fetchone()
    raw = row["ledger_id"] if row is not None else None
    if not isinstance(raw, str) or len(raw) != 32:
        raise StateError("Cost guard ledger has no durable identity")
    try:
        parsed = uuid.UUID(hex=raw)
    except ValueError:
        raise StateError("Cost guard ledger has an invalid durable identity") from None
    if parsed.hex != raw:
        raise StateError("Cost guard ledger has an invalid durable identity")
    return raw


def _verify_ledger_identity(connection: sqlite3.Connection, reservation: CostReservation) -> None:
    if reservation._ledger_id is None or reservation._ledger_id != _read_ledger_identity(connection):
        raise StateError("Cost reservation belongs to a replaced ledger")


def _validate_reservation_id(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_SQLITE_INTEGER:
        raise StateError("Invalid cost guard reservation_id value")
    return value


def _upsert_sqlite_state(connection: sqlite3.Connection, state: CostRunState) -> None:
    connection.execute(
        """
        INSERT INTO ai_cost_runs (edition, run_id, budget_usd, reserved_usd, spent_usd, ai_calls, started_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(edition, run_id) DO UPDATE SET
            budget_usd = excluded.budget_usd,
            reserved_usd = excluded.reserved_usd,
            spent_usd = excluded.spent_usd,
            ai_calls = excluded.ai_calls,
            updated_at = excluded.updated_at
        """,
        (
            state.edition, state.run_id, state.budget_usd, state.reserved_usd,
            state.spent_usd, state.ai_calls, state.started_at.isoformat(), state.updated_at.isoformat(),
        ),
    )


def _state_from_sqlite_row(row: sqlite3.Row) -> CostRunState:
    return CostRunState(
        edition=str(row["edition"]),
        run_id=str(row["run_id"]),
        budget_usd=_coerce_state_float(row["budget_usd"], field_name="budget_usd"),
        spent_usd=_coerce_state_float(row["spent_usd"], field_name="spent_usd"),
        reserved_usd=_coerce_state_float(row["reserved_usd"], field_name="reserved_usd"),
        ai_calls=_coerce_state_int(row["ai_calls"], field_name="ai_calls"),
        started_at=_parse_state_datetime(row["started_at"], field_name="started_at") or _raise_missing_state_datetime("started_at"),
        updated_at=_parse_state_datetime(row["updated_at"], field_name="updated_at") or _raise_missing_state_datetime("updated_at"),
    )


def _replace_state(state: CostRunState, **changes: Any) -> CostRunState:
    return CostRunState(
        edition=state.edition,
        run_id=state.run_id,
        budget_usd=state.budget_usd,
        spent_usd=changes.get("spent_usd", state.spent_usd),
        reserved_usd=changes.get("reserved_usd", state.reserved_usd),
        ai_calls=changes.get("ai_calls", state.ai_calls),
        started_at=state.started_at,
        updated_at=datetime.now(timezone.utc),
    )


def _write_atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _parse_state_datetime(value: Any, *, field_name: str) -> datetime | None:
    if value is None:
        return None
    parsed = _parse_datetime(value)
    if parsed is None:
        raise StateError(f"Invalid cost guard {field_name} value") from None
    return parsed
