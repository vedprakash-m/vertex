from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.core.circuit_breaker import BreakerSnapshot, CircuitBreaker, CircuitBreakerState
from src.core.edition_resolver import resolve_edition


def default_breaker_snapshot() -> BreakerSnapshot:
    return BreakerSnapshot(
        state=CircuitBreakerState.CLOSED,
        failure_count=0,
        last_failure_at=None,
        last_opened_at=None,
        last_success_at=None,
    )


def display_path(path: Path, *, output_root: Path | None = None, programs_root: Path | None = None, repo_root: Path | None = None) -> str:
    if programs_root is not None:
        try:
            rel = path.relative_to(programs_root)
            # Strip the leading program-id component (e.g. "acme/output/..." → "output/...")
            parts = rel.parts
            if len(parts) > 1:
                return Path(*parts[1:]).as_posix()
        except ValueError:
            pass
    if output_root is not None:
        try:
            return Path(output_root.name, path.relative_to(output_root)).as_posix()
        except ValueError:
            pass
    if repo_root is not None:
        try:
            return path.relative_to(repo_root).as_posix()
        except ValueError:
            pass
    return str(path)


def describe_circuit_breaker_snapshot(
    snapshot: BreakerSnapshot,
    *,
    path_label: str,
    state_exists: bool,
) -> str:
    if not state_exists:
        return f"{path_label} is absent; effective ADO breaker state is CLOSED."

    details = [f"failure_count={snapshot.failure_count}"]
    if snapshot.last_failure_at is not None:
        details.append(f"last_failure_at={snapshot.last_failure_at.isoformat()}")
    if snapshot.last_opened_at is not None:
        details.append(f"last_opened_at={snapshot.last_opened_at.isoformat()}")
    if snapshot.last_success_at is not None:
        details.append(f"last_success_at={snapshot.last_success_at.isoformat()}")

    suffix = ""
    if snapshot.state != CircuitBreakerState.CLOSED:
        suffix = " Live freshness ADO requests remain gated until recovery or reset."
    return f"ADO breaker {snapshot.state.value} at {path_label} ({', '.join(details)}).{suffix}"


def run_circuit_breaker_doctor(
    *,
    edition_name: str,
    editions_root: Path,
    programs_root: Path,
    reset: bool,
    display_path_fn: Callable[[Path], str],
    describe_circuit_breaker_snapshot_fn: Callable[[BreakerSnapshot, Path, bool], str],
    default_breaker_snapshot_fn: Callable[[], BreakerSnapshot],
) -> DoctorReport:
    resolved = resolve_edition(
        edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    if resolved is None:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Circuit Breakers", "fail", f"Edition '{edition_name}' could not be resolved."),),
        )

    state_path = resolved.paths.publications_dir / ".ado_breaker.json"
    snapshot, state_exists, malformed = inspect_circuit_breaker_state(
        state_path,
        default_breaker_snapshot_fn=default_breaker_snapshot_fn,
    )
    path_label = display_path_fn(state_path)

    if reset:
        CircuitBreaker(state_path=state_path).reset()
        if state_exists:
            previous_state = "malformed persisted state" if malformed else snapshot.state.value
            detail = f"Reset ADO breaker from {previous_state} to CLOSED at {path_label}."
        else:
            detail = f"Initialized ADO breaker state at {path_label} and set it to CLOSED."
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Circuit Breakers", "ok", detail),),
        )

    if malformed:
        return DoctorReport(
            edition=edition_name,
            checks=(
                DoctorCheck(
                    "Circuit Breakers",
                    "fail",
                    f"Malformed ADO breaker state at {path_label}; reset with --reset-circuit-breakers to restore a clean CLOSED state.",
                ),
            ),
        )

    status = "ok" if snapshot.state == CircuitBreakerState.CLOSED else "warn"
    detail = describe_circuit_breaker_snapshot_fn(snapshot, state_path, state_exists)
    return DoctorReport(edition=edition_name, checks=(DoctorCheck("Circuit Breakers", status, detail),))


def inspect_circuit_breaker_state(
    state_path: Path,
    *,
    default_breaker_snapshot_fn: Callable[[], BreakerSnapshot],
) -> tuple[BreakerSnapshot, bool, bool]:
    if not state_path.exists():
        return (default_breaker_snapshot_fn(), False, False)

    malformed = False
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = None
        malformed = True

    if payload is not None:
        malformed = malformed or breaker_payload_is_malformed(payload)

    if malformed:
        return (default_breaker_snapshot_fn(), True, True)

    return (CircuitBreaker(state_path=state_path).get_state(), True, False)


def breaker_payload_is_malformed(payload: object) -> bool:
    if not isinstance(payload, dict):
        return True
    try:
        CircuitBreakerState(str(payload.get("state", CircuitBreakerState.CLOSED.value)))
        int(payload.get("failure_count", 0))
    except (TypeError, ValueError):
        return True

    return any(
        not breaker_timestamp_is_valid(payload.get(key))
        for key in ("last_failure_at", "last_opened_at", "last_success_at")
    )


def breaker_timestamp_is_valid(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True
