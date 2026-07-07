from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.ai.client import BudgetExceeded
from src.core._db import open_program_db
from src.core.edition_resolver import get_program_output_dir, PROGRAMS_ROOT
from src.core.exceptions import StateError


@dataclass(frozen=True, slots=True)
class CostRunState:
    edition: str
    run_id: str
    budget_usd: float
    spent_usd: float
    ai_calls: int
    started_at: datetime
    updated_at: datetime

    @property
    def within_budget(self) -> bool:
        return self.spent_usd <= self.budget_usd


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
        self._budget_usd = budget_usd
        self._programs_root = programs_root if programs_root is not None else PROGRAMS_ROOT

    @property
    def state_path(self) -> Path:
        return _cost_guard_path(self._edition, programs_root=self._programs_root)

    @property
    def ledger_path(self) -> Path:
        return _cost_guard_ledger_path(self._edition, programs_root=self._programs_root)

    def current_state(self) -> CostRunState:
        sqlite_state = _load_sqlite_run_state(
            self.ledger_path,
            edition=self._edition,
            run_id=self._run_id,
            budget_usd=self._budget_usd,
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
        state = self.current_state()
        if state.spent_usd + estimated_cost_usd > state.budget_usd:
            raise BudgetExceeded(
                f"Spent ${state.spent_usd:.3f} of ${state.budget_usd:.2f}; next AI spend ${estimated_cost_usd:.3f} would exceed the run ceiling."
            )

    def record(self, cost_usd: float, *, ai_calls: int = 1) -> CostRunState:
        self.check(cost_usd)
        return self.record_actual(cost_usd, ai_calls=ai_calls)

    def record_actual(self, cost_usd: float, *, ai_calls: int = 1) -> CostRunState:
        current = self.current_state()
        updated_at = datetime.now(timezone.utc)
        updated_state = CostRunState(
            edition=current.edition,
            run_id=current.run_id,
            budget_usd=current.budget_usd,
            spent_usd=current.spent_usd + cost_usd,
            ai_calls=current.ai_calls + ai_calls,
            started_at=current.started_at,
            updated_at=updated_at,
        )
        _write_sqlite_state(self.ledger_path, updated_state)

        payload = _projection_payload_for_write(self.state_path, edition=self._edition)
        runs = payload.setdefault("runs", {})
        if not isinstance(runs, dict):
            raise StateError(f"Invalid cost guard format in {self.state_path}")
        runs[self._run_id] = _state_to_payload(updated_state)
        _write_atomic_json(self.state_path, payload)
        return updated_state


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
        return _load_payload(path, edition=edition)
    except StateError:
        return {
            "schema_version": "1.0",
            "edition": edition,
            "runs": {},
        }


def _new_state(edition: str, run_id: str, budget_usd: float) -> CostRunState:
    now = datetime.now(timezone.utc)
    return CostRunState(
        edition=edition,
        run_id=run_id,
        budget_usd=budget_usd,
        spent_usd=0.0,
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


def _coerce_state_float(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise StateError(f"Invalid cost guard {field_name} value: {value!r}")
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise StateError(f"Invalid cost guard {field_name} value: {value!r}") from error


def _coerce_state_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise StateError(f"Invalid cost guard {field_name} value: {value!r}")
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise StateError(f"Invalid cost guard {field_name} value: {value!r}") from error


def _state_to_payload(state: CostRunState) -> dict[str, Any]:
    return {
        "budget_usd": state.budget_usd,
        "spent_usd": state.spent_usd,
        "ai_calls": state.ai_calls,
        "started_at": state.started_at.isoformat(),
        "updated_at": state.updated_at.isoformat(),
    }


def _load_sqlite_run_state(
    path: Path,
    *,
    edition: str,
    run_id: str,
    budget_usd: float,
) -> CostRunState | None:
    if not path.exists():
        return None
    try:
        with open_program_db(path, read_only=True) as connection:
            _ensure_sqlite_schema(connection)
            row = connection.execute(
                """
                SELECT edition, run_id, budget_usd, spent_usd, ai_calls, started_at, updated_at
                FROM ai_cost_runs
                WHERE edition = ? AND run_id = ?
                """,
                (edition, run_id),
            ).fetchone()
    except sqlite3.DatabaseError as error:
        raise StateError(f"Invalid cost guard SQLite ledger in {path}") from error
    if row is None:
        return None
    return _state_from_sqlite_row(row, budget_usd=budget_usd)


def _load_sqlite_run_states(path: Path, *, edition: str) -> tuple[CostRunState, ...] | None:
    if not path.exists():
        return None
    try:
        with open_program_db(path, read_only=True) as connection:
            _ensure_sqlite_schema(connection)
            rows = connection.execute(
                """
                SELECT edition, run_id, budget_usd, spent_usd, ai_calls, started_at, updated_at
                FROM ai_cost_runs
                WHERE edition = ?
                ORDER BY updated_at DESC, run_id DESC
                """,
                (edition,),
            ).fetchall()
    except sqlite3.DatabaseError as error:
        raise StateError(f"Invalid cost guard SQLite ledger in {path}") from error
    if not rows:
        return None
    return tuple(_state_from_sqlite_row(row, budget_usd=0.0) for row in rows)


def _write_sqlite_state(path: Path, state: CostRunState) -> None:
    try:
        with open_program_db(path, durability="strict") as connection:
            _ensure_sqlite_schema(connection)
            connection.execute(
                """
                INSERT INTO ai_cost_runs (
                    edition,
                    run_id,
                    budget_usd,
                    reserved_usd,
                    spent_usd,
                    ai_calls,
                    started_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(edition, run_id) DO UPDATE SET
                    budget_usd = excluded.budget_usd,
                    spent_usd = excluded.spent_usd,
                    ai_calls = excluded.ai_calls,
                    updated_at = excluded.updated_at
                """,
                (
                    state.edition,
                    state.run_id,
                    state.budget_usd,
                    0.0,
                    state.spent_usd,
                    state.ai_calls,
                    state.started_at.isoformat(),
                    state.updated_at.isoformat(),
                ),
            )
    except sqlite3.DatabaseError as error:
        raise StateError(f"Unable to persist cost guard SQLite ledger at {path}") from error


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


def _state_from_sqlite_row(row: sqlite3.Row, *, budget_usd: float) -> CostRunState:
    stored_budget = _coerce_state_float(row["budget_usd"], field_name="budget_usd")
    return CostRunState(
        edition=str(row["edition"]),
        run_id=str(row["run_id"]),
        budget_usd=stored_budget if stored_budget > 0 else budget_usd,
        spent_usd=_coerce_state_float(row["spent_usd"], field_name="spent_usd"),
        ai_calls=_coerce_state_int(row["ai_calls"], field_name="ai_calls"),
        started_at=_parse_state_datetime(row["started_at"], field_name="started_at") or _raise_missing_state_datetime("started_at"),
        updated_at=_parse_state_datetime(row["updated_at"], field_name="updated_at") or _raise_missing_state_datetime("updated_at"),
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
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_state_datetime(value: Any, *, field_name: str) -> datetime | None:
    if value is None:
        return None
    parsed = _parse_datetime(value)
    if parsed is None:
        raise StateError(f"Invalid cost guard {field_name} value: {value!r}")
    return parsed