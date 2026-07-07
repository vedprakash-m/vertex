"""REV Zone-C package — M365 capability ports for Program-Context Intelligence.

specs/program-context-intelligence.md + specs/gaps.md. Zone-C implementations
of the Zone-A ``src.core.rev.ports`` protocols for the Microsoft Graph mail
(P1), calendar (P2), SharePoint (P2), and Teams (P2) surfaces. The mail walking
skeleton ships against the mockable ``RevGraphClient``.

**Graph API pivot (ADR-008):** all delegated Microsoft Graph API scopes
(``Mail.Read``, ``Calendars.Read``, ``Chat.Read``, ``Files.Read.All``,
``Sites.Read.All``) are **permanently blocked by Microsoft IT policy** for custom
Entra app registrations — there is no consent path. Live Graph retrieval is
non-functional and never will be. The Phase 1 production path is **local export
import**: ``EmlEnumerator`` + ``EmlHydrator`` (``.eml``), with ``.ics`` / Teams /
docs importers in Phase 3. The re-exported ``*DeltaFeed`` and ``Live*Hydrator``
names below are **DEPRECATED** (kept only for test scaffolding and import
stability) — see ``docs/adrs/adr-008-graph-api-pivot.md``. The
``CandidateEnumerator`` / ``ContentHydrator`` port interfaces remain so a
``GraphEnumerator``+``GraphHydrator`` could substitute if IT policy ever changes,
without pipeline changes.

Zone C: imports only ``src.core.*`` + stdlib. Never imports ``src.ai`` /
``src.commands``, and never imports the ledger event-write API — REV candidates
are staged only via ``candidate_store.append_candidate``.
"""

from __future__ import annotations

from src.m365.rev.calendar_hydrator import (
    CalendarContext,
    CalendarHydrator,
    FakeRevCalendarClient,
    GraphEvent,
    LiveCalendarHydrator,
)
from src.m365.rev.change_feeds import (
    CalendarDeltaFeed,
    DeltaPage,
    DeltaTombstone,
    FakeChangeFeed,
    MailDeltaFeed,
    SharePointDriveItemDeltaFeed,
)
from src.m365.rev.graph_client import (
    FakeRevGraphClient,
    GraphMessage,
    GraphMessageHit,
    GraphSearchHit,
    RevGraphClient,
)
from src.m365.rev.sharepoint_hydrator import (
    FakeRevSharePointClient,
    GraphDriveItem,
    LiveSharePointHydrator,
    SharePointContext,
    SharePointHydrator,
)
from src.m365.rev.teams_hydrator import (
    FakeRevTeamsClient,
    GraphTeamsMessage,
    LiveTeamsHydrator,
    TeamsContext,
    TeamsHydrator,
    TeamsHydratorContext,
)

__all__ = [
    # Mail (P1)
    "RevGraphClient",
    "FakeRevGraphClient",
    "GraphMessage",
    "GraphMessageHit",
    "GraphSearchHit",
    # Calendar (P2)
    "GraphEvent",
    "FakeRevCalendarClient",
    "CalendarContext",
    "CalendarHydrator",
    "LiveCalendarHydrator",
    # SharePoint (P2)
    "GraphDriveItem",
    "FakeRevSharePointClient",
    "SharePointContext",
    "SharePointHydrator",
    "LiveSharePointHydrator",
    # Teams (P2)
    "GraphTeamsMessage",
    "TeamsContext",
    "FakeRevTeamsClient",
    "TeamsHydratorContext",
    "TeamsHydrator",
    "LiveTeamsHydrator",
    # ChangeFeed / delta (P2)
    "DeltaTombstone",
    "DeltaPage",
    "FakeChangeFeed",
    "MailDeltaFeed",
    "CalendarDeltaFeed",
    "SharePointDriveItemDeltaFeed",
]