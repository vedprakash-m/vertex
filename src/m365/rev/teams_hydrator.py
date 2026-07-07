"""REV Teams hydrator (Zone C) — FR-PCI-5 / REV-12.

specs/program-context-intelligence.md §5.4/§5.6. Hydrates Teams chat and
channel messages to canonical text + chunks.

**Hydration paths (Phase 2, §5.4)**:
  * Chat message:    GET /chats/{chatId}/messages/{messageId}   (Chat.Read)
  * Channel message: GET /teams/{teamId}/channels/{channelId}/messages/{messageId}
                     (ChannelMessage.Read.All — heavy consent, opt-in)

**Key Teams notes (§5.2/§5.4)**:
  * chatMessage Search returns **metadata + summary only** (no body) — hydration
    via Teams GET is **mandatory** for any body content.
  * chatMessage **cannot** be combined with other entity types in one Search
    request (separate request required).
  * There is **no chatMessage delta** endpoint — Teams requires periodic
    re-enumeration via Search (Phase 2).
  * ImmutableId is **not** applicable to Teams; the stable resource key is the
    combination of chat/team+channel IDs + message ID.

**P2 operator-gated.** Requires ``Chat.Read`` (chat) or
``ChannelMessage.Read.All`` (channel) live consent (RV-S1-TEAMS-CHAT /
RV-S1-TEAMS-CHANNEL). Returns ``Unsupported`` with the spike reference until
consent is granted.

Zone C: imports only ``src.core.*`` + stdlib.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from src.core.rev.entity_types import EntityType
from src.core.rev.identity import CanonicalItemIdentity
from src.core.rev.normalizer import normalize
from src.core.rev.ports import EnumeratedCandidate, HydratedContent
from src.core.rev.privacy import run_local_checks
from src.core.rev.result import (
    Forbidden,
    Incomplete,
    PortResult,
    RateLimited,
    Success,
    Unsupported,
)

log = logging.getLogger(__name__)

RUNG_TEAMS_BODY = "teams_body"
RUNG_TEAMS_METADATA_ONLY = "metadata_only_flagged"

_RV_S1_TEAMS_CHAT_GATE = (
    "TeamsHydrator: live Teams chat hydration requires RV-S1-TEAMS-CHAT operator spike. "
    "Confirm: (1) Chat.Read consent granted, (2) GET /chats/{chatId}/messages/{messageId} "
    "returns full body (Search returns metadata+summary only), (3) no chatMessage delta "
    "(periodic re-enumeration via Search). Run 'vertex rev run --mock-fixture' for P1."
)

_RV_S1_TEAMS_CHANNEL_GATE = (
    "TeamsHydrator: live Teams channel hydration requires RV-S1-TEAMS-CHANNEL operator spike. "
    "Confirm: (1) ChannelMessage.Read.All consent granted (heavy consent, opt-in), "
    "(2) GET /teams/{teamId}/channels/{channelId}/messages/{messageId} returns full body. "
    "Run 'vertex rev run --mock-fixture' for P1."
)


# ---------------------------------------------------------------------------
# Teams message data model
# ---------------------------------------------------------------------------


class TeamsContext(str, Enum):
    CHAT = "chat"
    CHANNEL = "channel"


@dataclass(frozen=True, slots=True)
class GraphTeamsMessage:
    """A hydrated Teams chat or channel message.

    ``teams_context`` distinguishes chat (Chat.Read) from channel
    (ChannelMessage.Read.All). ``chat_id`` / ``team_id`` + ``channel_id``
    identify the container. No ImmutableId — Teams uses native resource keys.
    """

    message_id: str
    teams_context: str = TeamsContext.CHAT.value   # "chat" | "channel"
    chat_id: str = ""
    team_id: str = ""
    channel_id: str = ""
    sender: str = ""
    sent_at: str = ""
    body: str = ""
    body_content_type: str = "text"    # "text" | "html"
    subject: str = ""
    summary: str = ""        # metadata-only preview from Search
    etag: str = ""
    importance: str = ""
    has_attachments: bool = False
    reply_to_id: str = ""


class RevTeamsClient(Protocol):
    """Mockable Graph surface for the REV Teams pipeline."""

    def get_chat_message(
        self,
        *,
        chat_id: str,
        message_id: str,
        correlation_id: str = "",
    ) -> PortResult[GraphTeamsMessage]:
        ...

    def get_channel_message(
        self,
        *,
        team_id: str,
        channel_id: str,
        message_id: str,
        correlation_id: str = "",
    ) -> PortResult[GraphTeamsMessage]:
        ...


class FakeRevTeamsClient:
    """In-memory Teams client for tests / ``--mock-fixture``."""

    def __init__(
        self,
        messages: tuple[GraphTeamsMessage, ...] = (),
        *,
        rate_limited_ids: frozenset[str] = frozenset(),
        forbidden_ids: frozenset[str] = frozenset(),
    ) -> None:
        self._messages: dict[str, GraphTeamsMessage] = {m.message_id: m for m in messages}
        self._rate_limited_ids = rate_limited_ids
        self._forbidden_ids = forbidden_ids
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_chat_message(
        self,
        *,
        chat_id: str,
        message_id: str,
        correlation_id: str = "",
    ) -> PortResult[GraphTeamsMessage]:
        self.calls.append(("get_chat_message", {"chat_id": chat_id, "message_id": message_id}))
        if message_id in self._rate_limited_ids:
            return RateLimited(provider="graph_teams", retry_after_seconds=1.0)
        if message_id in self._forbidden_ids:
            return Forbidden(scope="chat", reason="forbidden_by_fake")
        msg = self._messages.get(message_id)
        if msg is None:
            return Incomplete(
                GraphTeamsMessage(message_id=message_id, teams_context=TeamsContext.CHAT.value, chat_id=chat_id),
                reason=f"chat_message_not_found:{message_id}",
            )
        return Success(msg)

    def get_channel_message(
        self,
        *,
        team_id: str,
        channel_id: str,
        message_id: str,
        correlation_id: str = "",
    ) -> PortResult[GraphTeamsMessage]:
        self.calls.append(("get_channel_message", {"team_id": team_id, "channel_id": channel_id, "message_id": message_id}))
        if message_id in self._rate_limited_ids:
            return RateLimited(provider="graph_teams", retry_after_seconds=1.0)
        if message_id in self._forbidden_ids:
            return Forbidden(scope="channel", reason="forbidden_by_fake")
        msg = self._messages.get(message_id)
        if msg is None:
            return Incomplete(
                GraphTeamsMessage(message_id=message_id, teams_context=TeamsContext.CHANNEL.value, team_id=team_id, channel_id=channel_id),
                reason=f"channel_message_not_found:{message_id}",
            )
        return Success(msg)


# ---------------------------------------------------------------------------
# TeamsHydrator
# ---------------------------------------------------------------------------


@dataclass
class TeamsHydratorContext:
    """Teams connection context."""

    tenant_id: str
    principal_mailbox: str
    teams_context: str = TeamsContext.CHAT.value   # "chat" | "channel"
    chat_id: str = ""
    team_id: str = ""
    channel_id: str = ""
    max_body_bytes: int = 1_048_576


class TeamsHydrator:
    """ContentHydrator for Teams chat/channel messages (§5.6).

    **P2 operator-gated** for the live Graph path. Phase 2 because:
      * chatMessage Search returns metadata+summary only; full-body hydration is
        mandatory, requiring Chat.Read / ChannelMessage.Read.All.
      * ChannelMessage.Read.All requires heavy consent (opt-in only).

    Fake client makes the full value chain testable without consent.
    """

    entity_type = EntityType.CHAT_MESSAGE

    def __init__(
        self,
        client: Any,     # RevTeamsClient | FakeRevTeamsClient
        context: TeamsHydratorContext,
    ) -> None:
        self._client = client
        self._context = context

    def hydrate(
        self,
        candidate: EnumeratedCandidate,
        *,
        correlation_id: str,
    ) -> PortResult[HydratedContent]:
        message_id = candidate.locator.resource_id
        # Route to chat or channel GET based on context.
        if self._context.teams_context == TeamsContext.CHAT.value:
            chat_id = self._context.chat_id or candidate.partial_metadata.get("chat_id", "")
            result = self._client.get_chat_message(
                chat_id=chat_id, message_id=message_id, correlation_id=correlation_id,
            )
        else:
            team_id = self._context.team_id or candidate.partial_metadata.get("team_id", "")
            channel_id = self._context.channel_id or candidate.partial_metadata.get("channel_id", "")
            result = self._client.get_channel_message(
                team_id=team_id, channel_id=channel_id, message_id=message_id, correlation_id=correlation_id,
            )

        if isinstance(result, (Forbidden, RateLimited, Unsupported)):
            return result
        if isinstance(result, Incomplete):
            return self._metadata_only_fallback(candidate, correlation_id, reason=result.reason)
        msg: GraphTeamsMessage = result.value

        body_text = msg.body or msg.summary
        if not body_text:
            return self._metadata_only_fallback(candidate, correlation_id, reason="no_teams_body")

        # Privacy gate.
        local = run_local_checks(
            body_text,
            source_type=self.entity_type,
            sensitivity_label=None,
            max_bytes=self._context.max_body_bytes,
        )
        if not local.passed:
            if local.quarantined:
                return Forbidden(scope="content", reason=local.reason)
            if local.sensitivity_denied:
                return Forbidden(scope="sensitivity", reason=local.reason)
            return Unsupported(entity_type=self.entity_type.value, reason=local.reason or "local_check_failed")

        is_html = msg.body_content_type.lower() == "html"
        norm = normalize(body_text, is_html=is_html, source_type=self.entity_type)
        container = (
            msg.chat_id if self._context.teams_context == TeamsContext.CHAT.value
            else f"{msg.team_id}/{msg.channel_id}"
        )
        identity = CanonicalItemIdentity(
            source_type=self.entity_type,
            tenant_id=self._context.tenant_id,
            principal_mailbox=self._context.principal_mailbox,
            container=container,
            resource_id=message_id,
            immutable_id=None,   # Teams: no Outlook ImmutableId
            etag=msg.etag or None,
        )
        route_meta: dict[str, Any] = {
            "teams_context": self._context.teams_context,
            "chat_id": msg.chat_id,
            "team_id": msg.team_id,
            "channel_id": msg.channel_id,
            "sender": msg.sender,
            "sent_at": msg.sent_at,
            "subject": msg.subject,
            "rung": RUNG_TEAMS_BODY,
        }
        return Success(
            HydratedContent(
                identity=identity,
                canonical_text=norm.canonical_text,
                normalized_source_hash=norm.normalized_source_hash,
                chunks=norm.chunks,
                route_metadata=route_meta,
                hydration_rung=RUNG_TEAMS_BODY,
                metadata_only=False,
                retrieved_at=None,
                correlation_id=correlation_id,
            )
        )

    def _metadata_only_fallback(
        self,
        candidate: EnumeratedCandidate,
        correlation_id: str,
        *,
        reason: str,
    ) -> PortResult[HydratedContent]:
        log.warning("rev.teams.hydrate metadata_only cid=%s reason=%s", correlation_id, reason)
        identity = CanonicalItemIdentity(
            source_type=self.entity_type,
            tenant_id=self._context.tenant_id,
            principal_mailbox=self._context.principal_mailbox,
            container=self._context.chat_id or f"{self._context.team_id}/{self._context.channel_id}",
            resource_id=candidate.locator.resource_id,
        )
        text = str(candidate.partial_metadata.get("summary", candidate.partial_metadata.get("subject", "")))
        norm = normalize(text, is_html=False, source_type=self.entity_type)
        return Success(
            HydratedContent(
                identity=identity,
                canonical_text=norm.canonical_text,
                normalized_source_hash=norm.normalized_source_hash,
                chunks=norm.chunks,
                route_metadata={"fallback_reason": reason},
                hydration_rung=RUNG_TEAMS_METADATA_ONLY,
                metadata_only=True,
                retrieved_at=None,
                correlation_id=correlation_id,
            )
        )


class LiveTeamsHydrator:
    """Operator-gated live TeamsHydrator stub (RV-S1-TEAMS-CHAT/CHANNEL).

    Returns ``Unsupported`` until the relevant Teams scope is consented.
    """

    entity_type = EntityType.CHAT_MESSAGE

    def __init__(self, *, for_channel: bool = False) -> None:
        self._for_channel = for_channel

    def hydrate(
        self,
        candidate: EnumeratedCandidate,
        *,
        correlation_id: str,
    ) -> PortResult[HydratedContent]:
        gate = _RV_S1_TEAMS_CHANNEL_GATE if self._for_channel else _RV_S1_TEAMS_CHAT_GATE
        log.warning(gate)
        reason = (
            "rv_s1_teams_channel_gate: live Teams channel hydration requires ChannelMessage.Read.All consent"
            if self._for_channel
            else "rv_s1_teams_chat_gate: live Teams chat hydration requires Chat.Read consent"
        )
        return Unsupported(entity_type=EntityType.CHAT_MESSAGE.value, reason=reason)


__all__ = [
    "GraphTeamsMessage",
    "TeamsContext",
    "RevTeamsClient",
    "FakeRevTeamsClient",
    "TeamsHydratorContext",
    "TeamsHydrator",
    "LiveTeamsHydrator",
    "RUNG_TEAMS_BODY",
    "RUNG_TEAMS_METADATA_ONLY",
]
