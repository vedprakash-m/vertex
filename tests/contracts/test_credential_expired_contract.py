"""WS-3 contract tests: CredentialExpired exception.

PB-38: mid-flight credential expiry (expired ADO PAT / Graph token) must
raise a typed ``CredentialExpired`` exception rather than crashing.  The
class must be importable from ``src.core.exceptions`` and carry
``auth_method`` / ``connector`` attributes so callers can emit an
``ActionRequired`` prompt without inspecting raw exception text.
"""
from __future__ import annotations

import pytest

from src.core.exceptions import AuthError, CredentialExpired, VertexError


# ---------------------------------------------------------------------------
# Existence and hierarchy
# ---------------------------------------------------------------------------


def test_credential_expired_is_importable() -> None:
    """The exception class must live in src.core.exceptions."""
    assert CredentialExpired is not None


def test_credential_expired_is_subclass_of_auth_error() -> None:
    assert issubclass(CredentialExpired, AuthError)


def test_credential_expired_is_subclass_of_vertex_error() -> None:
    assert issubclass(CredentialExpired, VertexError)


# ---------------------------------------------------------------------------
# Constructor / attributes
# ---------------------------------------------------------------------------


def test_credential_expired_raises_with_message() -> None:
    with pytest.raises(CredentialExpired, match="ADO PAT has expired"):
        raise CredentialExpired("ADO PAT has expired", auth_method="ADO_PAT", connector="ADO")


def test_credential_expired_carries_auth_method() -> None:
    exc = CredentialExpired("token expired", auth_method="AAD_device_code", connector="Graph")
    assert exc.auth_method == "AAD_device_code"


def test_credential_expired_carries_connector() -> None:
    exc = CredentialExpired("token expired", auth_method="managed_identity", connector="Kusto")
    assert exc.connector == "Kusto"


def test_credential_expired_defaults_empty_fields() -> None:
    """Callers that don't know the auth method can still raise without crashing."""
    exc = CredentialExpired("credential expired")
    assert exc.auth_method == ""
    assert exc.connector == ""


def test_credential_expired_message_is_str() -> None:
    exc = CredentialExpired("expired", auth_method="ADO_PAT", connector="ADO")
    assert str(exc) == "expired"


def test_credential_expired_is_caught_as_auth_error() -> None:
    """Existing ``except AuthError`` handlers must catch CredentialExpired."""
    caught = False
    try:
        raise CredentialExpired("expired", auth_method="ADO_PAT", connector="ADO")
    except AuthError:
        caught = True
    assert caught


def test_credential_expired_is_not_caught_as_query_error() -> None:
    """CredentialExpired must NOT be silently swallowed by QueryError handlers."""
    from src.core.exceptions import QueryError

    caught_as_query_error = False
    try:
        raise CredentialExpired("expired")
    except QueryError:
        caught_as_query_error = True
    except CredentialExpired:
        pass
    assert not caught_as_query_error


# ---------------------------------------------------------------------------
# Transport-level wiring (PB-38: ADO 401 raises CredentialExpired, not QueryError)
# ---------------------------------------------------------------------------


def _make_mock_response(status_code: int, text: str = "", www_auth: str = "") -> object:
    """Minimal response object for ADOClient._request_json tests."""

    class _Response:
        headers: dict[str, str]

        def __init__(self) -> None:
            self.status_code = status_code
            self.text = text
            self.headers = {"WWW-Authenticate": www_auth} if www_auth else {}

        def json(self) -> dict:
            return {}

    return _Response()


def test_ado_client_raises_credential_expired_on_401(monkeypatch) -> None:
    """ADOClient._request_json must raise CredentialExpired (not QueryError) on HTTP 401."""
    from src.core.ado_client import ADOClient
    from src.core.exceptions import QueryError

    client = ADOClient.__new__(ADOClient)
    client.auth_method = "ADO_PAT"
    client.show_progress = False
    client.timeout = 30

    mock_resp = _make_mock_response(401, text="Unauthorized", www_auth="Bearer realm=ADO")
    monkeypatch.setattr(
        client,
        "_request_with_progress",
        lambda method, url, **kwargs: mock_resp,
    )

    with pytest.raises(CredentialExpired) as exc_info:
        client._request_json("GET", "https://dev.azure.com/test/_apis/wit/workitems")

    exc = exc_info.value
    assert exc.connector == "ADO"
    assert exc.auth_method == "ADO_PAT"
    assert not isinstance(exc, QueryError)


def test_ado_client_raises_query_error_on_non_401_4xx(monkeypatch) -> None:
    """ADOClient._request_json must still raise QueryError for non-401 4xx responses."""
    from src.core.ado_client import ADOClient
    from src.core.exceptions import QueryError

    client = ADOClient.__new__(ADOClient)
    client.auth_method = "ADO_PAT"
    client.show_progress = False
    client.timeout = 30

    mock_resp = _make_mock_response(404, text="Not Found")
    monkeypatch.setattr(
        client,
        "_request_with_progress",
        lambda method, url, **kwargs: mock_resp,
    )

    with pytest.raises(QueryError):
        client._request_json("GET", "https://dev.azure.com/test/_apis/wit/workitems/99999")


def test_ado_writer_raises_credential_expired_on_401(monkeypatch) -> None:
    """ADOWriter._request_json must raise CredentialExpired (not QueryError) on HTTP 401."""
    import unittest.mock as mock

    from src.core.ado_client import ADOClient
    from src.core.exceptions import QueryError
    from src.m365.ado_writer import ADOWriter

    ado_client = ADOClient.__new__(ADOClient)
    ado_client.auth_method = "ADO_PAT"
    ado_client.timeout = 30
    ado_client._headers = lambda: {"Authorization": "Basic dGVzdA=="}

    mock_resp = _make_mock_response(401, text="Unauthorized")
    session_mock = mock.MagicMock()
    session_mock.request.return_value = mock_resp
    ado_client._session = session_mock
    # ADF-W1.1: ADOWriter._request_json now uses the dedicated mutation session.
    ado_client._mutation_session = session_mock

    writer = ADOWriter.__new__(ADOWriter)
    writer._client = ado_client

    with pytest.raises(CredentialExpired) as exc_info:
        writer._request_json("PATCH", "https://dev.azure.com/test/_apis/wit/workitems/1")

    exc = exc_info.value
    assert exc.connector == "ADO"
    assert not isinstance(exc, QueryError)


# ---------------------------------------------------------------------------
# A-14 / A-15: ActionRequired banner in gather pipeline (PB-38 residual)
# ---------------------------------------------------------------------------


def test_credential_expired_banner_emitted_on_credential_expired(capsys) -> None:
    """A-14: _emit_credential_expired_banner writes [ACTION REQUIRED] to stderr
    when the exception is a CredentialExpired."""
    from src.commands.gather import _emit_credential_expired_banner

    exc = CredentialExpired("PAT expired", auth_method="ADO_PAT", connector="ADO")
    _emit_credential_expired_banner(exc, "ado")

    captured = capsys.readouterr()
    assert "[ACTION REQUIRED]" in captured.err
    assert "ADO" in captured.err
    assert "ADO_PAT" in captured.err


def test_credential_expired_banner_silent_for_non_credential_error(capsys) -> None:
    """A-15: _emit_credential_expired_banner is silent for generic AuthError."""
    from src.commands.gather import _emit_credential_expired_banner

    exc = AuthError("some auth failure")
    _emit_credential_expired_banner(exc, "workiq")

    captured = capsys.readouterr()
    assert "[ACTION REQUIRED]" not in captured.err
    assert captured.err == ""
