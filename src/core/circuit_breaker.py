from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path


@dataclass(frozen=True, slots=True)
class BreakerSnapshot:
    state: "CircuitBreakerState"
    failure_count: int
    last_failure_at: datetime | None
    last_opened_at: datetime | None
    last_success_at: datetime | None


class CircuitBreakerState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    def __init__(
        self,
        *,
        state_path: Path,
        lock_path: Path | None = None,
        failure_threshold: int = 3,
        recovery_timeout: timedelta = timedelta(hours=4),
    ) -> None:
        self._state_path = state_path
        self._lock_path = lock_path or state_path.with_suffix(state_path.suffix + ".lock")
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout

    def get_state(self) -> BreakerSnapshot:
        return self._from_payload(self._read_payload())

    def should_allow_request(self, *, now: datetime | None = None) -> tuple[bool, bool]:
        current_time = now or datetime.now(timezone.utc)
        state = self.get_state()
        if state.state == CircuitBreakerState.CLOSED:
            return True, False

        if state.state == CircuitBreakerState.OPEN:
            last_opened_at = state.last_opened_at or datetime.min.replace(tzinfo=timezone.utc)
            if current_time - last_opened_at >= self._recovery_timeout and self._acquire_probe_lock():
                payload = self._read_payload()
                payload["state"] = CircuitBreakerState.HALF_OPEN.value
                self._write_payload(payload)
                return True, True
            return False, False

        if state.state == CircuitBreakerState.HALF_OPEN:
            return False, False

        return True, False

    def record_success(self, *, is_probe: bool = False, now: datetime | None = None) -> None:
        current_time = now or datetime.now(timezone.utc)
        payload = self._read_payload()
        payload.update(
            {
                "state": CircuitBreakerState.CLOSED.value,
                "failure_count": 0,
                "last_success_at": current_time.isoformat(),
            }
        )
        self._write_payload(payload)
        if is_probe:
            self._release_probe_lock()

    def record_failure(
        self,
        *,
        error: str | None = None,
        is_probe: bool = False,
        now: datetime | None = None,
    ) -> None:
        del error
        current_time = now or datetime.now(timezone.utc)
        payload = self._read_payload()
        state = CircuitBreakerState(str(payload.get("state", CircuitBreakerState.CLOSED.value)))

        if is_probe or state == CircuitBreakerState.HALF_OPEN:
            payload.update(
                {
                    "state": CircuitBreakerState.OPEN.value,
                    "failure_count": 0,
                    "last_failure_at": current_time.isoformat(),
                    "last_opened_at": current_time.isoformat(),
                }
            )
            self._write_payload(payload)
            if is_probe:
                self._release_probe_lock()
            return

        failure_count = _coerce_int(payload.get("failure_count", 0)) + 1
        payload["failure_count"] = failure_count
        payload["last_failure_at"] = current_time.isoformat()
        if failure_count >= self._failure_threshold:
            payload["state"] = CircuitBreakerState.OPEN.value
            payload["last_opened_at"] = current_time.isoformat()
        self._write_payload(payload)

    def reset(self) -> None:
        self._write_payload(self._default_payload())
        self._release_probe_lock()

    def _default_payload(self) -> dict[str, object]:
        return {
            "state": CircuitBreakerState.CLOSED.value,
            "failure_count": 0,
            "last_failure_at": None,
            "last_opened_at": None,
            "last_success_at": None,
        }

    def _read_payload(self) -> dict[str, object]:
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._default_payload()
        except (OSError, json.JSONDecodeError):
            return self._default_payload()

        if not isinstance(payload, dict):
            return self._default_payload()
        try:
            CircuitBreakerState(str(payload.get("state", CircuitBreakerState.CLOSED.value)))
        except ValueError:
            return self._default_payload()
        return {**self._default_payload(), **payload}

    def _write_payload(self, payload: dict[str, object]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, self._state_path)

    def _from_payload(self, payload: dict[str, object]) -> BreakerSnapshot:
        return BreakerSnapshot(
            state=CircuitBreakerState(str(payload["state"])),
            failure_count=_coerce_int(payload.get("failure_count", 0)),
            last_failure_at=_parse_datetime(payload.get("last_failure_at")),
            last_opened_at=_parse_datetime(payload.get("last_opened_at")),
            last_success_at=_parse_datetime(payload.get("last_success_at")),
        )

    def _acquire_probe_lock(self) -> bool:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            file_descriptor = os.open(str(self._lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        try:
            os.write(file_descriptor, str(os.getpid()).encode("utf-8"))
        finally:
            os.close(file_descriptor)
        return True

    def _release_probe_lock(self) -> None:
        try:
            self._lock_path.unlink(missing_ok=True)
        except OSError:
            return


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _coerce_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        return int(value)
    return 0
