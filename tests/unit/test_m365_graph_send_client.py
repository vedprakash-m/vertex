from __future__ import annotations

import pytest

from src.core.exceptions import AuthError, StateError
from src.m365.graph_send_client import GraphMailMessage, GraphSendClient


def test_graph_send_client_requires_graph_env(monkeypatch) -> None:
    monkeypatch.delenv("GRAPH_TENANT_ID", raising=False)
    monkeypatch.delenv("GRAPH_CLIENT_ID", raising=False)

    with pytest.raises(AuthError, match="GRAPH_TENANT_ID"):
        GraphSendClient()

    with pytest.raises(AuthError, match="GRAPH_CLIENT_ID"):
        GraphSendClient(tenant_id="tenant-1")


def test_graph_send_client_provider_primitive_remains_unavailable() -> None:
    client = GraphSendClient(tenant_id="tenant-1", client_id="client-1")
    with pytest.raises(StateError, match="no credential or HTTP request"):
        client._dispatch_qualified_mail(
            GraphMailMessage(
                to=("jordan@example.com",),
                cc=("author@example.com",),
                subject="Need your update",
                html_body="<p>Hello</p>",
            )
        )


def test_graph_send_client_public_path_fails_before_provider_access() -> None:
    client = GraphSendClient(tenant_id="tenant-1", client_id="client-1")
    with pytest.raises(StateError, match="outbox"):
        client.send_mail(
            GraphMailMessage(to=("jordan@example.com",), cc=(), subject="Need your update", html_body="<p>Hello</p>")
        )


def test_graph_send_client_never_falls_through_after_capability_check(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.m365.graph_send_client.require_dispatchable_operation",
        lambda operation: None,
    )
    client = GraphSendClient(tenant_id="tenant-1", client_id="client-1")
    with pytest.raises(StateError, match="provider primitive"):
        client.send_mail(
            GraphMailMessage(to=("jordan@example.com",), cc=(), subject="Need your update", html_body="<p>Hello</p>")
        )