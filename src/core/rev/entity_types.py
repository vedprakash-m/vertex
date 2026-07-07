"""Entity types for the REV capability-port pipeline (Zone A).

Mirrors the Microsoft Graph / Search entity types the pipeline enumerates and
hydrates. These are the only source kinds the ports reason about; surface
clients (Zone C) map Graph/Teams/SharePoint resources onto them.
"""

from __future__ import annotations

from enum import Enum


class EntityType(str, Enum):
    """A retrievable M365 source kind.

    Values are the Graph/Search entity names so telemetry and config can cite
    them verbatim without a translation layer.
    """

    MESSAGE = "message"                # Outlook mail (Graph message)
    EVENT = "event"                    # Outlook calendar event
    CHAT_MESSAGE = "chatMessage"       # Teams chat or channel message
    LIST_ITEM = "listItem"             # SharePoint list item
    DRIVE_ITEM = "driveItem"           # SharePoint/OneDrive file


# Entity types that have an Outlook ImmutableId (established at hydration GET
# via ``Prefer: IdType="ImmutableId"``). Teams and SharePoint use their own
# stable resource keys — see specs/program-context-intelligence.md §5.4.
IMMUTABLE_ID_ENTITY_TYPES: frozenset[EntityType] = frozenset({EntityType.MESSAGE, EntityType.EVENT})

# Entity types whose routes feed the §13.5 registry today.
# DRIVE_ITEM added (specs/gaps.md G4): driveitem route via site+library key.
# LIST_ITEM still unsupported (no stable route identifier available).
REGISTRY_ROUTE_ENTITY_TYPES: frozenset[EntityType] = frozenset({
    EntityType.MESSAGE,
    EntityType.CHAT_MESSAGE,
    EntityType.EVENT,
    EntityType.DRIVE_ITEM,
})


def supports_immutable_id(entity_type: EntityType) -> bool:
    return entity_type in IMMUTABLE_ID_ENTITY_TYPES


def supports_registry_route(entity_type: EntityType) -> bool:
    """True if §13.5 has a route type for this entity (mail/Teams/calendar)."""
    return entity_type in REGISTRY_ROUTE_ENTITY_TYPES