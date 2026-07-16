"""Contract tests for ADF-W1.1: no transparent retry for ADO mutations.

INV-ADF-9: retrying or re-approving an intent cannot duplicate the remote
effect. A lost response after a committed POST/PATCH must never be silently
retried by the transport layer -- that is exactly how a duplicate work item
gets created. Reads (WIQL, batch-get) keep automatic retry.
"""

from __future__ import annotations

import os

import pytest

from src.core.ado_client import ADOClient
from src.core.exceptions import QueryError
from src.m365.ado_writer import ADOWriter, _parse_retry_after_seconds


def _bare_client() -> ADOClient:
    """A real ADOClient with only session-building exercised (no network/auth)."""
    os.environ.setdefault("ADO_PAT", "fake-pat-for-contract-test")
    client = object.__new__(ADOClient)
    client.timeout = 30
    client.auth_method = "pat"
    client._session = client._build_session()
    client._mutation_session = client._build_mutation_session()
    return client


def test_wiql_read_retry_enabled() -> None:
    """The read session keeps automatic retry (GET + the read-only WIQL/batch POSTs)."""
    client = _bare_client()
    adapter = client._session.get_adapter("https://example.com")
    assert adapter.max_retries.total == 5
    assert "GET" in adapter.max_retries.allowed_methods
    assert "POST" in adapter.max_retries.allowed_methods


def test_mutation_session_has_no_retry_budget() -> None:
    client = _bare_client()
    adapter = client._mutation_session.get_adapter("https://example.com")
    assert adapter.max_retries.total == 0


def test_mutation_session_is_distinct_from_read_session() -> None:
    client = _bare_client()
    assert client._session is not client._mutation_session


class _FakeResponse:
    def __init__(self, status_code: int, *, headers: dict[str, str] | None = None, body: dict | None = None) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body or {}
        self.text = "mock upstream error"

    def json(self) -> dict:
        return self._body


class _CountingSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method: str, url: str, **kwargs) -> _FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


class _FakeADOClientForWriter:
    """Minimal ADOClient double: only what ADOWriter._request_json touches."""

    def __init__(self, mutation_session: _CountingSession) -> None:
        self.timeout = 30
        self.auth_method = "pat"
        self._mutation_session = mutation_session

    def _headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer fake", "Accept": "application/json"}


def test_mutation_post_not_retried_on_502() -> None:
    """A 502 on a mutation POST is not retried -- one attempt, then QueryError."""
    session = _CountingSession([_FakeResponse(502)])
    client = _FakeADOClientForWriter(session)
    writer = ADOWriter(client)

    with pytest.raises(QueryError):
        writer._request_json("POST", "https://dev.azure.com/org/proj/_apis/wit/workitems/$Task", json_body={"op": "add"})

    assert len(session.calls) == 1


def test_mutation_post_not_retried_on_500() -> None:
    session = _CountingSession([_FakeResponse(500)])
    client = _FakeADOClientForWriter(session)
    writer = ADOWriter(client)

    with pytest.raises(QueryError):
        writer._request_json("PATCH", "https://dev.azure.com/org/proj/_apis/wit/workitems/1?api-version=7.1")

    assert len(session.calls) == 1


def test_429_sleeps_once_via_retry_after_then_raises_without_retry(monkeypatch) -> None:
    sleep_calls: list[float] = []
    monkeypatch.setattr("src.m365.ado_writer.time.sleep", lambda seconds: sleep_calls.append(seconds))

    session = _CountingSession([_FakeResponse(429, headers={"Retry-After": "3"})])
    client = _FakeADOClientForWriter(session)
    writer = ADOWriter(client)

    with pytest.raises(QueryError, match="rate-limited"):
        writer._request_json("POST", "https://dev.azure.com/org/proj/_apis/wit/workitems/$Task", json_body={})

    assert len(session.calls) == 1  # no automatic retry loop
    assert sleep_calls == [3.0]  # exactly one courtesy sleep


def test_429_without_retry_after_header_does_not_sleep(monkeypatch) -> None:
    sleep_calls: list[float] = []
    monkeypatch.setattr("src.m365.ado_writer.time.sleep", lambda seconds: sleep_calls.append(seconds))

    session = _CountingSession([_FakeResponse(429)])
    client = _FakeADOClientForWriter(session)
    writer = ADOWriter(client)

    with pytest.raises(QueryError, match="rate-limited"):
        writer._request_json("POST", "https://dev.azure.com/org/proj/_apis/wit/workitems/$Task", json_body={})

    assert len(session.calls) == 1
    assert sleep_calls == []


@pytest.mark.parametrize(
    "header_value,expected",
    [
        (None, 0.0),
        ("", 0.0),
        ("5", 5.0),
        ("0", 0.0),
        ("not-a-number-or-date", 0.0),
    ],
)
def test_parse_retry_after_seconds_delta_form(header_value, expected) -> None:
    assert _parse_retry_after_seconds(header_value) == expected
