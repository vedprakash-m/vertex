# Adapted from Artha scripts/lib/retry.py
from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterable
from functools import wraps
from typing import Any, TypeVar


RetryableFunction = TypeVar("RetryableFunction", bound=Callable[..., Any])
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def retry_with_backoff(
    func: RetryableFunction | Callable[..., Any] | None = None,
    *,
    max_attempts: int = 5,
    base_delay: float = 0.5,
    max_delay: float = 30.0,
    jitter_max: float = 0.3,
    retryable_status_codes: Iterable[int] = RETRYABLE_STATUS_CODES,
    retry_on: tuple[type[BaseException], ...] = (ConnectionError,),
    sleep_func: Callable[[float], None] = time.sleep,
) -> Any:
    retryable_codes = set(retryable_status_codes)

    def execute(callable_fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        delay = base_delay
        for attempt in range(1, max_attempts + 1):
            try:
                return callable_fn(*args, **kwargs)
            except Exception as exc:
                if not _is_retryable(exc, retryable_codes, retry_on) or attempt == max_attempts:
                    raise
                retry_after = _extract_retry_after(exc)
                wait_seconds = retry_after if retry_after is not None else min(delay + random.uniform(0.0, jitter_max), max_delay)
                sleep_func(wait_seconds)
                if retry_after is None:
                    delay = min(delay * 2, max_delay)

    if func is not None:
        return execute(func)

    def decorator(inner: RetryableFunction) -> RetryableFunction:
        @wraps(inner)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return execute(inner, *args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


def _is_retryable(
    exc: Exception,
    retryable_status_codes: set[int],
    retry_on: tuple[type[BaseException], ...],
) -> bool:
    if isinstance(exc, retry_on):
        return True
    status_code = _extract_status_code(exc)
    return status_code in retryable_status_codes


def _extract_status_code(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    if response is not None:
        return getattr(response, "status_code", None)
    return getattr(exc, "status_code", None)


def _extract_retry_after(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or getattr(exc, "headers", None)
    if not isinstance(headers, dict):
        return None
    raw_value = headers.get("Retry-After")
    if raw_value is None:
        return None
    try:
        return float(raw_value)
    except (TypeError, ValueError):
        return None