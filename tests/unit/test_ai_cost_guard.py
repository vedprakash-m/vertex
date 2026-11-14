from __future__ import annotations

import json
import math
import sqlite3
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone
from pathlib import Path
from threading import Barrier, Event

import pytest

from src.ai.client import BudgetExceeded
from src.ai.cost_guard import CostGuard, CostReservation, load_latest_run_state, load_run_states
from src.core.exceptions import StateError


EDITION_NAME = "acme_weekly"
PROGRAM_ID = "acme"


def _guard_path(programs_root, suffix: str):
    """Return path under programs_root for this edition's AI output."""
    return programs_root / EDITION_NAME / "publications" / EDITION_NAME / "ai" / suffix


def test_cost_guard_records_and_persists_run_state(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    guard = CostGuard(edition=EDITION_NAME, run_id="run-001", budget_usd=0.5, programs_root=programs_root)

    initial_state = guard.current_state()
    updated_state = guard.record(0.12)
    reloaded_guard = CostGuard(edition=EDITION_NAME, run_id="run-001", budget_usd=0.5, programs_root=programs_root)
    reloaded_state = reloaded_guard.current_state()

    assert initial_state.spent_usd == 0.0
    assert updated_state.spent_usd == pytest.approx(0.12)
    assert updated_state.ai_calls == 1
    assert reloaded_state.spent_usd == pytest.approx(0.12)
    assert reloaded_state.ai_calls == 1
    assert reloaded_guard.state_path.exists()
    assert reloaded_guard.ledger_path.exists()

    with sqlite3.connect(reloaded_guard.ledger_path) as connection:
        row = connection.execute(
            "SELECT budget_usd, spent_usd, ai_calls FROM ai_cost_runs WHERE edition = ? AND run_id = ?",
            (EDITION_NAME, "run-001"),
        ).fetchone()

    assert row == pytest.approx((0.5, 0.12, 1))


@pytest.mark.parametrize(
    "budget",
    [-1, float("nan"), float("inf"), float("-inf"), True, "0.5", None, 10**400],
    ids=["negative", "nan", "infinity", "negative-infinity", "boolean", "string", "none", "overflow"],
)
def test_cost_guard_rejects_invalid_constructor_budget_before_storage(tmp_path, budget) -> None:
    with pytest.raises(StateError, match="Invalid cost guard budget_usd value"):
        CostGuard(edition=EDITION_NAME, run_id="invalid-budget", budget_usd=budget, programs_root=tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("budget", [0, 1, 0.5])
def test_cost_guard_accepts_finite_nonnegative_constructor_budget(tmp_path, budget) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="valid-budget", budget_usd=budget, programs_root=tmp_path)
    assert guard.current_state().budget_usd == budget


def test_cost_guard_blocks_when_run_budget_would_be_exceeded(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    guard = CostGuard(edition=EDITION_NAME, run_id="run-001", budget_usd=0.5, programs_root=programs_root)

    guard.record(0.4)

    with pytest.raises(BudgetExceeded, match="run ceiling"):
        guard.check(0.11)

    with pytest.raises(BudgetExceeded, match="run ceiling"):
        guard.record(0.11)


def _configured_budget_test_guard(
    tmp_path: Path, storage: str, run_id: str, retained_budget: float,
) -> CostGuard:
    original = CostGuard(edition=EDITION_NAME, run_id=run_id, budget_usd=retained_budget, programs_root=tmp_path)
    if storage == "json":
        original.state_path.parent.mkdir(parents=True, exist_ok=True)
        original.state_path.write_text(
            json.dumps({
                "edition": EDITION_NAME,
                "runs": {
                    run_id: {
                        "budget_usd": retained_budget, "spent_usd": 0.125, "reserved_usd": 0.125, "ai_calls": 1,
                        "started_at": "2026-05-16T00:00:00+00:00", "updated_at": "2026-05-16T00:00:00+00:00",
                    },
                },
            }),
            encoding="utf-8",
        )
    else:
        original.record_actual(0.125)
        original.reserve_call(0.125)
    return original


@pytest.mark.parametrize("storage", ["json", "sqlite"])
@pytest.mark.parametrize("operation", ["check", "reserve", "record"])
def test_configured_run_budget_refuses_lower_ceiling_before_admission(
    tmp_path: Path, storage: str, operation: str,
) -> None:
    original = _configured_budget_test_guard(tmp_path, storage, "lower-budget", 1.0)
    before = original.current_state()
    projection_before = original.state_path.read_bytes()
    guard = CostGuard(edition=EDITION_NAME, run_id="lower-budget", budget_usd=0.5, programs_root=tmp_path)

    with pytest.raises(BudgetExceeded, match=r"\$0\.50.*run ceiling"):
        if operation == "check":
            guard.check(0.3125)
        elif operation == "reserve":
            guard.reserve_call(0.3125)
        else:
            guard.record(0.3125)

    assert original.state_path.read_bytes() == projection_before
    assert guard.current_state() == before
    if original.ledger_path.exists():
        with sqlite3.connect(guard.ledger_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM ai_cost_reservations").fetchone() == (
                1 if storage == "sqlite" else 0,
            )


@pytest.mark.parametrize("retained_budget,configured_budget", [(1.0, 0.5), (0.5, 0.5), (0.5, 1.0)])
@pytest.mark.parametrize("operation", ["check", "reserve", "record"])
@pytest.mark.parametrize("storage", ["json", "sqlite"])
def test_configured_run_budget_allows_exact_stricter_ceiling(
    tmp_path: Path, retained_budget: float, configured_budget: float, operation: str, storage: str,
) -> None:
    original = _configured_budget_test_guard(tmp_path, storage, "exact-budget", retained_budget)
    guard = CostGuard(
        edition=EDITION_NAME, run_id="exact-budget", budget_usd=configured_budget, programs_root=tmp_path,
    )

    if operation == "check":
        guard.check(0.25)
    elif operation == "reserve":
        guard.reserve_call(0.25)
    else:
        guard.record(0.25)

    state = guard.current_state()
    assert state.budget_usd == retained_budget
    assert state.spent_usd + state.reserved_usd == (0.25 if operation == "check" else 0.5)


@pytest.mark.parametrize("operation", ["reserve", "record"])
def test_configured_run_budget_rechecks_after_interleaved_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    original = CostGuard(edition=EDITION_NAME, run_id="interleaved-budget", budget_usd=1.0, programs_root=tmp_path)
    original.record_actual(0.125)
    guard = CostGuard(edition=EDITION_NAME, run_id="interleaved-budget", budget_usd=0.5, programs_root=tmp_path)
    if operation == "reserve":
        current_state = guard.current_state

        def capture_then_spend():
            state = current_state()
            original.record_actual(0.25, ai_calls=0)
            return state

        monkeypatch.setattr(guard, "current_state", capture_then_spend)
    else:
        check = guard.check

        def check_then_spend(amount: float = 0.0) -> None:
            check(amount)
            original.record_actual(0.25, ai_calls=0)

        monkeypatch.setattr(guard, "check", check_then_spend)

    with pytest.raises(BudgetExceeded, match=r"\$0\.50.*run ceiling"):
        if operation == "reserve":
            guard.reserve_call(0.25)
        else:
            guard.record(0.25)

    state = original.current_state()
    assert (state.budget_usd, state.spent_usd, state.reserved_usd, state.ai_calls) == (1.0, 0.375, 0.0, 1)
    with sqlite3.connect(original.ledger_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ai_cost_reservations").fetchone() == (0,)


def test_configured_run_budget_does_not_block_actual_cost_or_settlement(tmp_path: Path) -> None:
    original = CostGuard(edition=EDITION_NAME, run_id="actual-budget", budget_usd=1.0, programs_root=tmp_path)
    reservation = original.reserve_call(0.375)
    guard = CostGuard(edition=EDITION_NAME, run_id="actual-budget", budget_usd=0.25, programs_root=tmp_path)

    settled = guard.settle_call(reservation, 0.5)
    recorded = guard.record_actual(0.125)

    assert (settled.budget_usd, settled.spent_usd, settled.reserved_usd, settled.ai_calls) == (1.0, 0.5, 0.0, 1)
    assert (recorded.budget_usd, recorded.spent_usd, recorded.reserved_usd, recorded.ai_calls) == (1.0, 0.625, 0.0, 2)
    assert recorded.within_budget
    with pytest.raises(BudgetExceeded, match=r"\$0\.25.*run ceiling"):
        guard.check()


def test_configured_run_budget_does_not_propagate_to_other_guards(tmp_path: Path) -> None:
    original = _configured_budget_test_guard(tmp_path, "sqlite", "independent-budget", 1.0)
    lower = CostGuard(
        edition=EDITION_NAME, run_id="independent-budget", budget_usd=0.5, programs_root=tmp_path,
    )
    before = original.current_state()

    with pytest.raises(BudgetExceeded, match=r"\$0\.50.*run ceiling"):
        lower.check(0.375)
    original.check(0.375)
    assert original.current_state() == before


@pytest.mark.parametrize("existing_run", [False, True])
def test_checked_record_is_atomic_across_concurrent_guards(tmp_path, monkeypatch, existing_run) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="checked-concurrent", budget_usd=1.0, programs_root=tmp_path)
    if existing_run:
        guard.record_actual(0.0, ai_calls=0)
    check_barrier, snapshot_barrier = Barrier(2), Barrier(2)

    def record():
        writer = CostGuard(
            edition=EDITION_NAME, run_id="checked-concurrent", budget_usd=1.0, programs_root=tmp_path,
        )
        check, current_state = writer.check, writer.current_state
        reads = 0

        def synchronized_check(estimated_cost_usd=0.0):
            check(estimated_cost_usd)
            check_barrier.wait(timeout=10)

        def synchronized_state():
            nonlocal reads
            state = current_state()
            reads += 1
            if reads == 2:
                snapshot_barrier.wait(timeout=10)
            return state

        monkeypatch.setattr(writer, "check", synchronized_check)
        monkeypatch.setattr(writer, "current_state", synchronized_state)
        return writer.record(0.6)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(record) for _ in range(2)]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result(timeout=10))
            except BudgetExceeded as exc:
                assert str(exc) == (
                    "Spent/reserved $0.600 of $1.00; next AI spend $0.600 would exceed the run ceiling."
                )
                outcomes.append(None)

    assert sum(outcome is not None for outcome in outcomes) == 1
    state = guard.current_state()
    assert (state.spent_usd, state.reserved_usd, state.ai_calls) == pytest.approx((0.6, 0.0, 1))
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT spent_usd, reserved_usd, ai_calls FROM ai_cost_runs WHERE edition = ? AND run_id = ?",
            (EDITION_NAME, "checked-concurrent"),
        ).fetchone() == pytest.approx((0.6, 0.0, 1))
    projection = json.loads(guard.state_path.read_text(encoding="utf-8"))["runs"]["checked-concurrent"]
    assert (projection["spent_usd"], projection["reserved_usd"], projection["ai_calls"]) == pytest.approx(
        (0.6, 0.0, 1),
    )


@pytest.mark.parametrize("interleaving", ["reservation", "actual", "settlement", "record"])
@pytest.mark.parametrize("pause_at", ["preflight", "fallback"])
def test_checked_record_rechecks_ceiling_after_interleaved_update(tmp_path, monkeypatch, interleaving, pause_at) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="checked-interleaved", budget_usd=1.0, programs_root=tmp_path)
    guard.record_actual(0.0, ai_calls=0)
    reservation = guard.reserve_call(0.25) if interleaving == "settlement" else None
    writer = CostGuard(edition=EDITION_NAME, run_id="checked-interleaved", budget_usd=1.0, programs_root=tmp_path)
    captured, resume = Event(), Event()
    check, current_state = writer.check, writer.current_state
    reads = 0

    def pause():
        captured.set()
        if not resume.wait(10):
            raise TimeoutError("interleaved ledger update did not finish")

    def pause_after_check(estimated_cost_usd=0.0):
        check(estimated_cost_usd)
        pause()

    def pause_after_fallback():
        nonlocal reads
        state = current_state()
        reads += 1
        if reads == 2:
            pause()
        return state

    if pause_at == "preflight":
        monkeypatch.setattr(writer, "check", pause_after_check)
    else:
        monkeypatch.setattr(writer, "current_state", pause_after_fallback)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(writer.record, 0.6)
        try:
            assert captured.wait(10)
            if interleaving == "reservation":
                guard.reserve_call(0.5)
            elif interleaving == "actual":
                guard.record_actual(0.5)
            elif interleaving == "settlement":
                guard.settle_call(reservation, 0.5)
            else:
                guard.record(0.5)
            before = guard.current_state()
            projection_before = guard.state_path.read_bytes()
            with sqlite3.connect(guard.ledger_path) as connection:
                reservations_before = connection.execute(
                    "SELECT reservation_id, reserved_usd, status, actual_usd FROM ai_cost_reservations"
                ).fetchall()
        finally:
            resume.set()
        with pytest.raises(BudgetExceeded, match="run ceiling") as error:
            future.result(timeout=10)

    assert str(error.value) == "Spent/reserved $0.500 of $1.00; next AI spend $0.600 would exceed the run ceiling."
    assert guard.current_state() == before
    assert guard.state_path.read_bytes() == projection_before
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT reservation_id, reserved_usd, status, actual_usd FROM ai_cost_reservations"
        ).fetchall() == reservations_before
    with pytest.raises(BudgetExceeded, match="run ceiling"):
        guard.check(0.6)


def test_checked_record_allows_exact_ceiling_and_preserves_reservations(tmp_path) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="checked-equality", budget_usd=1.0, programs_root=tmp_path)
    reservation = guard.reserve_call(0.25)

    state = guard.record(0.75, ai_calls=0)

    assert (state.spent_usd, state.reserved_usd, state.ai_calls) == pytest.approx((0.75, 0.25, 0))
    before = guard.state_path.read_bytes()
    with pytest.raises(BudgetExceeded, match="run ceiling"):
        guard.record(0.01)
    assert guard.current_state() == state
    assert guard.state_path.read_bytes() == before
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT status, actual_usd FROM ai_cost_reservations WHERE reservation_id = ?",
            (reservation.reservation_id,),
        ).fetchone() == ("reserved", None)


def test_checked_record_preflight_refusal_does_not_create_storage(tmp_path) -> None:
    root = tmp_path / "absent"
    guard = CostGuard(edition=EDITION_NAME, run_id="checked-preflight", budget_usd=1.0, programs_root=root)

    with pytest.raises(BudgetExceeded, match="run ceiling"):
        guard.record(1.01)

    assert not root.exists()


def test_cost_guard_persists_and_settles_conservative_reservation(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    guard = CostGuard(edition=EDITION_NAME, run_id="run-001", budget_usd=0.5, programs_root=programs_root)

    reserved = guard.reserve(0.4)

    assert reserved.reserved_usd == pytest.approx(0.4)
    with pytest.raises(BudgetExceeded, match="Spent/reserved"):
        guard.reserve(0.11)

    settled = guard.settle_reservation(0.4, 0.12)

    assert settled.spent_usd == pytest.approx(0.12)
    assert settled.reserved_usd == 0.0
    assert settled.ai_calls == 1


def test_call_reservations_are_atomic_across_concurrent_clients_and_settle_once(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    # Seed the SQLite file before the concurrent writers open it.
    CostGuard(edition=EDITION_NAME, run_id="run-atomic", budget_usd=1.0, programs_root=programs_root).record_actual(0.0)

    def reserve() -> object:
        return CostGuard(edition=EDITION_NAME, run_id="run-atomic", budget_usd=1.0, programs_root=programs_root).reserve_call(0.6)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(reserve) for _ in range(2)]
    outcomes = []
    for future in futures:
        try:
            outcomes.append(future.result())
        except BudgetExceeded:
            outcomes.append(None)

    reservations = [outcome for outcome in outcomes if outcome is not None]
    assert len(reservations) == 1
    reservation = reservations[0]
    guard = CostGuard(edition=EDITION_NAME, run_id="run-atomic", budget_usd=1.0, programs_root=programs_root)
    settled = guard.settle_call(reservation, 0.2)
    assert settled.spent_usd == pytest.approx(0.2)
    assert settled.reserved_usd == 0.0
    with pytest.raises(StateError, match="already been settled"):
        guard.settle_call(reservation, 0.2)


@pytest.mark.parametrize("operation", ["settle", "release"])
def test_call_reservation_rejects_matching_id_from_another_ledger(
    tmp_path: Path, operation: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = CostGuard(edition=EDITION_NAME, run_id="same-run", budget_usd=1.0, programs_root=tmp_path / "first")
    second = CostGuard(edition=EDITION_NAME, run_id="same-run", budget_usd=1.0, programs_root=tmp_path / "second")
    first_reservation = first.reserve_call(0.4)
    second_reservation = second.reserve_call(0.4)
    assert first_reservation.reservation_id == second_reservation.reservation_id
    before_first = first.state_path.read_bytes()
    before_second = second.state_path.read_bytes()

    with monkeypatch.context() as blocked_open:
        blocked_open.setattr(
            "src.ai.cost_guard.open_program_db",
            lambda *args, **kwargs: pytest.fail("foreign ledger must not be opened"),
        )
        with pytest.raises(StateError, match="different ledger") as error:
            if operation == "settle":
                second.settle_call(first_reservation, 0.1)
            else:
                second._release_undispatched_call(first_reservation)

    assert str(first.ledger_path.resolve()) not in repr(first_reservation)
    assert str(first.ledger_path.resolve()) not in str(error.value)
    assert str(second.ledger_path.resolve()) not in str(error.value)

    for guard, before in ((first, before_first), (second, before_second)):
        state = guard.current_state()
        assert (state.spent_usd, state.reserved_usd, state.ai_calls) == pytest.approx((0.0, 0.4, 0))
        assert guard.state_path.read_bytes() == before
        with sqlite3.connect(guard.ledger_path) as connection:
            assert connection.execute(
                "SELECT status, actual_usd FROM ai_cost_reservations"
            ).fetchall() == [("reserved", None)]
            assert connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'ai_cost_non_dispatch_releases'"
            ).fetchone() == (0,)

    reloaded = CostGuard(
        edition=EDITION_NAME, run_id="same-run", budget_usd=1.0, programs_root=tmp_path / "second",
    )
    settled = reloaded.settle_call(second_reservation, 0.1)
    assert (settled.spent_usd, settled.reserved_usd, settled.ai_calls) == pytest.approx((0.1, 0.0, 1))


@pytest.mark.parametrize("operation", ["settle", "release"])
def test_call_reservation_rejects_unbound_legacy_handle(
    tmp_path: Path, operation: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="unbound", budget_usd=1.0, programs_root=tmp_path)
    reservation = guard.reserve_call(0.4)
    unbound = CostReservation(
        reservation_id=reservation.reservation_id,
        estimated_cost_usd=reservation.estimated_cost_usd,
        state=reservation.state,
    )
    projection_before = guard.state_path.read_bytes()

    with monkeypatch.context() as blocked_open:
        blocked_open.setattr(
            "src.ai.cost_guard.open_program_db",
            lambda *args, **kwargs: pytest.fail("unbound handle must not open the ledger"),
        )
        with pytest.raises(StateError, match="not bound"):
            if operation == "settle":
                guard.settle_call(unbound, 0.1)
            else:
                guard._release_undispatched_call(unbound)

    assert guard.current_state().reserved_usd == pytest.approx(0.4)
    assert guard.state_path.read_bytes() == projection_before


@pytest.mark.parametrize("operation", ["settle", "release"])
def test_call_reservation_rejects_foreign_handle_before_creating_ledger(tmp_path: Path, operation: str) -> None:
    first = CostGuard(edition=EDITION_NAME, run_id="same-run", budget_usd=1.0, programs_root=tmp_path / "first")
    reservation = first.reserve_call(0.4)
    foreign_root = tmp_path / "absent"
    foreign = CostGuard(edition=EDITION_NAME, run_id="same-run", budget_usd=1.0, programs_root=foreign_root)

    with pytest.raises(StateError, match="different ledger"):
        if operation == "settle":
            foreign.settle_call(reservation, 0.1)
        else:
            foreign._release_undispatched_call(reservation)

    assert not foreign_root.exists()
    assert first.current_state().reserved_usd == pytest.approx(0.4)


def test_call_reservation_accepts_relative_and_absolute_paths_to_same_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    relative = CostGuard(edition=EDITION_NAME, run_id="alias", budget_usd=1.0, programs_root=Path("programs"))
    reservation = relative.reserve_call(0.4)
    absolute = CostGuard(
        edition=EDITION_NAME, run_id="alias", budget_usd=1.0, programs_root=tmp_path / "programs",
    )
    state = absolute.settle_call(reservation, 0.1)
    assert (state.spent_usd, state.reserved_usd, state.ai_calls) == pytest.approx((0.1, 0.0, 1))


def test_non_dispatch_release_preserves_other_reservations_and_records_identity(tmp_path: Path) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="release", budget_usd=1.0, programs_root=tmp_path)
    other = guard.reserve_call(0.3)
    released = guard.reserve_call(0.4)
    foreign = CostGuard(edition=EDITION_NAME, run_id="foreign", budget_usd=1.0, programs_root=tmp_path)

    with pytest.raises(StateError, match="Unknown cost reservation"):
        foreign._release_undispatched_call(released)
    state = guard._release_undispatched_call(released)

    assert (state.spent_usd, state.reserved_usd, state.ai_calls) == pytest.approx((0.0, 0.3, 0))
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT reservation_id, status, actual_usd FROM ai_cost_reservations ORDER BY reservation_id"
        ).fetchall() == [
            (other.reservation_id, "reserved", None),
            (released.reservation_id, "settled", 0.0),
        ]
        assert connection.execute(
            "SELECT reservation_id FROM ai_cost_non_dispatch_releases"
        ).fetchall() == [(released.reservation_id,)]
    with pytest.raises(StateError, match="already been settled"):
        guard._release_undispatched_call(released)
    with pytest.raises(StateError, match="already been settled"):
        guard.settle_call(released, 0.1)

    state = guard.settle_call(other, 0.0)
    assert (state.spent_usd, state.reserved_usd, state.ai_calls) == pytest.approx((0.0, 0.0, 1))
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT reservation_id FROM ai_cost_non_dispatch_releases"
        ).fetchall() == [(released.reservation_id,)]


def test_non_dispatch_release_rolls_back_if_marker_insert_fails(tmp_path: Path) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="release-rollback", budget_usd=1.0, programs_root=tmp_path)
    reservation = guard.reserve_call(0.4)
    projection_before = guard.state_path.read_bytes()
    with sqlite3.connect(guard.ledger_path) as connection:
        connection.execute(
            "CREATE TABLE ai_cost_non_dispatch_releases "
            "(reservation_id INTEGER PRIMARY KEY, released_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TRIGGER reject_release BEFORE INSERT ON ai_cost_non_dispatch_releases "
            "BEGIN SELECT RAISE(ABORT, 'synthetic marker failure'); END"
        )

    with pytest.raises(StateError):
        guard._release_undispatched_call(reservation)

    assert guard.current_state().reserved_usd == pytest.approx(0.4)
    assert guard.state_path.read_bytes() == projection_before
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT status, actual_usd FROM ai_cost_reservations"
        ).fetchall() == [("reserved", None)]
        assert connection.execute("SELECT COUNT(*) FROM ai_cost_non_dispatch_releases").fetchone() == (0,)


@pytest.mark.parametrize("corruption", ["missing-run", "insufficient-balance"])
def test_non_dispatch_release_refuses_inconsistent_run_state(tmp_path: Path, corruption: str) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="release-invalid", budget_usd=1.0, programs_root=tmp_path)
    reservation = guard.reserve_call(0.4)
    projection_before = guard.state_path.read_bytes()
    with sqlite3.connect(guard.ledger_path) as connection:
        if corruption == "missing-run":
            connection.execute("DELETE FROM ai_cost_runs")
        else:
            connection.execute("UPDATE ai_cost_runs SET reserved_usd = 0.2")

    with pytest.raises(StateError):
        guard._release_undispatched_call(reservation)

    assert guard.state_path.read_bytes() == projection_before
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT status, actual_usd FROM ai_cost_reservations"
        ).fetchall() == [("reserved", None)]
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'ai_cost_non_dispatch_releases'"
        ).fetchone() == (0,)


def test_non_dispatch_release_and_settlement_are_atomic_under_contention(tmp_path: Path) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="release-race", budget_usd=1.0, programs_root=tmp_path)
    reservation = guard.reserve_call(0.4)
    barrier = Barrier(2)

    def finish(release: bool) -> str:
        barrier.wait()
        try:
            if release:
                guard._release_undispatched_call(reservation)
                return "release"
            guard.settle_call(reservation, 0.1)
            return "settle"
        except StateError:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(finish, (True, False)))

    assert results.count("refused") == 1
    state = guard.current_state()
    assert state.reserved_usd == pytest.approx(0.0)
    with sqlite3.connect(guard.ledger_path) as connection:
        table_exists = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'ai_cost_non_dispatch_releases'"
        ).fetchone() == (1,)
        markers = (
            connection.execute("SELECT reservation_id FROM ai_cost_non_dispatch_releases").fetchall()
            if table_exists else []
        )
        row = connection.execute(
            "SELECT status, actual_usd FROM ai_cost_reservations"
        ).fetchone()
    if "release" in results:
        assert (state.spent_usd, state.ai_calls) == pytest.approx((0.0, 0))
        assert row == ("settled", 0.0)
        assert markers == [(reservation.reservation_id,)]
    else:
        assert (state.spent_usd, state.ai_calls) == pytest.approx((0.1, 1))
        assert row == ("settled", 0.1)
        assert markers == []


@pytest.mark.parametrize("operation", ["reserve", "record"])
@pytest.mark.parametrize("message", ["database is locked", "database is busy"])
def test_cost_guard_first_open_retries_transient_lock(tmp_path, monkeypatch, operation, message) -> None:
    original_connect = sqlite3.connect
    failures: list[str] = []

    class ContendedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            if sql.startswith("PRAGMA journal_mode") and not failures:
                failures.append(message)
                raise sqlite3.OperationalError(message)
            return super().execute(sql, parameters)

    def connect(*args, **kwargs):
        return original_connect(*args, factory=ContendedConnection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    guard = CostGuard(edition=EDITION_NAME, run_id="first-open", budget_usd=1.0, programs_root=tmp_path)
    if operation == "reserve":
        guard.reserve_call(0.3)
    else:
        guard.record_actual(0.2)

    state = guard.current_state()
    assert failures == [message]
    assert state.reserved_usd == pytest.approx(0.3 if operation == "reserve" else 0.0)
    assert state.spent_usd == pytest.approx(0.0 if operation == "reserve" else 0.2)


@pytest.mark.parametrize("operation", ["reserve", "record"])
@pytest.mark.parametrize("message", ["database is locked", "disk I/O error"])
def test_cost_guard_first_open_surfaces_unresolved_errors(tmp_path, monkeypatch, operation, message) -> None:
    original_connect = sqlite3.connect
    attempts: list[str] = []
    sleeps: list[float] = []

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            if sql.startswith("PRAGMA journal_mode"):
                attempts.append(message)
                raise sqlite3.OperationalError(message)
            return super().execute(sql, parameters)

    def connect(*args, **kwargs):
        return original_connect(*args, factory=FailingConnection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr("src.core._db.time.sleep", sleeps.append)
    guard = CostGuard(edition=EDITION_NAME, run_id="open-error", budget_usd=1.0, programs_root=tmp_path)
    with pytest.raises(StateError, match="cost guard SQLite ledger") as error:
        if operation == "reserve":
            guard.reserve_call(0.3)
        else:
            guard.record_actual(0.2)

    assert isinstance(error.value.__cause__, sqlite3.OperationalError)
    assert str(error.value.__cause__) == message
    assert len(attempts) == (8 if message == "database is locked" else 1)
    assert not guard.state_path.exists()


@pytest.mark.parametrize("iteration", range(20))
def test_cost_guard_first_open_serializes_concurrent_reservations(tmp_path, monkeypatch, iteration) -> None:
    run_id = f"first-concurrent-{iteration}"
    guards = [
        CostGuard(edition=EDITION_NAME, run_id=run_id, budget_usd=1.0, programs_root=tmp_path)
        for _ in range(2)
    ]
    preflight = Barrier(2, timeout=10)
    for guard in guards:
        current_state = guard.current_state

        def synchronized_state(read=current_state):
            state = read()
            preflight.wait()
            return state

        monkeypatch.setattr(guard, "current_state", synchronized_state)

    def reserve(guard):
        try:
            return guard.reserve_call(0.6)
        except BudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(reserve, guards))

    assert sum(outcome is not None for outcome in outcomes) == 1
    reloaded = CostGuard(edition=EDITION_NAME, run_id=run_id, budget_usd=1.0, programs_root=tmp_path)
    assert reloaded.current_state().reserved_usd == pytest.approx(0.6)


@pytest.mark.parametrize("operation", ["state", "list", "reserve"])
def test_cost_guard_first_open_waits_for_in_progress_schema(tmp_path, monkeypatch, operation) -> None:
    from src.ai import cost_guard

    original_connect = sqlite3.connect
    original_ensure = cost_guard._ensure_sqlite_schema
    schema_paused, missing_table, resume = Event(), Event(), Event()

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            try:
                return super().execute(sql, parameters)
            except sqlite3.OperationalError as error:
                if str(error) == "no such table: ai_cost_runs":
                    missing_table.set()
                raise

    def connect(*args, **kwargs):
        return original_connect(*args, factory=ObservedConnection, **kwargs)

    def pause_schema(connection):
        schema_paused.set()
        if not resume.wait(10):
            raise TimeoutError("cost ledger initialization did not resume")
        original_ensure(connection)

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(cost_guard, "_ensure_sqlite_schema", pause_schema)
    writer = CostGuard(edition=EDITION_NAME, run_id="schema-race", budget_usd=1.0, programs_root=tmp_path)
    reader = CostGuard(edition=EDITION_NAME, run_id="schema-race", budget_usd=1.0, programs_root=tmp_path)

    def try_reserve(guard):
        try:
            return guard.reserve_call(0.6)
        except BudgetExceeded:
            return None

    def read_or_reserve():
        if operation == "state":
            return reader.current_state()
        if operation == "list":
            return load_run_states(EDITION_NAME, programs_root=tmp_path)
        return try_reserve(reader)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(try_reserve, writer)
        try:
            assert schema_paused.wait(10)
            second = executor.submit(read_or_reserve)
            assert missing_table.wait(10)
        finally:
            resume.set()
        first_reservation = first.result(timeout=10)
        second_outcome = second.result(timeout=10)

    if operation == "reserve":
        assert sum(outcome is not None for outcome in (first_reservation, second_outcome)) == 1
    else:
        assert first_reservation is not None
    assert writer.current_state().reserved_usd == pytest.approx(0.6)


@pytest.mark.parametrize("operation", ["state", "list", "reserve"])
def test_cost_guard_first_open_still_rejects_missing_schema(tmp_path, monkeypatch, operation) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="missing-schema", budget_usd=1.0, programs_root=tmp_path)
    guard.ledger_path.parent.mkdir(parents=True)
    with sqlite3.connect(guard.ledger_path) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER)")
    before = guard.ledger_path.read_bytes()
    monkeypatch.setattr("src.core._db.time.sleep", lambda _: None)

    with pytest.raises(StateError, match="Invalid cost guard SQLite ledger"):
        if operation == "state":
            guard.current_state()
        elif operation == "list":
            load_run_states(EDITION_NAME, programs_root=tmp_path)
        else:
            guard.reserve_call(0.3)

    assert guard.ledger_path.read_bytes() == before
    assert not guard.state_path.exists()


@pytest.mark.parametrize("operation", ["state", "list", "reserve", "record"])
@pytest.mark.parametrize("error_type", [sqlite3.OperationalError, sqlite3.DatabaseError])
def test_cost_guard_first_open_surfaces_other_read_errors(tmp_path, monkeypatch, operation, error_type) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="read-error", budget_usd=1.0, programs_root=tmp_path)
    guard.record_actual(0.1)
    ledger_before = guard.ledger_path.read_bytes()
    projection_before = guard.state_path.read_bytes()
    calls = []

    def fail_open(path, *, read_only=False, **kwargs):
        assert read_only
        calls.append(path)
        raise error_type("synthetic read failure")

    monkeypatch.setattr("src.ai.cost_guard.open_program_db", fail_open)
    with pytest.raises(StateError, match="Invalid cost guard SQLite ledger") as error:
        if operation == "state":
            guard.current_state()
        elif operation == "list":
            load_run_states(EDITION_NAME, programs_root=tmp_path)
        elif operation == "reserve":
            guard.reserve_call(0.3)
        else:
            guard.record_actual(0.3)

    assert type(error.value.__cause__) is error_type
    assert calls == [guard.ledger_path]
    assert guard.ledger_path.read_bytes() == ledger_before
    assert guard.state_path.read_bytes() == projection_before


@pytest.mark.parametrize("operation", ["state", "list", "reserve", "record"])
def test_cost_guard_first_open_rejects_corrupt_sqlite_file(tmp_path, operation) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="corrupt", budget_usd=1.0, programs_root=tmp_path)
    guard.ledger_path.parent.mkdir(parents=True)
    before = b"not a SQLite database"
    guard.ledger_path.write_bytes(before)

    with pytest.raises(StateError, match="Invalid cost guard SQLite ledger"):
        if operation == "state":
            guard.current_state()
        elif operation == "list":
            load_run_states(EDITION_NAME, programs_root=tmp_path)
        elif operation == "reserve":
            guard.reserve_call(0.3)
        else:
            guard.record_actual(0.3)

    assert guard.ledger_path.read_bytes() == before
    assert not guard.state_path.exists()


def test_cost_guard_record_actual_persists_overspend(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    guard = CostGuard(edition=EDITION_NAME, run_id="run-001", budget_usd=0.5, programs_root=programs_root)

    updated_state = guard.record_actual(0.6)

    assert updated_state.spent_usd == pytest.approx(0.6)
    assert updated_state.ai_calls == 1
    assert guard.current_state().spent_usd == pytest.approx(0.6)


@pytest.mark.parametrize("interleaving", ["spend", "reservation", "settlement"])
def test_record_actual_preserves_interleaved_ledger_update(tmp_path, monkeypatch, interleaving) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="interleaved", budget_usd=1.0, programs_root=tmp_path)
    guard.record_actual(0.0, ai_calls=0)
    reservation = guard.reserve_call(0.3) if interleaving == "settlement" else None
    writer = CostGuard(edition=EDITION_NAME, run_id="interleaved", budget_usd=1.0, programs_root=tmp_path)
    captured, resume = Event(), Event()
    current_state = writer.current_state

    def pause_after_read():
        state = current_state()
        captured.set()
        if not resume.wait(10):
            raise TimeoutError("interleaved ledger update did not finish")
        return state

    monkeypatch.setattr(writer, "current_state", pause_after_read)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(writer.record_actual, 0.2)
        try:
            assert captured.wait(10)
            if interleaving == "spend":
                guard.record_actual(0.1)
            elif interleaving == "reservation":
                reservation = guard.reserve_call(0.3)
            else:
                guard.settle_call(reservation, 0.1)
        finally:
            resume.set()
        future.result(timeout=10)

    state = guard.current_state()
    assert state.spent_usd == pytest.approx(0.2 if interleaving == "reservation" else 0.3)
    assert state.reserved_usd == pytest.approx(0.3 if interleaving == "reservation" else 0.0)
    assert state.ai_calls == (1 if interleaving == "reservation" else 2)
    if interleaving == "reservation":
        with pytest.raises(BudgetExceeded):
            guard.check(0.6)
    if reservation is not None:
        with sqlite3.connect(guard.ledger_path) as connection:
            status = connection.execute(
                "SELECT status FROM ai_cost_reservations WHERE reservation_id = ?",
                (reservation.reservation_id,),
            ).fetchone()
        assert status == ("reserved" if interleaving == "reservation" else "settled",)


def test_legacy_settlement_preserves_interleaved_spend(tmp_path, monkeypatch) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="legacy-interleaved", budget_usd=1.0, programs_root=tmp_path)
    guard.state_path.parent.mkdir(parents=True)
    guard.state_path.write_text(json.dumps({
        "edition": EDITION_NAME,
        "runs": {
            "legacy-interleaved": {
                "budget_usd": 1.0,
                "spent_usd": 0.0,
                "reserved_usd": 0.3,
                "ai_calls": 0,
                "started_at": "2026-05-16T00:00:00+00:00",
                "updated_at": "2026-05-16T00:00:00+00:00",
            },
        },
    }), encoding="utf-8")
    writer = CostGuard(edition=EDITION_NAME, run_id="legacy-interleaved", budget_usd=1.0, programs_root=tmp_path)
    captured, resume = Event(), Event()
    current_state = writer.current_state

    def pause_after_read():
        state = current_state()
        captured.set()
        if not resume.wait(10):
            raise TimeoutError("interleaved legacy update did not finish")
        return state

    monkeypatch.setattr(writer, "current_state", pause_after_read)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(writer.settle_reservation, 0.3, 0.1)
        try:
            assert captured.wait(10)
            guard.record_actual(0.2)
        finally:
            resume.set()
        future.result(timeout=10)

    state = guard.current_state()
    assert state.spent_usd == pytest.approx(0.3)
    assert state.reserved_usd == 0.0
    assert state.ai_calls == 2


def test_legacy_settlement_cannot_consume_tracked_call_reservation(tmp_path) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="tracked-reservation", budget_usd=1.0, programs_root=tmp_path)
    reservation = guard.reserve_call(0.3)
    previous_state = guard.current_state()
    previous_projection = guard.state_path.read_bytes()

    with pytest.raises(StateError, match="reservation"):
        guard.settle_reservation(0.2, 0.1)

    assert guard.current_state() == previous_state
    assert guard.state_path.read_bytes() == previous_projection
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT status FROM ai_cost_reservations WHERE reservation_id = ?",
            (reservation.reservation_id,),
        ).fetchone() == ("reserved",)


@pytest.mark.parametrize(
    ("amount", "first_amount", "second_amount"),
    [
        (0.25, 0.25, 0.25),
        (0.25, 0.25, 0.2500000000005),
        (0.25, 0.24999999999925, 0.25000000000075),
        (0.0, 0.0, 0.0),
    ],
    ids=["equal", "within-tolerance", "opposite-tolerance-edges", "zero"],
)
def test_legacy_settlement_refuses_ambiguous_tracked_matches(
    tmp_path, amount, first_amount, second_amount,
) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="ambiguous-legacy", budget_usd=1.0, programs_root=tmp_path)
    uncertain = guard.reserve_call(first_amount)
    resolved = guard.reserve_call(second_amount)
    other_run = CostGuard(edition=EDITION_NAME, run_id="other-run", budget_usd=1.0, programs_root=tmp_path)
    other = other_run.reserve_call(amount)
    previous_state = guard.current_state()
    previous_projection = guard.state_path.read_bytes()

    with pytest.raises(StateError, match="ambiguous"):
        guard.settle_reservation(amount, 0.07)

    assert guard.current_state() == previous_state
    assert guard.state_path.read_bytes() == previous_projection
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT status FROM ai_cost_reservations ORDER BY reservation_id"
        ).fetchall() == [("reserved",), ("reserved",), ("reserved",)]

    state = guard.settle_call(resolved, 0.07)
    assert state.spent_usd == pytest.approx(0.07)
    assert state.reserved_usd == pytest.approx(first_amount)
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT reservation_id, status FROM ai_cost_reservations ORDER BY reservation_id"
        ).fetchall() == [
            (uncertain.reservation_id, "reserved"),
            (resolved.reservation_id, "settled"),
            (other.reservation_id, "reserved"),
        ]


def test_legacy_settlement_refuses_match_that_becomes_ambiguous(tmp_path, monkeypatch) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="late-ambiguous-legacy", budget_usd=1.0, programs_root=tmp_path)
    uncertain = guard.reserve_call(0.25)
    writer = CostGuard(edition=EDITION_NAME, run_id="late-ambiguous-legacy", budget_usd=1.0, programs_root=tmp_path)
    captured, resume = Event(), Event()
    find_reservation = writer._find_legacy_reservation

    def pause_after_lookup(amount):
        reservation = find_reservation(amount)
        captured.set()
        if not resume.wait(10):
            raise TimeoutError("interleaved call reservation did not finish")
        return reservation

    monkeypatch.setattr(writer, "_find_legacy_reservation", pause_after_lookup)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(writer.settle_reservation, 0.25, 0.07)
        try:
            assert captured.wait(10)
            resolved = guard.reserve_call(0.25)
            previous_state = guard.current_state()
            previous_projection = guard.state_path.read_bytes()
        finally:
            resume.set()
        with pytest.raises(StateError, match="ambiguous"):
            future.result(timeout=10)

    assert guard.current_state() == previous_state
    assert guard.state_path.read_bytes() == previous_projection
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT reservation_id, status FROM ai_cost_reservations ORDER BY reservation_id"
        ).fetchall() == [(uncertain.reservation_id, "reserved"), (resolved.reservation_id, "reserved")]


@pytest.mark.parametrize("replacement", [False, True], ids=["no-match", "different-match"])
def test_legacy_settlement_refuses_changed_tracked_match(tmp_path, monkeypatch, replacement) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="changed-legacy", budget_usd=1.0, programs_root=tmp_path)
    original = guard.reserve_call(0.25)
    writer = CostGuard(edition=EDITION_NAME, run_id="changed-legacy", budget_usd=1.0, programs_root=tmp_path)
    captured, resume = Event(), Event()
    find_reservation = writer._find_legacy_reservation

    def pause_after_lookup(amount):
        reservation = find_reservation(amount)
        captured.set()
        if not resume.wait(10):
            raise TimeoutError("interleaved reservation change did not finish")
        return reservation

    monkeypatch.setattr(writer, "_find_legacy_reservation", pause_after_lookup)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(writer.settle_reservation, 0.25, 0.07)
        try:
            assert captured.wait(10)
            with sqlite3.connect(guard.ledger_path) as connection:
                connection.execute(
                    "UPDATE ai_cost_reservations SET reserved_usd = ? WHERE reservation_id = ?",
                    (0.2, original.reservation_id),
                )
                connection.execute(
                    "UPDATE ai_cost_runs SET reserved_usd = ? WHERE edition = ? AND run_id = ?",
                    (0.2, EDITION_NAME, "changed-legacy"),
                )
            if replacement:
                guard.reserve_call(0.25)
            previous_state = guard.current_state()
            previous_projection = guard.state_path.read_bytes()
        finally:
            resume.set()
        with pytest.raises(StateError, match="changed"):
            future.result(timeout=10)

    assert guard.current_state() == previous_state
    assert guard.state_path.read_bytes() == previous_projection
    with sqlite3.connect(guard.ledger_path) as connection:
        statuses = connection.execute("SELECT status FROM ai_cost_reservations").fetchall()
    assert statuses == [("reserved",)] * (1 + replacement)


def test_legacy_settlement_retains_unique_tolerant_match_across_runs(tmp_path) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="unique-legacy", budget_usd=1.0, programs_root=tmp_path)
    guard.reserve(0.25)
    other_run = CostGuard(edition=EDITION_NAME, run_id="other-run", budget_usd=1.0, programs_root=tmp_path)
    other = other_run.reserve_call(0.25)

    state = guard.settle_reservation(0.2500000000005, 0.07)

    assert state.spent_usd == pytest.approx(0.07)
    assert state.reserved_usd == 0.0
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT status FROM ai_cost_reservations WHERE reservation_id = ?",
            (other.reservation_id,),
        ).fetchone() == ("reserved",)


def test_legacy_settlement_cannot_consume_interleaved_call_reservation(tmp_path, monkeypatch) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="interleaved-legacy", budget_usd=1.0, programs_root=tmp_path)
    guard.record_actual(0.0, ai_calls=0)
    writer = CostGuard(edition=EDITION_NAME, run_id="interleaved-legacy", budget_usd=1.0, programs_root=tmp_path)
    captured, resume = Event(), Event()
    find_reservation = writer._find_legacy_reservation

    def pause_after_lookup(amount):
        reservation = find_reservation(amount)
        captured.set()
        if not resume.wait(10):
            raise TimeoutError("interleaved call reservation did not finish")
        return reservation

    monkeypatch.setattr(writer, "_find_legacy_reservation", pause_after_lookup)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(writer.settle_reservation, 0.3, 0.1)
        try:
            assert captured.wait(10)
            reservation = guard.reserve_call(0.3)
        finally:
            resume.set()
        with pytest.raises(StateError, match="reservation"):
            future.result(timeout=10)

    state = guard.current_state()
    assert state.spent_usd == 0.0
    assert state.reserved_usd == pytest.approx(0.3)
    assert state.ai_calls == 0
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT status FROM ai_cost_reservations WHERE reservation_id = ?",
            (reservation.reservation_id,),
        ).fetchone() == ("reserved",)


def test_legacy_settlement_preserves_other_tracked_call_reservations(tmp_path) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="mixed-reservations", budget_usd=1.0, programs_root=tmp_path)
    legacy_reservation = guard.reserve_call(0.3)
    with sqlite3.connect(guard.ledger_path) as connection:
        connection.execute(
            "DELETE FROM ai_cost_reservations WHERE reservation_id = ?",
            (legacy_reservation.reservation_id,),
        )
    tracked_reservation = guard.reserve_call(0.2)

    state = guard.settle_reservation(0.3, 0.1)

    assert state.spent_usd == pytest.approx(0.1)
    assert state.reserved_usd == pytest.approx(0.2)
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT status FROM ai_cost_reservations WHERE reservation_id = ?",
            (tracked_reservation.reservation_id,),
        ).fetchone() == ("reserved",)


@pytest.mark.parametrize("operation", ["record", "reserve", "settle"])
def test_cost_projection_uses_latest_interleaved_ledger_state(tmp_path, monkeypatch, operation) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="projection-interleaved", budget_usd=1.0, programs_root=tmp_path)
    guard.record_actual(0.0, ai_calls=0)
    reservation = guard.reserve_call(0.3) if operation == "settle" else None
    writer = CostGuard(edition=EDITION_NAME, run_id="projection-interleaved", budget_usd=1.0, programs_root=tmp_path)
    committed, resume = Event(), Event()
    persist_projection = writer._persist_projection

    def pause_before_projection(state):
        committed.set()
        if not resume.wait(10):
            raise TimeoutError("interleaved projection update did not finish")
        persist_projection(state)

    monkeypatch.setattr(writer, "_persist_projection", pause_before_projection)
    calls = {
        "record": lambda: writer.record_actual(0.2),
        "reserve": lambda: writer.reserve_call(0.2),
        "settle": lambda: writer.settle_call(reservation, 0.2),
    }
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(calls[operation])
        try:
            assert committed.wait(10)
            guard.record_actual(0.1)
        finally:
            resume.set()
        future.result(timeout=10)

    state = guard.current_state()
    projection = json.loads(guard.state_path.read_text(encoding="utf-8"))["runs"]["projection-interleaved"]
    assert projection["spent_usd"] == pytest.approx(state.spent_usd)
    assert projection["reserved_usd"] == pytest.approx(state.reserved_usd)
    assert projection["ai_calls"] == state.ai_calls


def test_cost_projection_preserves_interleaved_updates_to_different_runs(tmp_path, monkeypatch) -> None:
    first = CostGuard(edition=EDITION_NAME, run_id="first", budget_usd=1.0, programs_root=tmp_path)
    second = CostGuard(edition=EDITION_NAME, run_id="second", budget_usd=1.0, programs_root=tmp_path)
    first.record_actual(0.0, ai_calls=0)
    second.record_actual(0.0, ai_calls=0)
    first_write, second_started, second_write, resume = Event(), Event(), Event(), Event()
    from src.ai import cost_guard

    write_json = cost_guard._write_atomic_json

    def interleave_projection(path, payload):
        if not first_write.is_set():
            first_write.set()
            if not resume.wait(10):
                raise TimeoutError("interleaved projection did not finish")
            write_json(path, payload)
        else:
            write_json(path, payload)
            second_write.set()

    def update_second():
        second_started.set()
        return second.record_actual(0.2)

    monkeypatch.setattr(cost_guard, "_write_atomic_json", interleave_projection)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(first.record_actual, 0.1)
        try:
            assert first_write.wait(10)
            second_future = executor.submit(update_second)
            assert second_started.wait(10)
            second_write.wait(1)
        finally:
            resume.set()
        first_future.result(timeout=10)
        second_future.result(timeout=10)

    projection = json.loads(first.state_path.read_text(encoding="utf-8"))["runs"]
    assert projection["first"]["spent_usd"] == pytest.approx(first.current_state().spent_usd)
    assert projection["second"]["spent_usd"] == pytest.approx(second.current_state().spent_usd)
    assert projection["first"]["spent_usd"] == pytest.approx(0.1)
    assert projection["second"]["spent_usd"] == pytest.approx(0.2)


@pytest.mark.parametrize(
    "projection",
    [
        b'{"edition": "private-projection"',
        b'{"edition": "private-projection", "runs": {}}',
        b'["private-projection"]',
        b'{"runs": {"private-projection": {}}}',
        b'{"edition": "acme_weekly"}',
        b'{"edition": "acme_weekly", "runs": ["private-projection"]}',
        b"\xffprivate-projection",
    ],
    ids=["malformed", "wrong-edition", "non-object", "missing-edition", "missing-runs", "invalid-runs", "invalid-utf8"],
)
@pytest.mark.parametrize("operation", ["record", "reserve", "settle"])
def test_cost_projection_refuses_corruption_without_replacing_bytes(tmp_path, projection, operation) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="projection-invalid", budget_usd=1.0, programs_root=tmp_path)
    guard.record_actual(0.0, ai_calls=0)
    reservation = guard.reserve_call(0.25) if operation == "settle" else None
    guard.state_path.write_bytes(projection)

    expected_error = UnicodeDecodeError if projection.startswith(b"\xff") else StateError
    with pytest.raises(expected_error) as error:
        if operation == "record":
            guard.record_actual(0.1)
        elif operation == "reserve":
            guard.reserve_call(0.25)
        else:
            assert reservation is not None
            guard.settle_call(reservation, 0.1)

    if expected_error is StateError:
        assert str(error.value) == "Unable to read cost guard projection; preserve it and reconcile before retrying."
    assert guard.state_path.read_bytes() == projection
    rendered_error = "".join(traceback.format_exception(error.type, error.value, error.tb))
    assert "private-projection" not in rendered_error
    assert str(guard.state_path) not in rendered_error
    state = guard.current_state()
    if operation == "reserve":
        assert state.reserved_usd == 0.0
        assert state.spent_usd == 0.0
    else:
        assert state.reserved_usd == 0.0
        assert state.spent_usd == pytest.approx(0.1)
    with sqlite3.connect(guard.ledger_path) as connection:
        statuses = connection.execute("SELECT status FROM ai_cost_reservations").fetchall()
    assert statuses == ([("settled",)] if operation == "settle" else [])


@pytest.mark.parametrize("operation", ["reserve", "reserve_call"])
@pytest.mark.parametrize("failure", ["invalid-json", "invalid-utf8", "unreadable"])
def test_cost_reservation_projection_preflight_preserves_existing_reservations(
    tmp_path, monkeypatch, operation, failure,
) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="preflight", budget_usd=1.0, programs_root=tmp_path)
    active = guard.reserve_call(0.25)
    previous_state = guard.current_state()
    valid_projection = guard.state_path.read_bytes()
    if failure == "invalid-json":
        guard.state_path.write_bytes(b'{"edition": "private-projection"')
    elif failure == "invalid-utf8":
        guard.state_path.write_bytes(b"\xffprivate-projection")
    projection_before = guard.state_path.read_bytes()
    with sqlite3.connect(guard.ledger_path) as connection:
        reservations_before = connection.execute(
            "SELECT reservation_id, reserved_usd, status FROM ai_cost_reservations ORDER BY reservation_id",
        ).fetchall()

    read_text = Path.read_text

    def fail_projection_read(path, *args, **kwargs):
        if path == guard.state_path:
            raise PermissionError("synthetic private projection error")
        return read_text(path, *args, **kwargs)

    expected_error = {
        "invalid-json": StateError,
        "invalid-utf8": UnicodeDecodeError,
        "unreadable": PermissionError,
    }[failure]
    with monkeypatch.context() as patch:
        if failure == "unreadable":
            patch.setattr(Path, "read_text", fail_projection_read)
        with pytest.raises(expected_error):
            getattr(guard, operation)(0.2)
        with pytest.raises(BudgetExceeded):
            guard.reserve_call(0.8)

    assert guard.current_state() == previous_state
    assert guard.state_path.read_bytes() == projection_before
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT reservation_id, reserved_usd, status FROM ai_cost_reservations ORDER BY reservation_id",
        ).fetchall() == reservations_before

    guard.state_path.write_bytes(valid_projection)
    settled = guard.settle_call(active, 0.1)
    assert settled.spent_usd == pytest.approx(0.1)
    assert settled.reserved_usd == 0.0


@pytest.mark.parametrize("operation", ["reserve", "reserve_call"])
@pytest.mark.parametrize("seeded", [False, True])
def test_cost_reservation_projection_preflight_recreates_absent_file(tmp_path, operation, seeded) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="preflight", budget_usd=1.0, programs_root=tmp_path)
    if seeded:
        guard.record_actual(0.0, ai_calls=0)
        guard.state_path.unlink()
    else:
        assert not guard.state_path.exists()

    getattr(guard, operation)(0.25)

    assert guard.current_state().reserved_usd == pytest.approx(0.25)
    projection = json.loads(guard.state_path.read_text(encoding="utf-8"))
    assert projection["runs"]["preflight"]["reserved_usd"] == pytest.approx(0.25)


@pytest.mark.parametrize("operation", ["reserve", "reserve_call"])
def test_cost_reservation_projection_failure_release_preserves_corrupt_projection(
    tmp_path, monkeypatch, operation,
) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="preflight", budget_usd=1.0, programs_root=tmp_path)
    active = guard.reserve_call(0.1)
    persist_projection = guard._persist_projection
    corrupted = b'{"edition": "private-projection"'

    def corrupt_before_refresh(state):
        guard.state_path.write_bytes(corrupted)
        persist_projection(state)

    monkeypatch.setattr(guard, "_persist_projection", corrupt_before_refresh)
    with pytest.raises(StateError, match="Unable to read cost guard projection"):
        getattr(guard, operation)(0.25)

    assert guard.state_path.read_bytes() == corrupted
    state = guard.current_state()
    assert (state.spent_usd, state.reserved_usd, state.ai_calls) == pytest.approx((0.0, 0.1, 0))
    with sqlite3.connect(guard.ledger_path) as connection:
        rows = connection.execute(
            "SELECT reservation_id, reserved_usd, status, actual_usd "
            "FROM ai_cost_reservations ORDER BY reservation_id"
        ).fetchall()
        assert [row[1:] for row in rows] == [
            (0.1, "reserved", None),
            (0.25, "settled", 0.0),
        ]
        assert rows[0][0] == active.reservation_id
        assert connection.execute(
            "SELECT reservation_id FROM ai_cost_non_dispatch_releases"
        ).fetchall() == [(rows[1][0],)]


@pytest.mark.parametrize("operation", ["reserve", "reserve_call"])
@pytest.mark.parametrize("error_type", [StateError, OSError, UnicodeError])
def test_cost_reservation_projection_failure_release_expected_errors(
    tmp_path, monkeypatch, operation, error_type,
) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="failure-release", budget_usd=1.0, programs_root=tmp_path)
    active = guard.reserve_call(0.2)
    projection_before = guard.state_path.read_bytes()
    failure = error_type("synthetic projection failure")

    def fail_projection(_state):
        raise failure

    monkeypatch.setattr(guard, "_persist_projection", fail_projection)
    with pytest.raises(error_type) as caught:
        getattr(guard, operation)(0.25)

    assert caught.value is failure
    state = guard.current_state()
    assert (state.spent_usd, state.reserved_usd, state.ai_calls) == pytest.approx((0.0, 0.2, 0))
    assert guard.state_path.read_bytes() == projection_before
    with sqlite3.connect(guard.ledger_path) as connection:
        rows = connection.execute(
            "SELECT reservation_id, status, actual_usd FROM ai_cost_reservations ORDER BY reservation_id"
        ).fetchall()
        assert rows[0] == (active.reservation_id, "reserved", None)
        assert rows[1][1:] == ("settled", 0.0)
        assert connection.execute(
            "SELECT reservation_id FROM ai_cost_non_dispatch_releases"
        ).fetchall() == [(rows[1][0],)]


def test_cost_reservation_projection_failure_release_after_json_replace(
    tmp_path, monkeypatch,
) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="after-replace", budget_usd=1.0, programs_root=tmp_path)
    active = guard.reserve_call(0.15)
    persist_projection = guard._persist_projection

    def fail_after_replace(state):
        persist_projection(state)
        raise OSError("synthetic failure after replacement")

    with monkeypatch.context() as patch:
        patch.setattr(guard, "_persist_projection", fail_after_replace)
        with pytest.raises(OSError, match="after replacement"):
            guard.reserve_call(0.25)

    assert guard.current_state().reserved_usd == pytest.approx(0.15)
    payload = json.loads(guard.state_path.read_text(encoding="utf-8"))
    assert payload["runs"]["after-replace"]["reserved_usd"] == pytest.approx(0.4)
    with sqlite3.connect(guard.ledger_path) as connection:
        rows = connection.execute(
            "SELECT reservation_id, status, actual_usd FROM ai_cost_reservations ORDER BY reservation_id"
        ).fetchall()
        assert rows[0] == (active.reservation_id, "reserved", None)
        assert rows[1][1:] == ("settled", 0.0)
        assert connection.execute(
            "SELECT reservation_id FROM ai_cost_non_dispatch_releases"
        ).fetchall() == [(rows[1][0],)]

    guard.reserve_call(0.05)
    payload = json.loads(guard.state_path.read_text(encoding="utf-8"))
    assert payload["runs"]["after-replace"]["reserved_usd"] == pytest.approx(0.2)


def test_cost_reservation_projection_failure_release_surfaces_cleanup_failure(
    tmp_path, monkeypatch,
) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="release-failure", budget_usd=1.0, programs_root=tmp_path)
    guard.record_actual(0.0, ai_calls=0)
    projection_before = guard.state_path.read_bytes()
    with sqlite3.connect(guard.ledger_path) as connection:
        connection.execute(
            "CREATE TABLE ai_cost_non_dispatch_releases "
            "(reservation_id INTEGER PRIMARY KEY, released_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TRIGGER reject_release BEFORE INSERT ON ai_cost_non_dispatch_releases "
            "BEGIN SELECT RAISE(ABORT, 'synthetic marker failure'); END"
        )

    def fail_projection(_state):
        raise OSError("synthetic private projection failure")

    monkeypatch.setattr(guard, "_persist_projection", fail_projection)
    with pytest.raises(StateError, match="release.*reconcile") as caught:
        guard.reserve_call(0.25)

    assert "private" not in "".join(traceback.format_exception(caught.value))
    assert guard.current_state().reserved_usd == pytest.approx(0.25)
    assert guard.state_path.read_bytes() == projection_before
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT status, actual_usd FROM ai_cost_reservations"
        ).fetchall() == [("reserved", None)]
        assert connection.execute("SELECT COUNT(*) FROM ai_cost_non_dispatch_releases").fetchone() == (0,)


def test_cost_reservation_projection_failure_release_reports_uncertain_cleanup_outcome(
    tmp_path, monkeypatch,
) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="uncertain-release", budget_usd=1.0, programs_root=tmp_path)
    finish_call = guard._finish_call

    def fail_projection(_state):
        raise OSError("synthetic projection failure")

    def fail_after_cleanup(*args, **kwargs):
        finish_call(*args, **kwargs)
        raise StateError("synthetic private cleanup confirmation failure")

    monkeypatch.setattr(guard, "_persist_projection", fail_projection)
    monkeypatch.setattr(guard, "_finish_call", fail_after_cleanup)
    with pytest.raises(StateError, match="release.*reconcile") as caught:
        guard.reserve_call(0.25)

    assert "private" not in "".join(traceback.format_exception(caught.value))
    assert guard.current_state().reserved_usd == pytest.approx(0.0)
    with sqlite3.connect(guard.ledger_path) as connection:
        rows = connection.execute(
            "SELECT reservation_id, status, actual_usd FROM ai_cost_reservations"
        ).fetchall()
        assert rows[0][1:] == ("settled", 0.0)
        assert connection.execute(
            "SELECT reservation_id FROM ai_cost_non_dispatch_releases"
        ).fetchall() == [(rows[0][0],)]


def test_cost_reservation_projection_failure_release_preserves_unexpected_cleanup_error(
    tmp_path, monkeypatch,
) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="unexpected-release", budget_usd=1.0, programs_root=tmp_path)

    def fail_projection(_state):
        raise OSError("synthetic projection failure")

    def fail_cleanup(*_args, **_kwargs):
        raise RuntimeError("unexpected cleanup error")

    monkeypatch.setattr(guard, "_persist_projection", fail_projection)
    monkeypatch.setattr(guard, "_finish_call", fail_cleanup)
    with pytest.raises(RuntimeError, match="unexpected cleanup error"):
        guard.reserve_call(0.25)

    assert guard.current_state().reserved_usd == pytest.approx(0.25)
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT status, actual_usd FROM ai_cost_reservations"
        ).fetchall() == [("reserved", None)]


def test_cost_reservation_projection_failure_release_rejects_equal_legacy_race(
    tmp_path, monkeypatch,
) -> None:
    first = CostGuard(edition=EDITION_NAME, run_id="legacy-race", budget_usd=1.0, programs_root=tmp_path)
    second = CostGuard(edition=EDITION_NAME, run_id="legacy-race", budget_usd=1.0, programs_root=tmp_path)
    entered, finish = Event(), Event()

    def paused_projection(_state):
        entered.set()
        assert finish.wait(timeout=10)
        raise OSError("synthetic projection failure")

    monkeypatch.setattr(first, "_persist_projection", paused_projection)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(first.reserve_call, 0.2)
        try:
            assert entered.wait(timeout=10)
            second.reserve(0.2)
            with pytest.raises(StateError, match="ambiguous"):
                second.settle_reservation(0.2, 0.07)
        finally:
            finish.set()
        with pytest.raises(OSError, match="synthetic projection failure"):
            pending.result(timeout=10)

    state = second.current_state()
    assert (state.spent_usd, state.reserved_usd, state.ai_calls) == pytest.approx((0.0, 0.2, 0))
    projection = json.loads(second.state_path.read_text(encoding="utf-8"))
    assert projection["runs"]["legacy-race"]["reserved_usd"] == pytest.approx(0.4)
    with sqlite3.connect(first.ledger_path) as connection:
        assert connection.execute(
            "SELECT status, actual_usd FROM ai_cost_reservations ORDER BY reservation_id"
        ).fetchall() == [("settled", 0.0), ("reserved", None)]
        assert connection.execute(
            "SELECT reservation_id FROM ai_cost_non_dispatch_releases"
        ).fetchall() == [(1,)]
    settled = second.settle_reservation(0.2, 0.07)
    assert (settled.spent_usd, settled.reserved_usd, settled.ai_calls) == pytest.approx((0.07, 0.0, 1))


@pytest.mark.parametrize("failure", [ValueError("unexpected failure"), KeyboardInterrupt()])
def test_cost_reservation_projection_failure_release_keeps_other_failures(
    tmp_path, monkeypatch, failure,
) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="other-failure", budget_usd=1.0, programs_root=tmp_path)

    def fail_projection(_state):
        raise failure

    monkeypatch.setattr(guard, "_persist_projection", fail_projection)
    with pytest.raises(type(failure)):
        guard.reserve_call(0.25)

    assert guard.current_state().reserved_usd == pytest.approx(0.25)
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT status, actual_usd FROM ai_cost_reservations"
        ).fetchall() == [("reserved", None)]
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'ai_cost_non_dispatch_releases'"
        ).fetchone() == (0,)


def test_cost_projection_recreates_absent_file_from_ledger(tmp_path) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="projection-current", budget_usd=1.0, programs_root=tmp_path)
    guard.record_actual(0.0, ai_calls=0)
    guard.state_path.unlink()

    guard.record_actual(0.1)

    projection = json.loads(guard.state_path.read_text(encoding="utf-8"))
    assert projection["runs"]["projection-current"]["spent_usd"] == pytest.approx(0.1)


def test_cost_projection_preserves_legacy_only_run_during_refresh(tmp_path) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="projection-current", budget_usd=1.0, programs_root=tmp_path)
    guard.record_actual(0.0, ai_calls=0)
    projection = json.loads(guard.state_path.read_text(encoding="utf-8"))
    legacy = {
        "budget_usd": 0.5,
        "spent_usd": 0.1,
        "ai_calls": 1,
        "started_at": "2026-05-16T00:00:00+00:00",
        "updated_at": "2026-05-16T00:05:00+00:00",
    }
    projection["runs"]["legacy-only"] = legacy
    guard.state_path.write_text(json.dumps(projection), encoding="utf-8")

    guard.record_actual(0.1)

    updated = json.loads(guard.state_path.read_text(encoding="utf-8"))
    assert updated["runs"]["legacy-only"] == legacy
    assert updated["runs"]["projection-current"]["spent_usd"] == pytest.approx(0.1)


@pytest.mark.parametrize(
    "amount",
    [-0.1, float("nan"), float("inf"), float("-inf"), True, "bad", 10**400],
    ids=["negative", "nan", "infinity", "negative-infinity", "boolean", "nonnumeric", "overflow"],
)
@pytest.mark.parametrize(
    "operation",
    ["check", "reserve", "reserve_call", "record", "record_actual", "settle_reserved", "settle_actual", "settle_call"],
)
def test_cost_guard_rejects_invalid_live_amounts_without_mutation(tmp_path, amount, operation) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="invalid-live", budget_usd=1.0, programs_root=tmp_path)
    guard.record_actual(0.25, ai_calls=0)
    reservation = guard.reserve_call(0.1)
    previous_state = guard.current_state()
    previous_projection = guard.state_path.read_bytes()
    with sqlite3.connect(guard.ledger_path) as connection:
        previous_reservations = connection.execute(
            "SELECT reservation_id, reserved_usd, actual_usd, status FROM ai_cost_reservations",
        ).fetchall()

    calls = {
        "check": lambda: guard.check(amount),
        "reserve": lambda: guard.reserve(amount),
        "reserve_call": lambda: guard.reserve_call(amount),
        "record": lambda: guard.record(amount),
        "record_actual": lambda: guard.record_actual(amount),
        "settle_reserved": lambda: guard.settle_reservation(amount, 0.1),
        "settle_actual": lambda: guard.settle_reservation(0.1, amount),
        "settle_call": lambda: guard.settle_call(reservation, amount),
    }
    with pytest.raises(StateError, match="Invalid cost guard"):
        calls[operation]()

    assert guard.current_state() == previous_state
    assert guard.state_path.read_bytes() == previous_projection
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT reservation_id, reserved_usd, actual_usd, status FROM ai_cost_reservations",
        ).fetchall() == previous_reservations


@pytest.mark.parametrize("operation", ["record_actual", "settle_reservation", "settle_call"])
def test_cost_guard_rejects_live_spend_overflow_without_mutation(tmp_path, operation) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="overflow-live", budget_usd=1e308, programs_root=tmp_path)
    reservation = guard.reserve_call(0.1)
    guard.record_actual(1e308, ai_calls=0)
    previous_state = guard.current_state()
    previous_projection = guard.state_path.read_bytes()
    with sqlite3.connect(guard.ledger_path) as connection:
        previous_reservations = connection.execute(
            "SELECT reservation_id, reserved_usd, actual_usd, status FROM ai_cost_reservations",
        ).fetchall()

    calls = {
        "record_actual": lambda: guard.record_actual(1e308),
        "settle_reservation": lambda: guard.settle_reservation(0.1, 1e308),
        "settle_call": lambda: guard.settle_call(reservation, 1e308),
    }
    with pytest.raises(StateError, match="Invalid cost guard"):
        calls[operation]()

    assert guard.current_state() == previous_state
    assert guard.state_path.read_bytes() == previous_projection
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT reservation_id, reserved_usd, actual_usd, status FROM ai_cost_reservations",
        ).fetchall() == previous_reservations


@pytest.mark.parametrize(
    "count",
    [True, False, -1, 1.5, float("nan"), float("inf"), "1", "private-call-count", None, 2**63],
    ids=["true", "false", "negative", "fractional", "nan", "infinity", "string", "private", "none", "overflow"],
)
@pytest.mark.parametrize("operation", ["record", "record_actual", "settle_call", "settle_matched", "settle_legacy"])
def test_cost_guard_rejects_invalid_live_call_count_without_mutation(tmp_path, count, operation) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="invalid-call-count", budget_usd=1.0, programs_root=tmp_path)
    reservation = guard.reserve_call(0.25)
    if operation == "settle_legacy":
        with sqlite3.connect(guard.ledger_path) as connection:
            connection.execute("DELETE FROM ai_cost_reservations WHERE reservation_id = ?", (reservation.reservation_id,))
    previous_state = guard.current_state()
    previous_projection = guard.state_path.read_bytes()
    with sqlite3.connect(guard.ledger_path) as connection:
        previous_reservations = connection.execute(
            "SELECT reservation_id, reserved_usd, actual_usd, status FROM ai_cost_reservations",
        ).fetchall()
    calls = {
        "record": lambda: guard.record(0.1, ai_calls=count),
        "record_actual": lambda: guard.record_actual(0.1, ai_calls=count),
        "settle_call": lambda: guard.settle_call(reservation, 0.1, ai_calls=count),
        "settle_matched": lambda: guard.settle_reservation(0.25, 0.1, ai_calls=count),
        "settle_legacy": lambda: guard.settle_reservation(0.25, 0.1, ai_calls=count),
    }

    with pytest.raises(StateError, match="Invalid cost guard ai_calls value") as error:
        calls[operation]()

    assert "private-call-count" not in str(error.value)
    assert guard.current_state() == previous_state
    assert guard.state_path.read_bytes() == previous_projection
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT reservation_id, reserved_usd, actual_usd, status FROM ai_cost_reservations",
        ).fetchall() == previous_reservations


@pytest.mark.parametrize("operation", ["record_actual", "settle_call", "settle_matched", "settle_legacy"])
def test_cost_guard_rejects_call_count_overflow_without_mutation(tmp_path, operation) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="overflow-call-count", budget_usd=1.0, programs_root=tmp_path)
    guard.record_actual(0.0, ai_calls=2**63 - 1)
    reservation = guard.reserve_call(0.25)
    if operation == "settle_legacy":
        with sqlite3.connect(guard.ledger_path) as connection:
            connection.execute("DELETE FROM ai_cost_reservations WHERE reservation_id = ?", (reservation.reservation_id,))
    previous_state = guard.current_state()
    previous_projection = guard.state_path.read_bytes()
    with sqlite3.connect(guard.ledger_path) as connection:
        previous_reservations = connection.execute(
            "SELECT reservation_id, reserved_usd, actual_usd, status FROM ai_cost_reservations",
        ).fetchall()
    calls = {
        "record_actual": lambda: guard.record_actual(0.1),
        "settle_call": lambda: guard.settle_call(reservation, 0.1),
        "settle_matched": lambda: guard.settle_reservation(0.25, 0.1),
        "settle_legacy": lambda: guard.settle_reservation(0.25, 0.1),
    }

    with pytest.raises(StateError, match="Invalid cost guard ai_calls value"):
        calls[operation]()

    assert guard.current_state() == previous_state
    assert guard.state_path.read_bytes() == previous_projection
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT reservation_id, reserved_usd, actual_usd, status FROM ai_cost_reservations",
        ).fetchall() == previous_reservations


@pytest.mark.parametrize(
    "count",
    [True, -1, 1.5, float("nan"), float("inf"), "private-call-count", str(2**63)],
    ids=["boolean", "negative", "fractional", "nan", "infinity", "private", "overflow"],
)
@pytest.mark.parametrize("storage", ["sqlite", "legacy_json"])
def test_cost_guard_rejects_invalid_retained_call_count_without_mutation(tmp_path, count, storage) -> None:
    if storage == "sqlite" and (isinstance(count, bool) or isinstance(count, float) and math.isnan(count)):
        pytest.skip("SQLite stores booleans as integers and rejects NaN in this non-null column")
    guard = CostGuard(edition=EDITION_NAME, run_id="retained-call-count", budget_usd=1.0, programs_root=tmp_path)
    if storage == "sqlite":
        guard.record_actual(0.0, ai_calls=0)
        with sqlite3.connect(guard.ledger_path) as connection:
            connection.execute("UPDATE ai_cost_runs SET ai_calls = ?", (count,))
    else:
        guard.state_path.parent.mkdir(parents=True)
        guard.state_path.write_text(json.dumps({
            "edition": EDITION_NAME,
            "runs": {
                "retained-call-count": {
                    "budget_usd": 1.0, "spent_usd": 0.0, "reserved_usd": 0.0, "ai_calls": count,
                    "started_at": "2026-05-16T00:00:00+00:00", "updated_at": "2026-05-16T00:00:00+00:00",
                },
            },
        }), encoding="utf-8")
    previous_projection = guard.state_path.read_bytes()

    with pytest.raises(StateError, match="Invalid cost guard ai_calls value") as error:
        guard.current_state()
    with pytest.raises(StateError, match="Invalid cost guard ai_calls value"):
        load_run_states(EDITION_NAME, programs_root=tmp_path)

    assert "private-call-count" not in str(error.value)
    assert guard.state_path.read_bytes() == previous_projection
    if storage == "sqlite":
        with sqlite3.connect(guard.ledger_path) as connection:
            retained = connection.execute("SELECT ai_calls FROM ai_cost_runs").fetchone()[0]
        if count == str(2**63):
            assert retained == float(2**63)
        else:
            assert retained == count


@pytest.mark.parametrize("count", [-1, 1.5, float("inf"), "private-call-count"])
def test_cost_guard_rejects_retained_call_count_before_settlement(tmp_path, count) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="invalid-count-settlement", budget_usd=1.0, programs_root=tmp_path)
    reservation = guard.reserve_call(0.25)
    with sqlite3.connect(guard.ledger_path) as connection:
        connection.execute("UPDATE ai_cost_runs SET ai_calls = ?", (count,))
    previous_projection = guard.state_path.read_bytes()

    with pytest.raises(StateError, match="Invalid cost guard ai_calls value"):
        guard.settle_call(reservation, 0.1)

    assert guard.state_path.read_bytes() == previous_projection
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute("SELECT ai_calls FROM ai_cost_runs").fetchone() == (count,)
        assert connection.execute("SELECT status FROM ai_cost_reservations").fetchall() == [("reserved",)]


@pytest.mark.parametrize("count", [0, 2])
@pytest.mark.parametrize("operation", ["record", "record_actual", "settle_call", "settle_matched", "settle_legacy"])
def test_cost_guard_accepts_live_call_count_without_truncation(tmp_path, count, operation) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="valid-live-call-count", budget_usd=1.0, programs_root=tmp_path)
    reservation = guard.reserve_call(0.25)
    if operation == "settle_legacy":
        with sqlite3.connect(guard.ledger_path) as connection:
            connection.execute("DELETE FROM ai_cost_reservations WHERE reservation_id = ?", (reservation.reservation_id,))
    calls = {
        "record": lambda: guard.record(0.1, ai_calls=count),
        "record_actual": lambda: guard.record_actual(0.1, ai_calls=count),
        "settle_call": lambda: guard.settle_call(reservation, 0.1, ai_calls=count),
        "settle_matched": lambda: guard.settle_reservation(0.25, 0.1, ai_calls=count),
        "settle_legacy": lambda: guard.settle_reservation(0.25, 0.1, ai_calls=count),
    }

    state = calls[operation]()

    assert state.ai_calls == count
    assert guard.current_state().ai_calls == count
    assert json.loads(guard.state_path.read_text(encoding="utf-8"))["runs"]["valid-live-call-count"]["ai_calls"] == count


@pytest.mark.parametrize("count", [0, 2, "2", 2.0])
def test_cost_guard_preserves_valid_legacy_call_count_representations(tmp_path, count) -> None:
    guard = CostGuard(edition=EDITION_NAME, run_id="valid-call-count", budget_usd=1.0, programs_root=tmp_path)
    guard.state_path.parent.mkdir(parents=True)
    guard.state_path.write_text(json.dumps({
        "edition": EDITION_NAME,
        "runs": {
            "valid-call-count": {
                "budget_usd": 1.0, "spent_usd": 0.0, "reserved_usd": 0.0, "ai_calls": count,
                "started_at": "2026-05-16T00:00:00+00:00", "updated_at": "2026-05-16T00:00:00+00:00",
            },
        },
    }), encoding="utf-8")
    assert guard.current_state().ai_calls == int(count)


def test_cost_guard_isolated_by_run_id(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    first_run = CostGuard(edition=EDITION_NAME, run_id="run-001", budget_usd=0.5, programs_root=programs_root)
    second_run = CostGuard(edition=EDITION_NAME, run_id="run-002", budget_usd=0.5, programs_root=programs_root)

    first_run.record(0.3)

    assert first_run.current_state().spent_usd == pytest.approx(0.3)
    assert second_run.current_state().spent_usd == 0.0
    second_run.check(0.5)


def test_load_latest_run_state_returns_most_recent_run(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    first_run = CostGuard(edition=EDITION_NAME, run_id="run-001", budget_usd=0.5, programs_root=programs_root)
    second_run = CostGuard(edition=EDITION_NAME, run_id="run-002", budget_usd=0.5, programs_root=programs_root)

    first_run.record_actual(0.2)
    second_run.record_actual(0.6)

    latest = load_latest_run_state(EDITION_NAME, programs_root=programs_root)

    assert latest is not None
    assert latest.run_id == "run-002"
    assert latest.within_budget is False


@pytest.mark.parametrize("storage", ["sqlite", "json"])
@pytest.mark.parametrize(
    ("earlier", "later"),
    [
        ("2026-11-10T00:30:00+00:00", "2026-11-09T22:00:00-05:00"),
        ("2026-11-10T05:30:00+05:00", "2026-11-10T01:00:00+00:00"),
        ("2026-11-10T03:00:00+00:00", "2026-11-09T22:00:00-05:00"),
        ("2026-11-10T03:00:00Z", "2026-11-10T03:00:00.000001+00:00"),
        ("2026-11-10T00:30:00", "2026-11-09T22:00:00-05:00"),
        ("2026-11-10T00:29:00+00:00", "2026-11-10T00:30:00+00:00"),
        ("2026-11-10T03:00:00+00:00", "2026-11-10T03:00:00+00:00"),
        ("0001-01-01T00:00:00+00:00", "0001-01-01T00:00:00.000001+00:00"),
        ("9999-12-31T23:59:59.999998+00:00", "9999-12-31T23:59:59.999999+00:00"),
    ],
    ids=[
        "negative-offset", "positive-offset", "equal-instant", "fractional", "naive",
        "utc", "equal-utc", "minimum-utc", "maximum-utc",
    ],
)
def test_cost_run_timestamp_order_matches_normalized_instant(
    tmp_path: Path, storage: str, earlier: str, later: str,
) -> None:
    programs_root = tmp_path / "programs"
    guard = CostGuard(edition=EDITION_NAME, run_id="a-run", budget_usd=0.5, programs_root=programs_root)
    if storage == "sqlite":
        guard.record_actual(0.1)
        CostGuard(edition=EDITION_NAME, run_id="z-run", budget_usd=0.5, programs_root=programs_root).record_actual(0.6)
        with sqlite3.connect(guard.ledger_path) as connection:
            connection.executemany(
                "UPDATE ai_cost_runs SET updated_at = ? WHERE edition = ? AND run_id = ?",
                [(earlier, EDITION_NAME, "a-run"), (later, EDITION_NAME, "z-run")],
            )
    else:
        guard.state_path.parent.mkdir(parents=True)
        guard.state_path.write_text(
            json.dumps({
                "edition": EDITION_NAME,
                "runs": {
                    run_id: {
                        "budget_usd": 0.5,
                        "spent_usd": spend,
                        "reserved_usd": 0.0,
                        "ai_calls": 1,
                        "started_at": "2026-11-08T00:00:00+00:00",
                        "updated_at": timestamp,
                    }
                    for run_id, spend, timestamp in (
                        ("a-run", 0.1, earlier), ("z-run", 0.6, later),
                    )
                },
            }),
            encoding="utf-8",
        )
    before = {
        path: path.read_bytes()
        for path in (guard.state_path, guard.ledger_path)
        if path.exists()
    }

    states = load_run_states(EDITION_NAME, programs_root=programs_root)
    latest = load_latest_run_state(EDITION_NAME, programs_root=programs_root)

    assert [state.run_id for state in states] == ["z-run", "a-run"]
    assert all(state.updated_at.tzinfo == timezone.utc for state in states)
    assert latest is not None
    assert latest == states[0]
    assert not latest.within_budget
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("field_name", ["started_at", "updated_at"])
@pytest.mark.parametrize(
    "invalid_value",
    ["private-time", "", "0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59.999999-01:00"],
    ids=["invalid-text", "empty", "utc-underflow", "utc-overflow"],
)
@pytest.mark.parametrize("invalid_run", ["a-run", "z-run"])
@pytest.mark.parametrize("storage", ["sqlite", "json"])
def test_cost_run_timestamp_order_refuses_invalid_retained_values(
    tmp_path: Path, field_name: str, invalid_value: str, invalid_run: str, storage: str,
) -> None:
    programs_root = tmp_path / "programs"
    guard = CostGuard(edition=EDITION_NAME, run_id="a-run", budget_usd=0.5, programs_root=programs_root)
    if storage == "sqlite":
        guard.record_actual(0.1)
        CostGuard(edition=EDITION_NAME, run_id="z-run", budget_usd=0.5, programs_root=programs_root).record_actual(0.2)
        with sqlite3.connect(guard.ledger_path) as connection:
            connection.execute(
                f"UPDATE ai_cost_runs SET {field_name} = ? WHERE edition = ? AND run_id = ?",
                (invalid_value, EDITION_NAME, invalid_run),
            )
    else:
        guard.state_path.parent.mkdir(parents=True)
        runs = {
            run_id: {
                "budget_usd": 0.5,
                "spent_usd": 0.1,
                "reserved_usd": 0.0,
                "ai_calls": 1,
                "started_at": "2026-11-08T00:00:00+00:00",
                "updated_at": "2026-11-08T00:00:00+00:00",
            }
            for run_id in ("a-run", "z-run")
        }
        runs[invalid_run][field_name] = invalid_value
        guard.state_path.write_text(
            json.dumps({"edition": EDITION_NAME, "runs": runs}),
            encoding="utf-8",
        )
    before = {
        path: path.read_bytes()
        for path in (guard.state_path, guard.ledger_path)
        if path.exists()
    }

    for read in (load_run_states, load_latest_run_state):
        with pytest.raises(StateError, match=f"Invalid cost guard {field_name} value") as error:
            read(EDITION_NAME, programs_root=programs_root)
        if invalid_value:
            assert invalid_value not in str(error.value)
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("consumer", ["brief", "triage", "doctor"])
def test_cost_run_timestamp_order_reaches_diagnostic_consumers(tmp_path: Path, consumer: str) -> None:
    programs_root = tmp_path / "programs"
    edition_dir = programs_root / PROGRAM_ID / "editions"
    edition_dir.mkdir(parents=True)
    (edition_dir / f"{EDITION_NAME}.yaml").write_text(f"program_id: {PROGRAM_ID}\n", encoding="utf-8")
    older = CostGuard(edition=EDITION_NAME, run_id="older", budget_usd=0.5, programs_root=programs_root)
    newer = CostGuard(edition=EDITION_NAME, run_id="newer", budget_usd=0.5, programs_root=programs_root)
    older.record_actual(0.1)
    newer.record_actual(0.6)
    with sqlite3.connect(older.ledger_path) as connection:
        connection.executemany(
            "UPDATE ai_cost_runs SET updated_at = ? WHERE edition = ? AND run_id = ?",
            [
                ("2026-11-10T00:30:00+00:00", EDITION_NAME, "older"),
                ("2026-11-09T22:00:00-05:00", EDITION_NAME, "newer"),
            ],
        )
    before = {path: path.read_bytes() for path in (older.state_path, older.ledger_path)}
    message = (
        f"AI cost ceiling exceeded for {EDITION_NAME}: $0.600 / $0.50 "
        "across 1 AI call(s) (run newer)."
    )

    if consumer == "brief":
        from src.commands.brief import _build_cost_guard_brief_lines

        assert [line.text for line in _build_cost_guard_brief_lines(PROGRAM_ID, programs_root=programs_root)] == [message]
    elif consumer == "triage":
        from src.commands.triage import _load_cost_guard_integration_diagnostics

        assert _load_cost_guard_integration_diagnostics(
            edition_name=EDITION_NAME, programs_root=programs_root,
        ) == (message,)
    else:
        from src.commands.doctor_checks.storage_checks import _cost_ledger_storage_check

        check = _cost_ledger_storage_check(EDITION_NAME, programs_root=programs_root)
        assert check.metadata["latest_run_id"] == "newer"
        assert check.metadata["spent_usd"] == pytest.approx(0.6)
        assert "latest run newer" in check.detail
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("consumer", ["brief", "triage", "doctor"])
@pytest.mark.parametrize(
    "timestamp", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59.999999-01:00"],
    ids=["utc-underflow", "utc-overflow"],
)
def test_cost_run_timestamp_order_reports_unrepresentable_instants(
    tmp_path: Path, consumer: str, timestamp: str,
) -> None:
    programs_root = tmp_path / "programs"
    edition_dir = programs_root / PROGRAM_ID / "editions"
    edition_dir.mkdir(parents=True)
    (edition_dir / f"{EDITION_NAME}.yaml").write_text(f"program_id: {PROGRAM_ID}\n", encoding="utf-8")
    guard = CostGuard(edition=EDITION_NAME, run_id="run", budget_usd=0.5, programs_root=programs_root)
    guard.record_actual(0.1)
    with sqlite3.connect(guard.ledger_path) as connection:
        connection.execute("UPDATE ai_cost_runs SET updated_at = ?", (timestamp,))
    before = {path: path.read_bytes() for path in (guard.state_path, guard.ledger_path)}

    if consumer == "brief":
        from src.commands.brief import _build_cost_guard_brief_lines

        messages = tuple(line.text for line in _build_cost_guard_brief_lines(PROGRAM_ID, programs_root=programs_root))
    elif consumer == "triage":
        from src.commands.triage import _load_cost_guard_integration_diagnostics

        messages = _load_cost_guard_integration_diagnostics(edition_name=EDITION_NAME, programs_root=programs_root)
    else:
        from src.commands.doctor_checks.storage_checks import _cost_ledger_storage_check

        check = _cost_ledger_storage_check(EDITION_NAME, programs_root=programs_root)
        assert check.status == "fail"
        messages = (check.detail,)
    assert len(messages) == 1
    assert "Invalid cost guard updated_at value" in messages[0]
    assert timestamp not in messages[0]
    assert {path: path.read_bytes() for path in before} == before


def test_cost_guard_prefers_sqlite_authority_over_json_projection(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    guard = CostGuard(edition=EDITION_NAME, run_id="run-001", budget_usd=0.5, programs_root=programs_root)

    guard.record_actual(0.2)
    guard.state_path.write_text(
        json.dumps(
            {
                "edition": EDITION_NAME,
                "runs": {
                    "run-001": {
                        "budget_usd": 0.5,
                        "spent_usd": 99.0,
                        "ai_calls": 99,
                        "started_at": "2026-05-16T00:00:00+00:00",
                        "updated_at": "2026-05-16T00:05:00+00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    state = guard.current_state()

    assert state.spent_usd == pytest.approx(0.2)
    assert state.ai_calls == 1


def test_cost_guard_legacy_json_state_is_migrated_on_next_write(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    path = _guard_path(programs_root, "cost_guard.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "edition": EDITION_NAME,
                "runs": {
                    "run-001": {
                        "budget_usd": 0.5,
                        "spent_usd": 0.1,
                        "ai_calls": 1,
                        "started_at": "2026-05-16T00:00:00+00:00",
                        "updated_at": "2026-05-16T00:05:00+00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    guard = CostGuard(edition=EDITION_NAME, run_id="run-001", budget_usd=0.5, programs_root=programs_root)

    updated = guard.record_actual(0.15)

    assert updated.spent_usd == pytest.approx(0.25)
    assert guard.ledger_path.exists()

    reloaded = CostGuard(edition=EDITION_NAME, run_id="run-001", budget_usd=0.5, programs_root=programs_root).current_state()

    assert reloaded.spent_usd == pytest.approx(0.25)
    assert reloaded.ai_calls == 2


def test_cost_guard_rejects_invalid_persisted_payload(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    path = _guard_path(programs_root, "cost_guard.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"edition": "other", "runs": {}}), encoding="utf-8")

    guard = CostGuard(edition=EDITION_NAME, run_id="run-001", budget_usd=0.5, programs_root=programs_root)

    with pytest.raises(StateError, match="belongs to other"):
        guard.current_state()


def test_cost_guard_rejects_missing_persisted_top_level_fields(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    path = _guard_path(programs_root, "cost_guard.json")
    path.parent.mkdir(parents=True, exist_ok=True)

    missing_fields = (
        ("edition", "Cost guard payload must include edition"),
        ("runs", "Cost guard payload must include runs"),
    )

    for field_name, message in missing_fields:
        payload = {
            key: value
            for key, value in {
                "edition": EDITION_NAME,
                "runs": {},
            }.items()
            if key != field_name
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

        guard = CostGuard(edition=EDITION_NAME, run_id="run-001", budget_usd=0.5, programs_root=programs_root)

        with pytest.raises(StateError, match=message):
            guard.current_state()


def test_cost_guard_rejects_invalid_persisted_run_numeric_fields(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    path = _guard_path(programs_root, "cost_guard.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "edition": EDITION_NAME,
                "runs": {
                    "run-001": {
                        "budget_usd": 0.5,
                        "spent_usd": "abc",
                        "ai_calls": 1,
                        "started_at": "2026-05-16T00:00:00+00:00",
                        "updated_at": "2026-05-16T00:05:00+00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    guard = CostGuard(edition=EDITION_NAME, run_id="run-001", budget_usd=0.5, programs_root=programs_root)

    with pytest.raises(StateError, match="Invalid cost guard spent_usd value"):
        guard.current_state()


@pytest.mark.parametrize("storage", ["json", "sqlite"])
@pytest.mark.parametrize("field_name", ["budget_usd", "spent_usd", "reserved_usd"])
@pytest.mark.parametrize(
    "invalid_value",
    [-0.5, float("nan"), float("inf"), float("-inf"), 10**400, "private-state-value"],
    ids=["negative", "nan", "positive-infinity", "negative-infinity", "float-overflow", "invalid-text"],
)
@pytest.mark.parametrize("operation", ["read", "check", "reserve", "list"])
def test_cost_guard_rejects_invalid_retained_amounts_without_writing(
    tmp_path, storage: str, field_name: str, invalid_value: float | int | str, operation: str,
) -> None:
    programs_root = tmp_path / "programs"
    guard = CostGuard(edition=EDITION_NAME, run_id="run-001", budget_usd=1.0, programs_root=programs_root)
    if storage == "json":
        payload = {
            "edition": EDITION_NAME,
            "runs": {
                "run-001": {
                    "budget_usd": 1.0,
                    "spent_usd": 0.0,
                    "reserved_usd": 0.0,
                    "ai_calls": 0,
                    "started_at": "2026-05-16T00:00:00+00:00",
                    "updated_at": "2026-05-16T00:00:00+00:00",
                    field_name: invalid_value,
                },
            },
        }
        guard.state_path.parent.mkdir(parents=True, exist_ok=True)
        guard.state_path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        guard.record_actual(0.0)
        value = str(invalid_value) if invalid_value != invalid_value or isinstance(invalid_value, int) else invalid_value
        with sqlite3.connect(guard.ledger_path) as connection:
            connection.execute(
                f"UPDATE ai_cost_runs SET {field_name} = ? WHERE edition = ? AND run_id = ?",
                (value, EDITION_NAME, "run-001"),
            )
    before = {
        path: path.read_bytes()
        for path in (guard.state_path, guard.ledger_path)
        if path.exists()
    }

    with pytest.raises(StateError, match=f"Invalid cost guard {field_name} value") as error:
        if operation == "read":
            guard.current_state()
        elif operation == "check":
            guard.check(0.1)
        elif operation == "reserve":
            guard.reserve_call(0.1)
        else:
            load_run_states(EDITION_NAME, programs_root=programs_root)

    assert "private-state-value" not in str(error.value)
    assert {
        path: path.read_bytes()
        for path in (guard.state_path, guard.ledger_path)
        if path.exists()
    } == before


@pytest.mark.parametrize("storage", ["json", "sqlite"])
def test_cost_guard_preserves_retained_zero_budget(
    tmp_path, storage: str,
) -> None:
    programs_root = tmp_path / "programs"
    original = CostGuard(edition=EDITION_NAME, run_id="run-001", budget_usd=0.0, programs_root=programs_root)
    if storage == "json":
        original.state_path.parent.mkdir(parents=True, exist_ok=True)
        original.state_path.write_text(
            json.dumps({
                "edition": EDITION_NAME,
                "runs": {
                    "run-001": {
                        "budget_usd": 0.0,
                        "spent_usd": 0.0,
                        "reserved_usd": 0.0,
                        "ai_calls": 0,
                        "started_at": "2026-05-16T00:00:00+00:00",
                        "updated_at": "2026-05-16T00:00:00+00:00",
                    },
                },
            }),
            encoding="utf-8",
        )
    else:
        original.record_actual(0.0)
    reloaded = CostGuard(edition=EDITION_NAME, run_id="run-001", budget_usd=1.0, programs_root=programs_root)

    assert reloaded.current_state().budget_usd == 0.0
    assert load_run_states(EDITION_NAME, programs_root=programs_root)[0].budget_usd == 0.0
    with pytest.raises(BudgetExceeded):
        reloaded.reserve_call(0.01)


@pytest.mark.parametrize("field_name", ["reservation", "budget_usd", "spent_usd", "reserved_usd"])
@pytest.mark.parametrize(
    "invalid_value",
    [-0.5, float("nan"), float("inf"), "private-state-value"],
    ids=["negative", "nan", "positive-infinity", "invalid-text"],
)
def test_cost_guard_rejects_invalid_retained_amounts_before_settlement_without_writing(
    tmp_path, field_name: str, invalid_value: float | str,
) -> None:
    programs_root = tmp_path / "programs"
    guard = CostGuard(edition=EDITION_NAME, run_id="run-001", budget_usd=1.0, programs_root=programs_root)
    reservation = guard.reserve_call(0.3)
    value = str(invalid_value) if invalid_value != invalid_value else invalid_value
    with sqlite3.connect(guard.ledger_path) as connection:
        if field_name == "reservation":
            connection.execute(
                "UPDATE ai_cost_reservations SET reserved_usd = ? WHERE reservation_id = ?",
                (value, reservation.reservation_id),
            )
        else:
            connection.execute(
                f"UPDATE ai_cost_runs SET {field_name} = ? WHERE edition = ? AND run_id = ?",
                (value, EDITION_NAME, "run-001"),
            )
    before = {path: path.read_bytes() for path in (guard.state_path, guard.ledger_path)}

    invalid_field = "reserved_usd" if field_name == "reservation" else field_name
    with pytest.raises(StateError, match=f"Invalid cost guard {invalid_field} value") as error:
        guard.settle_call(reservation, 0.1)

    assert "private-state-value" not in str(error.value)
    assert {path: path.read_bytes() for path in (guard.state_path, guard.ledger_path)} == before


def test_cost_guard_rejects_missing_persisted_run_numeric_fields(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    path = _guard_path(programs_root, "cost_guard.json")
    path.parent.mkdir(parents=True, exist_ok=True)

    missing_fields = (
        ("budget_usd", "Cost guard payload must include budget_usd"),
        ("spent_usd", "Cost guard payload must include spent_usd"),
        ("ai_calls", "Cost guard payload must include ai_calls"),
    )

    for field_name, message in missing_fields:
        payload = {
            "edition": EDITION_NAME,
            "runs": {
                "run-001": {
                    key: value
                    for key, value in {
                        "budget_usd": 0.5,
                        "spent_usd": 0.1,
                        "ai_calls": 1,
                        "started_at": "2026-05-16T00:00:00+00:00",
                        "updated_at": "2026-05-16T00:05:00+00:00",
                    }.items()
                    if key != field_name
                },
            },
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

        guard = CostGuard(edition=EDITION_NAME, run_id="run-001", budget_usd=0.5, programs_root=programs_root)

        with pytest.raises(StateError, match=message):
            guard.current_state()


def test_cost_guard_rejects_missing_persisted_run_datetime_fields(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    path = _guard_path(programs_root, "cost_guard.json")
    path.parent.mkdir(parents=True, exist_ok=True)

    missing_fields = (
        ("started_at", "Cost guard payload must include started_at"),
        ("updated_at", "Cost guard payload must include updated_at"),
    )

    for field_name, message in missing_fields:
        payload = {
            "edition": EDITION_NAME,
            "runs": {
                "run-001": {
                    key: value
                    for key, value in {
                        "budget_usd": 0.5,
                        "spent_usd": 0.1,
                        "ai_calls": 1,
                        "started_at": "2026-05-16T00:00:00+00:00",
                        "updated_at": "2026-05-16T00:05:00+00:00",
                    }.items()
                    if key != field_name
                },
            },
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

        guard = CostGuard(edition=EDITION_NAME, run_id="run-001", budget_usd=0.5, programs_root=programs_root)

        with pytest.raises(StateError, match=message):
            guard.current_state()


def test_cost_guard_rejects_invalid_persisted_run_datetime_fields(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    path = _guard_path(programs_root, "cost_guard.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "edition": EDITION_NAME,
                "runs": {
                    "run-001": {
                        "budget_usd": 0.5,
                        "spent_usd": 0.1,
                        "ai_calls": 1,
                        "started_at": "not-a-datetime",
                        "updated_at": "2026-05-16T00:05:00+00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    guard = CostGuard(edition=EDITION_NAME, run_id="run-001", budget_usd=0.5, programs_root=programs_root)

    with pytest.raises(StateError, match="Invalid cost guard started_at value"):
        guard.current_state()


def test_cost_guard_rejects_boolean_numeric_fields(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    path = _guard_path(programs_root, "cost_guard.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "edition": EDITION_NAME,
                "runs": {
                    "run-001": {
                        "budget_usd": True,
                        "spent_usd": 0.1,
                        "ai_calls": 1,
                        "started_at": "2026-05-16T00:00:00+00:00",
                        "updated_at": "2026-05-16T00:05:00+00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    guard = CostGuard(edition=EDITION_NAME, run_id="run-001", budget_usd=0.5, programs_root=programs_root)

    with pytest.raises(StateError, match="Invalid cost guard budget_usd value"):
        guard.current_state()


def test_cost_guard_rejects_non_object_run_payload(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    path = _guard_path(programs_root, "cost_guard.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "edition": EDITION_NAME,
                "runs": {
                    "run-001": "corrupt-state",
                },
            }
        ),
        encoding="utf-8",
    )

    guard = CostGuard(edition=EDITION_NAME, run_id="run-001", budget_usd=0.5, programs_root=programs_root)

    with pytest.raises(StateError, match="Invalid cost guard run payload"):
        guard.current_state()
