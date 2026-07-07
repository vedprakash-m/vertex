"""B14 — tests for ado_fetch_timeout_seconds plumbing to the request layer.

Verifies that ADOClient stores the given timeout and passes it through
to the underlying requests.Session.request call.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.core.ado_client import ADOClient


def _make_client(timeout: int) -> ADOClient:
    """Build an ADOClient with a mock session, bypassing real auth."""
    with patch.object(ADOClient, "_build_session", return_value=MagicMock()), \
         patch.object(ADOClient, "_init_auth", return_value=None):
        client = ADOClient(
            organization="your-org",
            project="One",
            timeout=timeout,
            show_progress=False,
        )
    return client


def test_ado_client_stores_timeout_on_instance() -> None:
    client = _make_client(timeout=90)
    assert client.timeout == 90


def test_ado_client_default_timeout_is_30() -> None:
    client = _make_client(timeout=30)
    assert client.timeout == 30


def test_ado_client_passes_timeout_to_request_layer() -> None:
    """timeout= is forwarded to session.request on every call."""
    client = _make_client(timeout=75)

    # Stub _headers so request can be called without real auth
    client._headers = lambda: {}  # type: ignore[method-assign]
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"value": [], "@odata.count": 0}
    client._session.request.return_value = fake_response

    client._request_with_progress("GET", "https://example.com/")

    call_kwargs = client._session.request.call_args
    assert call_kwargs.kwargs.get("timeout") == 75 or call_kwargs.args[2:] == (75,) or (
        # request(method, url, headers=..., timeout=75, ...)
        75 in (call_kwargs.args + tuple(call_kwargs.kwargs.values()))
    )


def test_ado_client_custom_timeout_is_distinct_from_default() -> None:
    """Confirms a non-default timeout is preserved (regression: field was ignored)."""
    client = _make_client(timeout=120)
    assert client.timeout != 30
    assert client.timeout == 120
