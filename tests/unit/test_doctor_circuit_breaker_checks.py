from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.commands.doctor_checks.circuit_breaker_checks import (
    default_breaker_snapshot,
    describe_circuit_breaker_snapshot,
    display_path,
    run_circuit_breaker_doctor,
)
from src.core.circuit_breaker import BreakerSnapshot, CircuitBreakerState


def test_default_breaker_snapshot_is_closed() -> None:
    snapshot = default_breaker_snapshot()

    assert snapshot == BreakerSnapshot(
        state=CircuitBreakerState.CLOSED,
        failure_count=0,
        last_failure_at=None,
        last_opened_at=None,
        last_success_at=None,
    )


def test_display_path_prefers_output_root_relative_path(tmp_path: Path) -> None:
    path = (tmp_path / "programs" / "acme" / "publications") / "demo_weekly" / ".ado_breaker.json"

    assert display_path(path, programs_root=(tmp_path / "programs"), repo_root=tmp_path) == "publications/demo_weekly/.ado_breaker.json"


def test_display_path_falls_back_to_repo_relative_path(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    path = repo_root / "reports" / "demo_weekly" / ".ado_breaker.json"

    assert display_path(path, repo_root=repo_root) == "reports/demo_weekly/.ado_breaker.json"


def test_describe_circuit_breaker_snapshot_reports_absent_state() -> None:
    detail = describe_circuit_breaker_snapshot(
        default_breaker_snapshot(),
        path_label="output/demo_weekly/.ado_breaker.json",
        state_exists=False,
    )

    assert detail == "output/demo_weekly/.ado_breaker.json is absent; effective ADO breaker state is CLOSED."


def test_describe_circuit_breaker_snapshot_appends_open_state_suffix() -> None:
    snapshot = BreakerSnapshot(
        state=CircuitBreakerState.OPEN,
        failure_count=3,
        last_failure_at=datetime(2026, 5, 11, 10, 0, tzinfo=timezone.utc),
        last_opened_at=datetime(2026, 5, 11, 10, 0, tzinfo=timezone.utc),
        last_success_at=None,
    )

    detail = describe_circuit_breaker_snapshot(
        snapshot,
        path_label="output/demo_weekly/.ado_breaker.json",
        state_exists=True,
    )

    assert detail == (
        "ADO breaker OPEN at output/demo_weekly/.ado_breaker.json "
        "(failure_count=3, last_failure_at=2026-05-11T10:00:00+00:00, last_opened_at=2026-05-11T10:00:00+00:00)."
        " Live freshness ADO requests remain gated until recovery or reset."
    )


def test_run_circuit_breaker_doctor_fails_when_edition_is_missing(tmp_path) -> None:
    report = run_circuit_breaker_doctor(
        edition_name="missing_weekly",
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
        reset=False,
        display_path_fn=lambda path: str(path),
        describe_circuit_breaker_snapshot_fn=lambda snapshot, state_path, state_exists: "",
        default_breaker_snapshot_fn=lambda: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    assert report.checks[0].label == "Circuit Breakers"
    assert report.checks[0].status == "fail"

