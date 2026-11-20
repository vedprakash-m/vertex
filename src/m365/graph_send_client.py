from __future__ import annotations

import os
from dataclasses import dataclass

from src.core.actuation_outbox import require_dispatchable_operation
from src.core.exceptions import AuthError, StateError
from src.core.ledger.durable_outbox_store import OutboxError


@dataclass(frozen=True, slots=True)
class GraphMailMessage:
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    html_body: str


class GraphSendClient:
    def __init__(self, *, tenant_id: str | None = None, client_id: str | None = None) -> None:
        self._tenant_id = (tenant_id or os.environ.get("GRAPH_TENANT_ID", "")).strip()
        self._client_id = (client_id or os.environ.get("GRAPH_CLIENT_ID", "")).strip()
        if not self._tenant_id:
            raise AuthError("Missing GRAPH_TENANT_ID for Graph mail send.")
        if not self._client_id:
            raise AuthError("Missing GRAPH_CLIENT_ID for Graph mail send.")

    def send_mail(self, message: GraphMailMessage, *, save_to_sent_items: bool = True) -> None:
        try:
            require_dispatchable_operation("email.v1")
        except OutboxError as error:
            raise StateError(
                "Direct Graph email delivery is blocked: email.v1 has no qualified "
                "durable-outbox capability."
            ) from error
        raise StateError(
            "Direct Graph email delivery is blocked; invoke the provider primitive "
            "only from a qualified durable-outbox callback."
        )

    def _dispatch_qualified_mail(
        self, message: GraphMailMessage, *, save_to_sent_items: bool = True
    ) -> None:
        del message, save_to_sent_items
        raise StateError(
            "Graph provider dispatch is unavailable until an outbox callback is "
            "separately qualified; no credential or HTTP request was attempted."
        )