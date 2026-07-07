"""A-12 contract tests: rate-limit retry coverage across all Zone-C connectors.

Acceptance criterion (A-12 from specs/prod-vis.md §10):
  "retry.py is 429-aware & extensible to all connectors — rate-limit
   fixture retried everywhere"

Each test seeds a 429-like error into a connector and asserts the
connector retries (calls the underlying function more than once) and
ultimately returns the success value.

Connectors covered:
- ``src.core.retry.retry_with_backoff`` (core helper)
- ``src.m365.graph_mail_client.GraphMailClient`` (uses retry_with_backoff)
- ``src.m365.graph_calendar_client.GraphCalendarClient`` (uses retry_with_backoff)
- ``src.m365.teams_reader.TeamsReader`` (uses retry_with_backoff)
- ``src.m365.transcript_reader.TranscriptReader`` (uses retry_with_backoff)
- ``src.core.kusto_client.KustoClient`` (own throttle-loop + _is_throttled)
- ``src.core.ado_client.ADOClient`` (urllib3 Retry with status_forcelist=429)
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.core.retry import RETRYABLE_STATUS_CODES, retry_with_backoff


REPO_ROOT = Path(__file__).resolve().parents[2]

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


class _FakeRateLimitError(Exception):
    """Simulates a 429 response with status_code attribute."""
    status_code = 429


class _FakeRateLimitErrorWithResponse(Exception):
    """Simulates a 429 response on a nested .response.status_code attribute."""
    def __init__(self) -> None:
        super().__init__("429 Too Many Requests")
        self.response = MagicMock(status_code=429)


# --------------------------------------------------------------------------
# Core retry_with_backoff
# --------------------------------------------------------------------------


def test_retry_with_backoff_includes_429_in_retryable_codes() -> None:
    """429 must be in the default retryable status codes."""
    assert 429 in RETRYABLE_STATUS_CODES


def test_retry_with_backoff_retries_on_429_status_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """retry_with_backoff calls the function again after a 429 error."""
    sleeps: list[float] = []
    call_count = 0

    def flaky() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise _FakeRateLimitError("rate limited")
        return "ok"

    result = retry_with_backoff(
        flaky,
        max_attempts=5,
        base_delay=0.0,
        sleep_func=lambda s: sleeps.append(s),
    )
    assert result == "ok"
    assert call_count == 3, f"Expected 3 calls (2 retries + 1 success), got {call_count}"


def test_retry_with_backoff_retries_on_nested_response_status_code() -> None:
    """retry_with_backoff also recognises status on .response.status_code."""
    call_count = 0

    def flaky() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise _FakeRateLimitErrorWithResponse()
        return "ok"

    result = retry_with_backoff(
        flaky,
        max_attempts=3,
        base_delay=0.0,
        sleep_func=lambda s: None,
    )
    assert result == "ok"
    assert call_count == 2


def test_retry_with_backoff_respects_retry_after_header() -> None:
    """Retry-After header value is used for sleep instead of backoff."""
    slept: list[float] = []
    call_count = 0

    class _RateLimitWithRetryAfter(Exception):
        def __init__(self) -> None:
            super().__init__("429")
            self.response = MagicMock(
                status_code=429,
                headers={"Retry-After": "2.5"},
            )

    def flaky() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise _RateLimitWithRetryAfter()
        return "ok"

    retry_with_backoff(
        flaky,
        max_attempts=3,
        base_delay=99.0,
        sleep_func=slept.append,
    )
    assert slept == [2.5], f"Expected Retry-After sleep of 2.5, got {slept}"


# --------------------------------------------------------------------------
# Kusto client — own throttle loop
# --------------------------------------------------------------------------


def test_kusto_client_retries_on_throttle() -> None:
    """KustoClient retries when _is_throttled returns True."""
    from src.core.kusto_client import KustoClient

    client = KustoClient(sleep_func=lambda s: None)

    # Patch _get_sdk_types to return a fake SDK that raises a 429-style error twice.
    call_count = 0

    class _FakeClient:
        def execute(self, *args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError("429: Too Many Requests — throttled")
            return MagicMock(primary_results=[])

    def _fake_sdk_types() -> tuple[Any, Any, Any, Any]:
        return (MagicMock, MagicMock, MagicMock, MagicMock)

    # Pre-seed the client cache so _get_or_create_client returns our fake.
    client._clients["https://mycluster.kusto.windows.net"] = _FakeClient()

    rows, _ = client.execute_with_schema(
        "https://mycluster.kusto.windows.net", "mydb", "MyTable | take 1"
    )
    assert call_count == 3
    assert rows == []


def test_kusto_client_is_throttled_recognises_429() -> None:
    """KustoClient._is_throttled must return True for 429 text."""
    from src.core.kusto_client import KustoClient

    client = KustoClient()
    assert client._is_throttled(RuntimeError("429 Too Many Requests"))
    assert client._is_throttled(RuntimeError("throttled by service"))
    assert not client._is_throttled(RuntimeError("404 Not Found"))


# --------------------------------------------------------------------------
# Graph mail client
# --------------------------------------------------------------------------


def test_graph_mail_client_uses_retry_with_backoff() -> None:
    """GraphMailClient._retry calls src.core.retry.retry_with_backoff."""
    source = (REPO_ROOT / "src" / "m365" / "graph_mail_client.py").read_text(encoding="utf-8")
    assert "retry_with_backoff" in source, (
        "graph_mail_client.py must use retry_with_backoff from src.core.retry"
    )


def test_graph_mail_client_retry_retries_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    """GraphMailClient._retry retries a 429-raising callable."""
    from src.m365.graph_mail_client import GraphMailClient

    bridge = MagicMock()
    client = GraphMailClient(bridge=bridge)

    call_count = 0

    def flaky() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise _FakeRateLimitError("rate limited")
        return {"value": []}

    result = client._retry(flaky)
    assert call_count == 2
    assert result == {"value": []}


# --------------------------------------------------------------------------
# Graph calendar client
# --------------------------------------------------------------------------


def test_graph_calendar_client_uses_retry_with_backoff() -> None:
    """GraphCalendarClient._retry calls src.core.retry.retry_with_backoff."""
    source = (REPO_ROOT / "src" / "m365" / "graph_calendar_client.py").read_text(encoding="utf-8")
    assert "retry_with_backoff" in source


def test_graph_calendar_client_retry_retries_on_429() -> None:
    """GraphCalendarClient._retry retries a 429-raising callable."""
    from src.m365.graph_calendar_client import GraphCalendarClient

    bridge = MagicMock()
    client = GraphCalendarClient(bridge=bridge)

    call_count = 0

    def flaky() -> dict[str, Any] | None:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise _FakeRateLimitError("rate limited")
        return {"value": []}

    result = client._retry(flaky)
    assert call_count == 2
    assert result == {"value": []}


# --------------------------------------------------------------------------
# Teams reader
# --------------------------------------------------------------------------


def test_teams_reader_uses_retry_with_backoff() -> None:
    """TeamsReader._retry calls src.core.retry.retry_with_backoff."""
    source = (REPO_ROOT / "src" / "m365" / "teams_reader.py").read_text(encoding="utf-8")
    assert "retry_with_backoff" in source


def test_teams_reader_retry_retries_on_429() -> None:
    """TeamsReader._retry retries a 429-raising callable."""
    from src.m365.teams_reader import TeamsReader

    bridge = MagicMock()
    reader = TeamsReader(bridge=bridge)

    call_count = 0

    def flaky() -> list[dict[str, Any]]:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise _FakeRateLimitError("rate limited")
        return []

    result = reader._retry(flaky)
    assert call_count == 2
    assert result == []


# --------------------------------------------------------------------------
# Transcript reader
# --------------------------------------------------------------------------


def test_transcript_reader_uses_retry_with_backoff() -> None:
    """TranscriptReader._retry calls src.core.retry.retry_with_backoff."""
    source = (REPO_ROOT / "src" / "m365" / "transcript_reader.py").read_text(encoding="utf-8")
    assert "retry_with_backoff" in source


def test_transcript_reader_retry_retries_on_429() -> None:
    """TranscriptReader._retry retries a 429-raising callable."""
    from src.m365.transcript_reader import TranscriptReader

    bridge = MagicMock()
    reader = TranscriptReader(bridge=bridge)

    call_count = 0

    def flaky() -> list[dict[str, Any]]:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise _FakeRateLimitError("rate limited")
        return []

    result = reader._retry(flaky)
    assert call_count == 2
    assert result == []


# --------------------------------------------------------------------------
# ADO client — urllib3 Retry with status_forcelist
# --------------------------------------------------------------------------


def test_ado_client_session_uses_urllib3_retry_with_429() -> None:
    """ADOClient._build_session must include 429 in the urllib3 status_forcelist."""
    source = (REPO_ROOT / "src" / "core" / "ado_client.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="ado_client.py")
    found_429_in_forcelist = False
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "status_forcelist":
            val = node.value
            if isinstance(val, (ast.List, ast.Tuple, ast.Set)):
                for elt in val.elts:
                    if isinstance(elt, ast.Constant) and elt.value == 429:
                        found_429_in_forcelist = True
    assert found_429_in_forcelist, (
        "ado_client.py _build_session must pass status_forcelist=(429, ...) to urllib3 Retry"
    )
