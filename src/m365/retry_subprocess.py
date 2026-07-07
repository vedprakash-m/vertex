"""WS-17: subprocess-level retry helper for ``agency_bridge`` and other
``subprocess.run`` callers.

``core.retry.retry_with_backoff`` is built for value-returning callables
that raise HTTP-shaped exceptions (status_code attribute). It does not
fit ``subprocess.run`` cleanly because the runner returns a
``CompletedProcess`` and signals failure via ``returncode`` (not an
exception) or via ``TimeoutExpired`` (a ``TimeoutError`` subclass).

This module fills the gap:

- ``SubprocessRunner`` — the same ``Callable[..., subprocess.CompletedProcess[str]]``
  protocol the bridge already uses (zero-change injection).
- ``retry_subprocess_call`` — runs the runner, retries on
  ``subprocess.TimeoutExpired`` (always) and on
  ``CalledProcessError`` / ``returncode in retryable_exit_codes``
  (caller-provided).
- ``build_subprocess_runner`` — convenience factory that wraps any
  ``SubprocessRunner`` with a default retry policy.

The retry uses the same backoff curve as ``retry_with_backoff`` (exponential
+ jitter capped at ``max_delay``) so SRE-tuned operators see one
behavior across the platform.
"""
from __future__ import annotations

import random
import subprocess
import time
from collections.abc import Iterable
from typing import Any, Callable


# Exit codes we will always retry regardless of caller preference. These
# cover the standard POSIX "this might work in a moment" cases.
_DEFAULT_RETRYABLE_EXIT_CODES: frozenset[int] = frozenset({
    1,       # Generic failure (workiq.exe frequently returns 1 on transient
             # "toolchain not yet bootstrapped" errors during a cold start)
    2,       # Misuse / unavailable
    75,      # EX_TEMPFAIL — BSD sysexits.h; designed for this case
    143,     # SIGTERM (e.g. operator-driven container restart)
})


SubprocessRunner = Callable[..., subprocess.CompletedProcess[str]]


def retry_subprocess_call(
    runner: SubprocessRunner,
    *args: Any,
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    jitter_max: float = 0.3,
    retryable_exit_codes: Iterable[int] = (),
    sleep_func: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """Invoke ``runner(*args, **kwargs)`` with bounded retry.

    Retries on:
    - ``subprocess.TimeoutExpired`` — always retryable (transient).
    - ``returncode in retryable_exit_codes`` — caller-provided.
    - ``returncode in _DEFAULT_RETRYABLE_EXIT_CODES`` — platform defaults.

    Does NOT retry on:
    - ``FileNotFoundError`` (the executable is missing; retrying won't help).
    - ``PermissionError`` (the executable is not executable; retrying won't help).
    - ``returncode == 0`` (success).
    - ``returncode`` not in the retryable set (caller is asking us to bubble it up).
    """
    combined_codes: set[int] = set(_DEFAULT_RETRYABLE_EXIT_CODES) | set(retryable_exit_codes)
    delay = base_delay
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            result = runner(*args, **kwargs)
        except subprocess.TimeoutExpired as exc:
            last_exc = exc
            if attempt == max_attempts:
                raise
            _sleep_with_jitter(delay, jitter_max, sleep_func)
            delay = min(delay * 2, max_delay)
            continue
        except (FileNotFoundError, PermissionError):
            # These never succeed on retry — fail fast.
            raise
        # Result returned; check the exit code.
        if result.returncode == 0 or result.returncode not in combined_codes:
            return result
        if attempt == max_attempts:
            return result  # final attempt; let the caller see the failure
        _sleep_with_jitter(delay, jitter_max, sleep_func)
        delay = min(delay * 2, max_delay)
    # Unreachable: the loop always returns or raises on the final attempt.
    if last_exc is not None:  # pragma: no cover - defensive
        raise last_exc
    raise RuntimeError("retry_subprocess_call exited without a result")  # pragma: no cover


def build_subprocess_runner(
    base_runner: SubprocessRunner,
    *,
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    jitter_max: float = 0.3,
    retryable_exit_codes: Iterable[int] = (),
) -> SubprocessRunner:
    """Wrap ``base_runner`` in a retry policy and return a closure with the
    same signature. The returned runner is drop-in for any
    ``SubprocessRunner``-typed parameter (e.g. ``agency_bridge``'s
    ``runner`` constructor argument)."""
    def wrapped(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return retry_subprocess_call(
            base_runner,
            *args,
            max_attempts=max_attempts,
            base_delay=base_delay,
            max_delay=max_delay,
            jitter_max=jitter_max,
            retryable_exit_codes=retryable_exit_codes,
            **kwargs,
        )
    return wrapped


def _sleep_with_jitter(delay: float, jitter_max: float, sleep_func: Callable[[float], None]) -> None:
    sleep_func(delay + random.uniform(0.0, jitter_max))
