"""REV mail hydrator (Zone C) — FR-PCI-5.

specs/program-context-intelligence.md §5.5/§5.6/§5.7. Hydrates one enumerated
mail candidate to canonical text + chunks, climbing the **body ladder**:

    uniqueBody  →  full body  →  conversation  →  attachment_flag  →  metadata_only

``uniqueBody`` is the spec-preferred canonical body (de-duplicated across the
conversation). When neither unique nor full body is available, the hydrator
falls back to ``metadata_only`` (the candidate still stages for triage but
carries no extractable body — ``metadata_only=True``).

Privacy gate (§5.7 Stage 1 step 2) runs **before** normalization produces the
canonical text: ``privacy.run_local_checks`` fail-closes on a credential hit
(quarantine — the content never reaches extraction) and denies over-sensitive
or over-size bodies. Only passing content is normalized (PII redacted into the
canonical text) and chunked (§5.6 fixed-order pipeline).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

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
from src.m365.rev.enumerators import MailboxContext
from src.m365.rev.graph_client import GraphMessage, RevGraphClient

log = logging.getLogger(__name__)

# Hydration ladder rungs (§5.5/§5.6) — recorded on HydratedContent for
# recall/cost telemetry. P1 implements unique_body / full_body / metadata_only;
# conversation + attachment rungs are flagged for P2.
RUNG_UNIQUE_BODY = "unique_body"
RUNG_FULL_BODY = "full_body"
RUNG_CONVERSATION = "conversation"
RUNG_ATTACHMENT = "attachment_flag"
RUNG_METADATA_ONLY = "metadata_only_flagged"


@dataclass(frozen=True, slots=True)
class _ChosenBody:
    text: str
    is_html: bool
    rung: str
    metadata_only: bool


def _choose_body(message: GraphMessage) -> _ChosenBody:
    """Climb the body ladder (§5.5) and return the best available body."""
    if message.unique_body:
        return _ChosenBody(
            text=message.unique_body,
            is_html=message.unique_body_content_type.lower() == "html",
            rung=RUNG_UNIQUE_BODY,
            metadata_only=False,
        )
    if message.body:
        return _ChosenBody(
            text=message.body,
            is_html=message.body_content_type.lower() == "html",
            rung=RUNG_FULL_BODY,
            metadata_only=False,
        )
    # Conversation / attachment rungs (P2): a real hydrator would fetch the
    # conversation thread or attachment text here. P1 records the fallback.
    return _ChosenBody(
        text=message.subject or "",
        is_html=False,
        rung=RUNG_METADATA_ONLY,
        metadata_only=True,
    )


class MailHydrator:
    """ContentHydrator for the mail surface (§5.6 fixed-order normalization)."""

    entity_type = EntityType.MESSAGE

    def __init__(
        self,
        graph: RevGraphClient,
        mailbox: MailboxContext,
        *,
        max_body_bytes: int = 1_048_576,
    ) -> None:
        self._graph = graph
        self._mailbox = mailbox
        self._max_body_bytes = max_body_bytes

    def hydrate(
        self,
        candidate: EnumeratedCandidate,
        *,
        correlation_id: str,
    ) -> PortResult[HydratedContent]:
        result = self._graph.get_message(
            mailbox=self._mailbox.principal_mailbox,
            message_id=candidate.locator.resource_id,
            correlation_id=correlation_id,
        )
        if isinstance(result, (Forbidden, RateLimited, Unsupported)):
            return result
        if isinstance(result, Incomplete):
            # Not found / partial — surface as metadata_only fallback so the
            # candidate still stages for triage rather than dropping silently.
            return self._metadata_only_fallback(candidate, correlation_id, reason=result.reason)
        message: GraphMessage = result.value

        chosen = _choose_body(message)
        if chosen.metadata_only:
            return self._build_metadata_only(candidate, message, correlation_id)

        # Privacy gate (§5.7 Stage 1 step 2) — fail-closed before normalization.
        local = run_local_checks(
            chosen.text,
            source_type=self.entity_type,
            sensitivity_label=None,
            max_bytes=self._max_body_bytes,
        )
        if not local.passed:
            if local.quarantined:
                return Forbidden(scope="content", reason=local.reason)
            if local.sensitivity_denied:
                return Forbidden(scope="sensitivity", reason=local.reason)
            if local.size_exceeded:
                return Unsupported(entity_type=self.entity_type.value, reason=local.reason)
            return Unsupported(entity_type=self.entity_type.value, reason=local.reason or "local_check_failed")

        norm = normalize(chosen.text, is_html=chosen.is_html, source_type=self.entity_type)
        identity = CanonicalItemIdentity(
            source_type=self.entity_type,
            tenant_id=self._mailbox.tenant_id,
            principal_mailbox=self._mailbox.principal_mailbox,
            container=self._mailbox.container,
            resource_id=message.message_id,
            immutable_id=message.immutable_id or None,
            etag=message.etag or None,
        )
        hydrated = HydratedContent(
            identity=identity,
            canonical_text=norm.canonical_text,
            normalized_source_hash=norm.normalized_source_hash,
            chunks=norm.chunks,
            route_metadata={
                "conversation_id": message.conversation_id,
                "subject": message.subject,
                "sender": message.sender,
                "received_at": message.received_at,
                "rung": chosen.rung,
            },
            hydration_rung=chosen.rung,
            metadata_only=False,
            retrieved_at=None,
            correlation_id=correlation_id,
        )
        return Success(hydrated)

    def _metadata_only_fallback(
        self,
        candidate: EnumeratedCandidate,
        correlation_id: str,
        *,
        reason: str,
    ) -> PortResult[HydratedContent]:
        log.warning("rev.mail.hydrate metadata_only_fallback cid=%s reason=%s", correlation_id, reason)
        identity = CanonicalItemIdentity(
            source_type=self.entity_type,
            tenant_id=candidate.locator.tenant_id,
            principal_mailbox=candidate.locator.principal_mailbox,
            container=candidate.locator.container,
            resource_id=candidate.locator.resource_id,
            immutable_id=None,
            etag=candidate.locator.etag_hint,
        )
        subject = str(candidate.partial_metadata.get("subject", ""))
        norm = normalize(subject, is_html=False, source_type=self.entity_type)
        return Success(
            HydratedContent(
                identity=identity,
                canonical_text=norm.canonical_text,
                normalized_source_hash=norm.normalized_source_hash,
                chunks=norm.chunks,
                route_metadata={
                    "conversation_id": candidate.partial_metadata.get("conversation_id", ""),
                    "fallback_reason": reason,
                },
                hydration_rung=RUNG_METADATA_ONLY,
                metadata_only=True,
                retrieved_at=None,
                correlation_id=correlation_id,
            )
        )

    def _build_metadata_only(
        self,
        candidate: EnumeratedCandidate,
        message: GraphMessage,
        correlation_id: str,
    ) -> PortResult[HydratedContent]:
        identity = CanonicalItemIdentity(
            source_type=self.entity_type,
            tenant_id=self._mailbox.tenant_id,
            principal_mailbox=self._mailbox.principal_mailbox,
            container=self._mailbox.container,
            resource_id=message.message_id,
            immutable_id=message.immutable_id or None,
            etag=message.etag or None,
        )
        norm = normalize(message.subject or "", is_html=False, source_type=self.entity_type)
        return Success(
            HydratedContent(
                identity=identity,
                canonical_text=norm.canonical_text,
                normalized_source_hash=norm.normalized_source_hash,
                chunks=norm.chunks,
                route_metadata={
                    "conversation_id": message.conversation_id,
                    "subject": message.subject,
                    "sender": message.sender,
                    "received_at": message.received_at,
                    "rung": RUNG_METADATA_ONLY,
                },
                hydration_rung=RUNG_METADATA_ONLY,
                metadata_only=True,
                retrieved_at=None,
                correlation_id=correlation_id,
            )
        )


__all__ = [
    "MailHydrator",
    "RUNG_UNIQUE_BODY",
    "RUNG_FULL_BODY",
    "RUNG_CONVERSATION",
    "RUNG_ATTACHMENT",
    "RUNG_METADATA_ONLY",
]