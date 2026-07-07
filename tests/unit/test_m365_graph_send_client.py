from __future__ import annotations

import types

import pytest

from src.core.exceptions import AuthError, QueryError
from src.m365.graph_send_client import GraphMailMessage, GraphSendClient


class _FakeCredential:
    def __init__(self, *, tenant_id: str, client_id: str) -> None:
        self.tenant_id = tenant_id
        self.client_id = client_id

    def get_token(self, _scope: str):
        return types.SimpleNamespace(token="token-123")


class _FakeResponse:
    def __init__(self, status_code: int, *, text: str = "", headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


def test_graph_send_client_requires_graph_env(monkeypatch) -> None:
    monkeypatch.delenv("GRAPH_TENANT_ID", raising=False)
    monkeypatch.delenv("GRAPH_CLIENT_ID", raising=False)

    with pytest.raises(AuthError, match="GRAPH_TENANT_ID"):
        GraphSendClient()


def test_graph_send_client_posts_expected_payload(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _post(url, *, headers, json, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResponse(202)

    monkeypatch.setattr("src.m365.graph_send_client._get_device_code_credential_class", lambda: _FakeCredential)
    monkeypatch.setattr("src.m365.graph_send_client._get_requests_module", lambda: types.SimpleNamespace(post=_post))

    client = GraphSendClient(tenant_id="tenant-1", client_id="client-1")
    client.send_mail(
        GraphMailMessage(
            to=("jordan@example.com",),
            cc=("author@example.com",),
            subject="Need your update",
            html_body="<p>Hello</p>",
        )
    )

    assert captured["url"] == "https://graph.microsoft.com/v1.0/me/sendMail"
    assert captured["headers"] == {
        "Authorization": "Bearer token-123",
        "Content-Type": "application/json",
    }
    assert captured["json"] == {
        "message": {
            "subject": "Need your update",
            "body": {
                "contentType": "HTML",
                "content": "<p>Hello</p>",
            },
            "toRecipients": [{"emailAddress": {"address": "jordan@example.com"}}],
            "ccRecipients": [{"emailAddress": {"address": "author@example.com"}}],
        },
        "saveToSentItems": True,
    }
    assert captured["timeout"] == 30


def test_graph_send_client_surfaces_graph_errors(monkeypatch) -> None:
    monkeypatch.setattr("src.m365.graph_send_client._get_device_code_credential_class", lambda: _FakeCredential)
    monkeypatch.setattr(
        "src.m365.graph_send_client._get_requests_module",
        lambda: types.SimpleNamespace(post=lambda *args, **kwargs: _FakeResponse(403, text="forbidden")),
    )

    client = GraphSendClient(tenant_id="tenant-1", client_id="client-1")

    with pytest.raises(AuthError, match="Mail.Send"):
        client.send_mail(GraphMailMessage(to=("jordan@example.com",), cc=(), subject="Need your update", html_body="<p>Hello</p>"))


def test_graph_send_client_surfaces_retryable_errors(monkeypatch) -> None:
    monkeypatch.setattr("src.m365.graph_send_client._get_device_code_credential_class", lambda: _FakeCredential)
    monkeypatch.setattr(
        "src.m365.graph_send_client._get_requests_module",
        lambda: types.SimpleNamespace(post=lambda *args, **kwargs: _FakeResponse(429, headers={"Retry-After": "7"})),
    )

    client = GraphSendClient(tenant_id="tenant-1", client_id="client-1")

    with pytest.raises(QueryError, match="Retry-After: 7"):
        client.send_mail(GraphMailMessage(to=("jordan@example.com",), cc=(), subject="Need your update", html_body="<p>Hello</p>"))