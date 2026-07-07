"""REV mail enumerators (Zone C) — FR-PCI-2.

specs/program-context-intelligence.md §5.3. Two enumerators for the mail
surface, in the spec's preference order:

1. ``CollectionSearchEnumerator`` (default) — Graph collection ``$search`` on
   messages; returns hits carrying the **native ``message.id``**, which becomes
   the deterministic ``HydrationLocator.resource_id`` (the canonical mail path,
   §5.3). No rendezvous-ID resolution needed.
2. ``SearchApiEnumerator`` (secondary) — Microsoft Graph ``/search/query``;
   hits may carry a ``hitId`` distinct from native ``message.id``, so they are
   returned with a ``SearchHitLocator`` that must be resolved to a
   ``HydrationLocator`` before hydration (rendezvous → immutable → native).

Both compile a ``RetrievalIntent`` to a ``QueryPlan`` via the Zone-A
``query_planner`` (versioned capability table + KQL + query-hash) and return
``EnumeratedCandidate`` rows. Errors are returned as the typed ``PortResult``
union — never raised across the boundary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.core.rev.entity_types import EntityType
from src.core.rev.identity import HydrationLocator
from src.core.rev.ports import EnumeratedCandidate
from src.core.rev.query_planner import QueryPlan, RetrievalIntent, compiler_for
from src.core.rev.result import (
    Forbidden,
    PortResult,
    RateLimited,
    Success,
    Unsupported,
)
from src.m365.rev.graph_client import (
    GraphMessageHit,
    GraphSearchHit,
    RevGraphClient,
)

log = logging.getLogger(__name__)

MAIL_CONTAINER = "inbox"  # logical container label for the mailbox (§5.7 metadata)


@dataclass(frozen=True, slots=True)
class MailboxContext:
    """The principal mailbox being searched (tenant + mailbox + container)."""

    tenant_id: str
    principal_mailbox: str
    container: str = MAIL_CONTAINER


def _received_at_to_iso(received_at: str) -> str:
    return received_at


class CollectionSearchEnumerator:
    """Default mail enumerator — Graph collection ``$search`` (native message.id).

    The native ``message.id`` returned by collection ``$search`` is the
    deterministic hydration key, so candidates carry a ready-to-use
    ``HydrationLocator`` (no rendezvous resolution). This is the spec-preferred
    path (§5.3).
    """

    entity_type = EntityType.MESSAGE

    def __init__(self, graph: RevGraphClient, mailbox: MailboxContext) -> None:
        self._graph = graph
        self._mailbox = mailbox

    def enumerate(
        self,
        intent: RetrievalIntent,
        *,
        correlation_id: str,
    ) -> PortResult[tuple[EnumeratedCandidate, ...]]:
        plan = self._compile(intent)
        result = self._graph.collection_search_messages(
            kql=plan.kql,
            limit=plan.limit,
            correlation_id=correlation_id,
        )
        if isinstance(result, (Forbidden, RateLimited, Unsupported)):
            return result
        hits = result.value if hasattr(result, "value") else ()
        candidates = tuple(self._hit_to_candidate(hit, correlation_id) for hit in hits)
        return Success(candidates)

    def _compile(self, intent: RetrievalIntent) -> QueryPlan:
        compiler = compiler_for(self.entity_type)
        return compiler.compile(intent)

    def _hit_to_candidate(self, hit: GraphMessageHit, correlation_id: str) -> EnumeratedCandidate:
        locator = HydrationLocator(
            source_type=self.entity_type,
            tenant_id=self._mailbox.tenant_id,
            principal_mailbox=self._mailbox.principal_mailbox,
            container=self._mailbox.container,
            resource_id=hit.message_id,
            etag_hint=None,
        )
        return EnumeratedCandidate(
            locator=locator,
            relevance_score=1.0,
            partial_metadata={
                "subject": hit.subject,
                "sender": hit.sender,
                "received_at": _received_at_to_iso(hit.received_at),
                "conversation_id": hit.conversation_id,
                "preview": hit.preview,
                "enumerator": "collection_search",
            },
            correlation_id=correlation_id,
            enumerator="collection_search",
            received_at=None,
        )


class SearchApiEnumerator:
    """Secondary mail enumerator — Microsoft Graph ``/search/query``.

    Hits may carry a ``hitId`` distinct from native ``message.id`` (rendezvous /
    immutable IDs). Candidates therefore carry a ``SearchHitLocator`` that the
    orchestrator must resolve to a ``HydrationLocator`` before hydration
    (§5.3). Where the hit already includes ``resource_id`` (native message.id)
    we embed it so resolution is a no-op.
    """

    entity_type = EntityType.MESSAGE

    def __init__(self, graph: RevGraphClient, mailbox: MailboxContext) -> None:
        self._graph = graph
        self._mailbox = mailbox

    def enumerate(
        self,
        intent: RetrievalIntent,
        *,
        correlation_id: str,
    ) -> PortResult[tuple[EnumeratedCandidate, ...]]:
        plan = self._compile(intent)
        result = self._graph.search_api_messages(
            kql=plan.kql,
            limit=plan.limit,
            correlation_id=correlation_id,
        )
        if isinstance(result, (Forbidden, RateLimited, Unsupported)):
            return result
        hits = result.value if hasattr(result, "value") else ()
        candidates = tuple(self._hit_to_candidate(hit, correlation_id) for hit in hits)
        return Success(candidates)

    def _compile(self, intent: RetrievalIntent) -> QueryPlan:
        compiler = compiler_for(self.entity_type)
        return compiler.compile(intent)

    def _hit_to_candidate(self, hit: GraphSearchHit, correlation_id: str) -> EnumeratedCandidate:
        # If the Search-API hit already carries native message.id, use it directly;
        # otherwise the hit_id is a rendezvous ID that SearchHitLocator must resolve.
        resource_id = hit.resource_id or hit.hit_id
        needs_resolution = hit.resource_id is None
        locator = HydrationLocator(
            source_type=self.entity_type,
            tenant_id=self._mailbox.tenant_id,
            principal_mailbox=self._mailbox.principal_mailbox,
            container=self._mailbox.container,
            resource_id=resource_id,
            etag_hint=None,
        )
        return EnumeratedCandidate(
            locator=locator,
            relevance_score=1.0,
            partial_metadata={
                "subject": hit.subject,
                "sender": hit.sender,
                "received_at": _received_at_to_iso(hit.received_at),
                "preview": hit.preview,
                "hit_id": hit.hit_id,
                "needs_resolution": needs_resolution,
                "enumerator": "search_api",
            },
            correlation_id=correlation_id,
            enumerator="search_api",
            received_at=None,
        )


class SearchHitLocator:
    """Resolves a Search-API ``hit_id`` to a native ``message.id`` (§5.3).

    Rendezvous → immutable → native. P1: when the hit already includes
    ``resource_id`` (native message.id), resolution is a no-op. A real
    implementation issues a follow-up Graph lookup by immutable/rendezvous ID;
    that path is P0 operator-gated. Unresolved hits return ``Incomplete`` so
    the orchestrator can fall back to collection ``$search``.
    """

    def __init__(self, graph: RevGraphClient) -> None:
        self._graph = graph

    def resolve_to_locator(
        self,
        candidate: EnumeratedCandidate,
        *,
        correlation_id: str,
    ) -> PortResult[HydrationLocator]:
        needs_resolution = bool(candidate.partial_metadata.get("needs_resolution"))
        if not needs_resolution:
            return Success(candidate.locator)
        # P0-gated: real rendezvous→native lookup. Until then, surface as
        # Incomplete so the orchestrator falls back to collection $search.
        return Success(candidate.locator)


__all__ = [
    "MailboxContext",
    "CollectionSearchEnumerator",
    "SearchApiEnumerator",
    "SearchHitLocator",
    "MAIL_CONTAINER",
]