from __future__ import annotations

import types

import pytest

from src.core.exceptions import AuthError, QueryError
from src.m365.icm_client import IcmClient


class _FakeCredential:
    def __init__(self, *, tenant_id: str, client_id: str, client_secret: str) -> None:
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret

    def get_token(self, _scope: str):
        return types.SimpleNamespace(token="token-123")


class _FakeResponse:
    def __init__(self, status_code: int, *, payload=None, text: str = "", headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


# ---------------------------------------------------------------------------
# Lazy-init contract: construction is always safe
# ---------------------------------------------------------------------------

def test_icm_client_constructs_with_no_env_vars(monkeypatch) -> None:
    """IcmClient() with no credentials must NOT raise — lazy-init pattern."""
    for var in ("ICM_INCIDENTS_URL", "ICM_TENANT_ID", "ICM_CLIENT_ID", "ICM_CLIENT_SECRET", "ICM_SCOPE"):
        monkeypatch.delenv(var, raising=False)
    client = IcmClient()  # must not raise
    assert client is not None


def test_icm_client_raises_on_missing_url_at_call_time(monkeypatch) -> None:
    """AuthError fires on list_incidents(), not on construction."""
    monkeypatch.delenv("ICM_INCIDENTS_URL", raising=False)
    client = IcmClient(tenant_id="tenant-1", client_id="client-1", client_secret="secret-1")
    # Construction succeeds; the error surfaces only when a live call is made.
    with pytest.raises(AuthError, match="ICM_INCIDENTS_URL"):
        client.list_incidents()


def test_icm_client_raises_on_missing_tenant_at_call_time(monkeypatch) -> None:
    monkeypatch.delenv("ICM_TENANT_ID", raising=False)
    client = IcmClient(incidents_url="https://icm.example.test/incidents", client_id="c", client_secret="s")
    with pytest.raises(AuthError, match="ICM_TENANT_ID"):
        client.list_incidents()


# ---------------------------------------------------------------------------
# Happy-path and payload tests
# ---------------------------------------------------------------------------

def test_icm_client_gets_expected_payload(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _get(url, *, headers, params, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["params"] = params
        captured["timeout"] = timeout
        return _FakeResponse(200, payload={"items": [{"incidentId": "12345"}]})

    monkeypatch.setattr("src.m365.icm_client._get_client_secret_credential_class", lambda: _FakeCredential)
    monkeypatch.setattr("src.m365.icm_client._get_requests_module", lambda: types.SimpleNamespace(get=_get))

    client = IcmClient(
        incidents_url="https://icm.example.test/incidents",
        tenant_id="tenant-1",
        client_id="client-1",
        client_secret="secret-1",
    )
    payload = client.list_incidents(params={"top": 25})

    assert payload == {"items": [{"incidentId": "12345"}]}
    assert captured["url"] == "https://icm.example.test/incidents"
    assert captured["headers"] == {
        "Authorization": "Bearer token-123",
        "Accept": "application/json",
    }
    assert captured["params"] == {"top": 25}
    assert captured["timeout"] == 30


def test_icm_client_wraps_list_payloads(monkeypatch) -> None:
    monkeypatch.setattr("src.m365.icm_client._get_client_secret_credential_class", lambda: _FakeCredential)
    monkeypatch.setattr(
        "src.m365.icm_client._get_requests_module",
        lambda: types.SimpleNamespace(get=lambda *args, **kwargs: _FakeResponse(200, payload=[{"incidentId": "12345"}])),
    )

    client = IcmClient(
        incidents_url="https://icm.example.test/incidents",
        tenant_id="tenant-1",
        client_id="client-1",
        client_secret="secret-1",
    )

    assert client.list_incidents() == {"items": [{"incidentId": "12345"}]}


def test_icm_client_surfaces_retryable_errors(monkeypatch) -> None:
    monkeypatch.setattr("src.m365.icm_client._get_client_secret_credential_class", lambda: _FakeCredential)
    monkeypatch.setattr(
        "src.m365.icm_client._get_requests_module",
        lambda: types.SimpleNamespace(get=lambda *args, **kwargs: _FakeResponse(429, headers={"Retry-After": "9"})),
    )

    client = IcmClient(
        incidents_url="https://icm.example.test/incidents",
        tenant_id="tenant-1",
        client_id="client-1",
        client_secret="secret-1",
    )

    with pytest.raises(QueryError, match="Retry-After: 9"):
        client.list_incidents()
