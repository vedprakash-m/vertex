from __future__ import annotations

import json
import sqlite3

import pytest

from src.ai.client import BudgetExceeded
from src.ai.cost_guard import CostGuard, load_latest_run_state
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


def test_cost_guard_blocks_when_run_budget_would_be_exceeded(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    guard = CostGuard(edition=EDITION_NAME, run_id="run-001", budget_usd=0.5, programs_root=programs_root)

    guard.record(0.4)

    with pytest.raises(BudgetExceeded, match="run ceiling"):
        guard.check(0.11)

    with pytest.raises(BudgetExceeded, match="run ceiling"):
        guard.record(0.11)


def test_cost_guard_record_actual_persists_overspend(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    guard = CostGuard(edition=EDITION_NAME, run_id="run-001", budget_usd=0.5, programs_root=programs_root)

    updated_state = guard.record_actual(0.6)

    assert updated_state.spent_usd == pytest.approx(0.6)
    assert updated_state.ai_calls == 1
    assert guard.current_state().spent_usd == pytest.approx(0.6)


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
