from __future__ import annotations

from typing import Any, Mapping

from src.core.actuation_outbox import require_dispatchable_operation
from src.core.exceptions import AuthError, StateError
from src.core.ledger.durable_outbox_store import OutboxError


class TeamsWebhookClient:
    def __init__(self, *, webhook_url: str) -> None:
        normalized = webhook_url.strip()
        if not normalized:
            raise AuthError("Missing Teams incoming webhook URL.")
        if not normalized.lower().startswith("https://"):
            raise AuthError("Teams incoming webhook URL must use https.")
        self._webhook_url = normalized

    def post_card(self, payload: Mapping[str, Any]) -> None:
        try:
            require_dispatchable_operation("teams_message.v1")
        except OutboxError as error:
            raise StateError(
                "Direct Teams webhook delivery is blocked: teams_message.v1 has no "
                "qualified durable-outbox capability."
            ) from error
        raise StateError(
            "Direct Teams webhook delivery is blocked; invoke the provider primitive "
            "only from a qualified durable-outbox callback."
        )

    def _dispatch_qualified_card(self, payload: Mapping[str, Any]) -> None:
        del payload
        raise StateError(
            "Teams provider dispatch is unavailable until an outbox callback is "
            "separately qualified; no webhook request was attempted."
        )