from __future__ import annotations

import pytest

from src.core.exceptions import AuthError, StateError
from src.m365.outbound_dispatch import dispatch_email_via_outbox, dispatch_teams_card_via_outbox
from src.m365.graph_send_client import GraphMailMessage
from src.m365.teams_webhook_client import TeamsWebhookClient


@pytest.mark.parametrize("webhook_url", ["", "   ", "http://example.invalid/hook"])
def test_teams_client_rejects_unqualified_webhook(webhook_url: str) -> None:
    with pytest.raises(AuthError):
        TeamsWebhookClient(webhook_url=webhook_url)


def test_public_teams_path_fails_before_webhook() -> None:
    client = TeamsWebhookClient(webhook_url="https://example.invalid/hook")
    with pytest.raises(StateError, match="outbox"):
        client.post_card({"type": "AdaptiveCard"})


def test_provider_primitive_remains_unavailable() -> None:
    client = TeamsWebhookClient(webhook_url="https://example.invalid/hook")
    with pytest.raises(StateError, match="no webhook request"):
        client._dispatch_qualified_card({"type": "AdaptiveCard"})


def test_teams_client_never_falls_through_after_capability_check(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.m365.teams_webhook_client.require_dispatchable_operation",
        lambda operation: None,
    )
    client = TeamsWebhookClient(webhook_url="https://example.invalid/hook")
    with pytest.raises(StateError, match="outbox callback"):
        client.post_card({"type": "AdaptiveCard"})


def test_outbound_dispatch_never_falls_through_to_a_provider(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.m365.outbound_dispatch.require_dispatchable_operation",
        lambda operation: None,
    )
    with pytest.raises(StateError, match="outbox"):
        dispatch_email_via_outbox(
            GraphMailMessage(to=("a@example.com",), cc=(), subject="x", html_body="<p>x</p>")
        )
    with pytest.raises(StateError, match="outbox"):
        dispatch_teams_card_via_outbox({"type": "AdaptiveCard"})
