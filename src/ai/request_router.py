from __future__ import annotations

import importlib
import os
import time
from collections import deque
from threading import Lock
from typing import Any, Callable

from src.ai.ai_mode import AIMode, get_ai_mode


class AIRequestRouter:
    """Owns direct SDK access for frontier model requests."""

    DEFAULT_API_VERSION = "2024-02-01"
    TIMEOUT_SECONDS = 20
    MAX_RETRIES = 2
    _RATE_LIMIT_WINDOWS: dict[str, deque[float]] = {}
    _RATE_LIMIT_LOCK = Lock()

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        api_key: str | None = None,
        api_version: str | None = None,
        requests_per_minute: int | None = None,
        sleep_func: Any = time.sleep,
        time_func: Callable[[], float] = time.monotonic,
        rate_limit_scope: str,
    ) -> None:
        resolved_endpoint = endpoint or os.environ.get("AZURE_OPENAI_ENDPOINT")
        if not resolved_endpoint:
            raise RuntimeError(
                "AZURE_OPENAI_ENDPOINT not set. Vertex requires Azure OpenAI (PRD §10.6)."
            )

        resolved_api_key = api_key or os.environ.get("AZURE_OPENAI_API_KEY")
        if not resolved_api_key:
            raise RuntimeError(
                "AZURE_OPENAI_API_KEY not set. Vertex requires Azure OpenAI (PRD §10.6)."
            )

        self._requests_per_minute = _normalize_requests_per_minute(requests_per_minute)
        self._sleep = sleep_func
        self._time = time_func
        self._rate_limit_scope = rate_limit_scope

        client_class, rate_limit_error_type, api_status_error_type = self._get_sdk_types()
        self._rate_limit_error_type = rate_limit_error_type
        self._api_status_error_type = api_status_error_type
        self._client = client_class(
            azure_endpoint=resolved_endpoint,
            api_key=resolved_api_key,
            api_version=api_version or os.environ.get("AZURE_OPENAI_API_VERSION") or self.DEFAULT_API_VERSION,
            timeout=self.TIMEOUT_SECONDS,
            max_retries=0,
        )

    def route(
        self,
        *,
        deployment: str,
        system: str,
        user: str,
        temperature: float,
        max_tokens: int,
        response_format: dict[str, Any] | None = None,
    ) -> Any:
        mode = get_ai_mode()
        if mode == AIMode.DISABLED:
            raise RuntimeError("AI execution is disabled for this invocation.")
        if mode == AIMode.OBSERVE_ONLY:
            raise RuntimeError("AI request router is in observe-only mode; frontier calls are blocked.")

        self._wait_for_request_slot()
        last_error: Exception | None = None

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                request_payload: dict[str, Any] = {
                    "model": deployment,
                    "messages": (
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ),
                    "temperature": temperature,
                    "max_completion_tokens": max_tokens,
                }
                if response_format is not None:
                    request_payload["response_format"] = response_format
                return self._client.chat.completions.create(**request_payload)
            except Exception as error:  # pragma: no cover - exercised through tests with fakes
                last_error = error
                if self._is_retryable(error) and attempt < self.MAX_RETRIES:
                    self._sleep(0.5 * (2 ** attempt))
                    continue
                raise

        if last_error is not None:
            raise last_error
        raise RuntimeError("AI request routing failed without an underlying exception.")

    def _wait_for_request_slot(self) -> None:
        if self._requests_per_minute is None:
            return

        while True:
            now = self._time()
            with self._RATE_LIMIT_LOCK:
                window = self._RATE_LIMIT_WINDOWS.setdefault(self._rate_limit_scope, deque())
                cutoff = now - 60.0
                while window and window[0] <= cutoff:
                    window.popleft()
                if len(window) < self._requests_per_minute:
                    window.append(now)
                    return
                wait_seconds = max(0.0, 60.0 - (now - window[0]))
            self._sleep(wait_seconds)

    def _get_sdk_types(self) -> tuple[Any, Any, Any]:
        try:
            module = importlib.import_module("openai")
        except ImportError as error:  # pragma: no cover - depends on optional packages
            raise RuntimeError(
                'AI support requires optional dependencies. Run: pip install -e ".[ai]"'
            ) from error

        client_class = getattr(module, "AzureOpenAI", None)
        if client_class is None:
            raise RuntimeError("OpenAI package is installed but AzureOpenAI is unavailable.")

        rate_limit_error_type = getattr(module, "RateLimitError", Exception)
        api_status_error_type = getattr(module, "APIStatusError", Exception)
        return client_class, rate_limit_error_type, api_status_error_type

    def _is_retryable(self, error: Exception) -> bool:
        if self._rate_limit_error_type is not Exception and isinstance(error, self._rate_limit_error_type):
            return True

        status_code = getattr(error, "status_code", None)
        if status_code == 429:
            return True
        if isinstance(status_code, int) and 500 <= status_code < 600:
            return True

        if self._api_status_error_type is not Exception and isinstance(error, self._api_status_error_type):
            normalized = str(error).lower()
            return "429" in normalized or "5" in normalized

        return False


def _normalize_requests_per_minute(value: int | None) -> int | None:
    if value is None:
        return None
    resolved = int(value)
    if resolved <= 0:
        raise ValueError("requests_per_minute must be a positive integer.")
    return resolved
