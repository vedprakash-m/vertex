"""RELO-W5.6 provider-delivery boundary for currently unqualified channels."""
from __future__ import annotations

from typing import Any, Mapping

from src.core.actuation_outbox import require_dispatchable_operation
from src.core.exceptions import StateError
from src.core.ledger.durable_outbox_store import OutboxError
from src.m365.graph_send_client import GraphMailMessage


def dispatch_email_via_outbox(message: GraphMailMessage) -> None:
    del message
    try:
        require_dispatchable_operation("email.v1")
    except OutboxError as error:
        raise StateError(
            "Email delivery is blocked: email.v1 has no qualified durable-outbox "
            "operation, marker or reconciliation capability."
        ) from error
    raise StateError("Email delivery must be driven by the durable outbox, not a direct caller.")


def dispatch_teams_card_via_outbox(payload: Mapping[str, Any]) -> None:
    del payload
    try:
        require_dispatchable_operation("teams_message.v1")
    except OutboxError as error:
        raise StateError(
            "Teams delivery is blocked: teams_message.v1 has no qualified durable-outbox "
            "operation, marker or reconciliation capability."
        ) from error
    raise StateError("Teams delivery must be driven by the durable outbox, not a direct caller.")
