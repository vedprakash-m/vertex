"""REV capability ports + shared data contracts (Zone A).

specs/program-context-intelligence.md §5.10. These are the Zone-A port
interfaces (``typing.Protocol``) that Zone-C clients (``src/m365/rev/``) and
Zone-B extraction implement against. Ports return the typed ``PortResult``
union (§5.3/§5.10) — never bare items, never cross-boundary exceptions.

No provider SDK is imported here. The ports are surface-agnostic; a
``RetrievalIntent`` (§5.2) is the port-agnostic input and each Zone-C compiler
adapts it to its surface's grammar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from src.core.rev.entity_types import EntityType
from src.core.rev.identity import CanonicalItemIdentity, HydrationLocator
from src.core.rev.result import PortResult

if TYPE_CHECKING:
    from src.core.rev.query_planner import RetrievalIntent


@dataclass(frozen=True, slots=True)
class EnumeratedCandidate:
    """One row of enumerator output (§5.3).

    Carries the pre-hydration ``HydrationLocator`` (the deterministic mail path
    fills ``resource_id`` from native ``message.id``), a relevance score for
    prioritization, partial metadata for the planner/telemetry, and a
    correlation id threaded end-to-end (§5.10).
    """

    locator: HydrationLocator
    relevance_score: float
    partial_metadata: dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""
    enumerator: str = ""          # "collection_search" | "search_api" | "delta"
    received_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Chunk:
    """A normalized chunk with stable id + source offsets (§5.6).

    Offsets are codepoints into the canonical normalized text (post HTML/MIME
    strip, post quoted-reply removal, post PII scrub) — NOT into the original
    bytes. ``chunk_id`` is stable so overlapping extractions reconcile
    deterministically.
    """

    chunk_id: str
    text: str
    start_codepoint: int
    end_codepoint: int
    overlap_with_previous: int = 0


@dataclass(frozen=True, slots=True)
class HydratedContent:
    """The hydrator's output for one candidate (§5.5/§5.6).

    ``canonical_text`` is the canonical normalized representation spans are
    defined against (§5.7). ``chunks`` are the extraction units. ``route`` is
    the hydration-derived route metadata used by ``ItemToRouteBinder``.
    ``hydration_rung`` records which ladder rung produced the content
    (``unique_body`` | ``full_body`` | ``conversation`` | ``attachment_flag``)
    for recall/cost telemetry, and ``metadata_only`` flags the
    ``metadata_only_flagged`` fallback (§5.6).
    """

    identity: CanonicalItemIdentity
    canonical_text: str
    normalized_source_hash: str          # SHA-256 of the full canonical text
    chunks: tuple[Chunk, ...]
    route_metadata: dict[str, Any] = field(default_factory=dict)
    hydration_rung: str = "unique_body"
    metadata_only: bool = False
    retrieved_at: datetime | None = None
    correlation_id: str = ""


class CandidateEnumerator(Protocol):
    """FR-PCI-2 — enumerates candidates for one entity type (§5.3)."""

    entity_type: EntityType

    def enumerate(
        self,
        intent: "RetrievalIntent",
        *,
        correlation_id: str,
    ) -> PortResult[tuple[EnumeratedCandidate, ...]]:
        ...


class ContentHydrator(Protocol):
    """FR-PCI-5 — hydrates + normalizes + chunks one candidate (§5.6)."""

    def hydrate(
        self,
        candidate: EnumeratedCandidate,
        *,
        correlation_id: str,
    ) -> PortResult[HydratedContent]:
        ...


class ChangeFeed(Protocol):
    """FR-PCI-4 — delta change feed that primes the enumerator/hydrator cache.

    P2 (REV-10). Returns the changed/tombstoned locators for a window plus a
    ``delta_link`` to resume from. Tombstones cascade to extraction-cache
    eviction (§5.5/§5.6).
    """

    def changes(
        self,
        *,
        delta_link: str | None,
        correlation_id: str,
    ) -> PortResult[tuple[EnumeratedCandidate, ...]]:
        ...


class SemanticChunkRetriever(Protocol):
    """FR-PCI-10 — Copilot Retrieval semantic chunks (P3, license-gated)."""

    def retrieve(
        self,
        intent: "RetrievalIntent",
        *,
        correlation_id: str,
    ) -> PortResult[tuple[Chunk, ...]]:
        ...


class RevExtractor(Protocol):
    """FR-PCI-6 — extract structured claims + evidence spans from hydrated content.

    Zone A port definition. Return type is ``PortResult[Any]`` here (Zone A cannot
    depend on ``ExtractedClaim`` from Zone B); concrete implementations in Zone B
    satisfy this via structural (duck-type) compatibility.
    """

    def extract(
        self,
        hydrated: "HydratedContent",
        *,
        correlation_id: str,
    ) -> "PortResult[Any]":
        ...


class EvidenceVerifier(Protocol):
    """FR-PCI-8 — layered verification (§5.9). P1 ships quote_span + consistency."""

    def verify(
        self,
        candidate_id: str,
        claim: dict[str, Any],
        evidence: tuple[Any, ...],
        *,
        correlation_id: str,
    ) -> PortResult[tuple[Any, ...]]:
        ...