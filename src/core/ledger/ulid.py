from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import secrets
from threading import Lock


_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_RANDOM_BITS = 80
_RANDOM_MASK = (1 << _RANDOM_BITS) - 1


@dataclass(slots=True)
class _UlidState:
    last_timestamp_ms: int = -1
    last_random: int = 0


_STATE = _UlidState()
_LOCK = Lock()


def _ensure_utc(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _encode_crockford(value: int, length: int) -> str:
    chars = ["0"] * length
    for index in range(length - 1, -1, -1):
        chars[index] = _CROCKFORD_ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(chars)


def _timestamp_ms(now: datetime) -> int:
    utc_now = _ensure_utc(now)
    return int(utc_now.timestamp() * 1000)


def new_ulid(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    current_ms = _timestamp_ms(current)
    with _LOCK:
        if current_ms < _STATE.last_timestamp_ms:
            current_ms = _STATE.last_timestamp_ms

        if current_ms == _STATE.last_timestamp_ms:
            random_bits = (_STATE.last_random + 1) & _RANDOM_MASK
            if random_bits == 0:
                current_ms += 1
                random_bits = secrets.randbits(_RANDOM_BITS)
        else:
            random_bits = secrets.randbits(_RANDOM_BITS)

        _STATE.last_timestamp_ms = current_ms
        _STATE.last_random = random_bits

    return _encode_crockford(current_ms, 10) + _encode_crockford(random_bits, 16)


def reset_ulid_state_for_tests() -> None:
    with _LOCK:
        _STATE.last_timestamp_ms = -1
        _STATE.last_random = 0