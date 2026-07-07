"""REV ChangeFeed / delta port implementations (Zone C) — FR-PCI-4.

.. deprecated::
   ``MailDeltaFeed``, ``CalendarDeltaFeed``, and ``SharePointDriveItemDeltaFeed``
   are **permanently non-functional**. All delegated Microsoft Graph API scopes
   required for live delta feeds (``Mail.Read``, ``Calendars.Read``,
   ``Files.Read.All``) were permanently blocked by Microsoft IT policy — no consent
   path exists. See ``docs/adrs/adr-008-graph-api-pivot.md`` for the decision
   record and the pivot to local ``.eml``/``.ics`` export import.

   ``FakeChangeFeed`` is kept for tests and remains fully functional.

specs/program-context-intelligence.md §5.5. Delta feeds prime the
enumerator/hydrator cache — they are NOT peer providers; they return changed
``EnumeratedCandidate`` records (including tombstones) so the pipeline can:
  * skip unchanged items (G-refresh),
  * evict extraction-cache entries for tombstoned (deleted) items (§5.5).

**Operational:**
* ``FakeChangeFeed`` — in-memory, fully controllable for tests. Can inject
  changed items and tombstones.

**Permanently blocked (IT policy — do not attempt to revive):**
* ``MailDeltaFeed`` — requires ``Mail.Read`` (blocked).
* ``CalendarDeltaFeed`` — requires ``Calendars.Read`` (blocked).
* ``SharePointDriveItemDeltaFeed`` — requires ``Files.Read.All`` (blocked).

**Replacement:** ``EmlEnumerator`` + ``EmlHydrator`` (Phase 1) provide the same
pipeline feed from locally-exported ``.eml`` files, with no API credentials.

Zone C: imports only ``src.core.*`` + stdlib.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.core.rev.entity_types import EntityType
from src.core.rev.identity import HydrationLocator
from src.core.rev.ports import EnumeratedCandidate
from src.core.rev.result import (
    Incomplete,
    PortResult,
    RateLimited,
    Success,
    Unsupported,
)
from src.core.rev.sync_state import SyncState, SyncStateStore

log = logging.getLogger(__name__)

# Sentinel delta_link value used by FakeChangeFeed to simulate "no more changes".
_FAKE_EXHAUSTED = "fake://exhausted"


# ---------------------------------------------------------------------------
# Tombstone record (§5.5 tombstone cascade)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeltaTombstone:
    """A deleted-item notification from a delta feed.

    The caller must evict the extraction-cache entry for ``canonical_id`` (the
    cache key from ``CanonicalItemIdentity.cache_key``) to satisfy the
    compliance data-minimisation requirement: do not retain LLM extractions for
    a deleted M365 item.
    """

    canonical_id: str        # CanonicalItemIdentity.cache_key of the deleted item
    resource_id: str         # native Graph resource id
    source_type: EntityType
    deleted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class DeltaPage:
    """One page of delta-feed results."""

    changed: tuple[EnumeratedCandidate, ...]
    tombstones: tuple[DeltaTombstone, ...]
    delta_link: str | None       # None = more pages; non-None = caught up
    more_available: bool = True  # False once delta_link is stable


# ---------------------------------------------------------------------------
# Zone-A port — re-exported here for completeness (defined in ports.py)
# ---------------------------------------------------------------------------

# ``ChangeFeed`` Protocol is in ``src.core.rev.ports``.  The implementations
# here satisfy that protocol via duck-typing; we do NOT import the Protocol to
# avoid the Zone-A→Zone-C import direction.


# ---------------------------------------------------------------------------
# FakeChangeFeed — deterministic, in-process (tests + --mock-fixture)
# ---------------------------------------------------------------------------


class FakeChangeFeed:
    """In-memory delta feed for tests.

    ``inject_changes`` queues changed candidates; ``inject_tombstones`` queues
    deletions. Each call to ``changes`` drains one page and returns the next
    delta_link (or ``None`` when caught up).
    """

    entity_type: EntityType = EntityType.MESSAGE

    def __init__(
        self,
        entity_type: EntityType = EntityType.MESSAGE,
        *,
        pages: list[DeltaPage] | None = None,
    ) -> None:
        self.entity_type = entity_type
        self._pages: list[DeltaPage] = pages or []
        self._page_index = 0

    def inject_page(self, page: DeltaPage) -> None:
        self._pages.append(page)

    def changes(
        self,
        *,
        delta_link: str | None,
        correlation_id: str,
    ) -> PortResult[tuple[EnumeratedCandidate, ...]]:
        if self._page_index >= len(self._pages):
            # No more changes — return a stable empty page.
            return Success(())
        page = self._pages[self._page_index]
        self._page_index += 1
        # Tombstones are surfaced in the changed tuple with a special metadata key.
        tombstoned: list[EnumeratedCandidate] = [
            EnumeratedCandidate(
                locator=HydrationLocator(
                    source_type=t.source_type,
                    tenant_id="",
                    principal_mailbox="",
                    container="",
                    resource_id=t.resource_id,
                ),
                relevance_score=0.0,
                partial_metadata={"deleted": True, "canonical_id": t.canonical_id},
                correlation_id=correlation_id,
                enumerator="delta_tombstone",
            )
            for t in page.tombstones
        ]
        all_candidates = tuple(page.changed) + tuple(tombstoned)
        return Success(all_candidates)

    def peek_delta_link(self) -> str | None:
        """Return the delta_link the current page would have emitted."""
        if self._page_index > 0 and self._page_index <= len(self._pages):
            return self._pages[self._page_index - 1].delta_link
        return None


# ---------------------------------------------------------------------------
# MailDeltaFeed — operator-gated (P2; requires Mail.Read live consent)
# ---------------------------------------------------------------------------


class MailDeltaFeed:
    """Graph folder-scoped mail delta feed (§5.5 / FR-PCI-4).

    .. deprecated:: P1-0
       ``Mail.Read`` scope permanently blocked by Microsoft IT policy. This class
       always returns ``Unsupported``. Use ``EmlEnumerator`` + ``EmlHydrator``
       from local ``.eml`` export instead. See ``docs/adrs/adr-008-graph-api-pivot.md``.
    """

    entity_type: EntityType = EntityType.MESSAGE

    _RV_S1_DELTA_GATE = (
        "MailDeltaFeed: PERMANENTLY BLOCKED — Mail.Read scope denied by Microsoft IT policy. "
        "No consent path available. Use EmlEnumerator + EmlHydrator for local .eml import. "
        "See docs/adrs/adr-008-graph-api-pivot.md."
    )

    def __init__(
        self,
        *,
        graph_client: Any | None = None,       # RevGraphClient (injected when live)
        sync_store: SyncStateStore | None = None,
        mailbox_tenant_id: str = "",
        principal_mailbox: str = "",
        folder_id: str = "inbox",
        api_version: str = "v1.0",
    ) -> None:
        self._graph = graph_client
        self._sync_store = sync_store
        self._mailbox_tenant_id = mailbox_tenant_id
        self._principal_mailbox = principal_mailbox
        self._folder_id = folder_id
        self._api_version = api_version

    def changes(
        self,
        *,
        delta_link: str | None,
        correlation_id: str,
    ) -> PortResult[tuple[EnumeratedCandidate, ...]]:
        if self._graph is None:
            log.warning(self._RV_S1_DELTA_GATE)
            return Unsupported(
                entity_type=EntityType.MESSAGE.value,
                reason="rv_s1_delta_gate: live Mail delta requires operator consent",
            )
        # P2 live implementation would call:
        #   GET /me/mailFolders/{folder_id}/messages/delta?$deltaToken=...
        # or use the deltaLink URL directly. For now (pre-consent), we gate.
        return Unsupported(
            entity_type=EntityType.MESSAGE.value,
            reason="rv_s1_delta_gate: MailDeltaFeed live path not yet consented",
        )


# ---------------------------------------------------------------------------
# CalendarDeltaFeed — operator-gated (P2; requires Calendars.Read live consent)
# ---------------------------------------------------------------------------


class CalendarDeltaFeed:
    """Graph calendarView delta feed (§5.5 / FR-PCI-4).

    .. deprecated:: P1-0
       ``Calendars.Read`` scope permanently blocked by Microsoft IT policy. This class
       always returns ``Unsupported``. Use local ``.ics`` export import instead.
       See ``docs/adrs/adr-008-graph-api-pivot.md``.
    """

    entity_type: EntityType = EntityType.EVENT

    _RV_S1_CALENDAR_GATE = (
        "CalendarDeltaFeed: PERMANENTLY BLOCKED — Calendars.Read scope denied by Microsoft IT policy. "
        "No consent path available. Use local .ics export import instead. "
        "See docs/adrs/adr-008-graph-api-pivot.md."
    )

    def __init__(
        self,
        *,
        graph_client: Any | None = None,
        sync_store: SyncStateStore | None = None,
        mailbox_tenant_id: str = "",
        principal_mailbox: str = "",
        start_datetime: str = "",
        end_datetime: str = "",
        api_version: str = "v1.0",
    ) -> None:
        self._graph = graph_client
        self._sync_store = sync_store
        self._mailbox_tenant_id = mailbox_tenant_id
        self._principal_mailbox = principal_mailbox
        self._start_datetime = start_datetime
        self._end_datetime = end_datetime
        self._api_version = api_version

    def changes(
        self,
        *,
        delta_link: str | None,
        correlation_id: str,
    ) -> PortResult[tuple[EnumeratedCandidate, ...]]:
        if self._graph is None:
            log.warning(self._RV_S1_CALENDAR_GATE)
            return Unsupported(
                entity_type=EntityType.EVENT.value,
                reason="rv_s1_calendar_gate: live calendar delta requires operator consent",
            )
        return Unsupported(
            entity_type=EntityType.EVENT.value,
            reason="rv_s1_calendar_gate: CalendarDeltaFeed live path not yet consented",
        )


# ---------------------------------------------------------------------------
# SharePointDriveItemDeltaFeed — operator-gated (P2)
# ---------------------------------------------------------------------------


class SharePointDriveItemDeltaFeed:
    """driveItem delta feed for SharePoint/OneDrive (§5.5 / FR-PCI-4).

    .. deprecated:: P1-0
       ``Files.Read.All`` scope permanently blocked by Microsoft IT policy. This class
       always returns ``Unsupported``. See ``docs/adrs/adr-008-graph-api-pivot.md``.
    """

    entity_type: EntityType = EntityType.DRIVE_ITEM

    _RV_S1_SHAREPOINT_GATE = (
        "SharePointDriveItemDeltaFeed: PERMANENTLY BLOCKED — Files.Read.All scope denied by Microsoft IT policy. "
        "No consent path available. See docs/adrs/adr-008-graph-api-pivot.md."
    )

    def __init__(
        self,
        *,
        graph_client: Any | None = None,
        sync_store: SyncStateStore | None = None,
        drive_id: str = "",
        api_version: str = "v1.0",
    ) -> None:
        self._graph = graph_client
        self._sync_store = sync_store
        self._drive_id = drive_id
        self._api_version = api_version

    def changes(
        self,
        *,
        delta_link: str | None,
        correlation_id: str,
    ) -> PortResult[tuple[EnumeratedCandidate, ...]]:
        if self._graph is None:
            log.warning(self._RV_S1_SHAREPOINT_GATE)
            return Unsupported(
                entity_type=EntityType.DRIVE_ITEM.value,
                reason="rv_s1_sharepoint_gate: live SharePoint delta requires operator consent",
            )
        return Unsupported(
            entity_type=EntityType.DRIVE_ITEM.value,
            reason="rv_s1_sharepoint_gate: SharePointDriveItemDeltaFeed live path not yet consented",
        )


__all__ = [
    "DeltaTombstone",
    "DeltaPage",
    "FakeChangeFeed",
    "MailDeltaFeed",
    "CalendarDeltaFeed",
    "SharePointDriveItemDeltaFeed",
]
