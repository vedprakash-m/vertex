"""Three-stage canonical identity resolution for REV (Zone A).

specs/program-context-intelligence.md §5.4. Outlook IDs, ``conversationId``s,
and routes are mailbox-scoped, and some identifying fields (``conversationId``,
``seriesMasterId``) are only available *after* hydration. Identity is therefore
globally scoped (tenant + principal/mailbox + container) and resolved in three
stages:

    HydrationLocator        (pre-hydration, from the enumerator)
        → hydrate
    CanonicalItemIdentity   (post-hydration, with immutable_id + etag)
        → ItemToRouteBinder.bind(... route metadata from hydration ...)
    SourceRouteIdentity     (post-route-metadata; the §13.5 durable route)

``SourceRouteIdentity`` objects feed §13.5 registry promotion. Supported
source types: mail (MESSAGE), Teams (CHAT_MESSAGE), calendar (EVENT), and
SharePoint files (DRIVE_ITEM — route key is the normalized site+library path).
LIST_ITEM remains unsupported (no stable route identifier available).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.core.m365_identifiers import normalize_meeting_id, normalize_site_library, normalize_thread_id
from src.core.rev.entity_types import EntityType, supports_immutable_id, supports_registry_route


@dataclass(frozen=True, slots=True)
class HydrationLocator:
    """Pre-hydration: just enough to fetch the item. No route fields yet.

    This is the enumerator output and the cache key for the hydrated body
    (§5.6). ``resource_id`` is a native Graph resource id — from collection
    ``$search`` directly, or resolved from a ``SearchHitLocator`` (§5.3).
    """

    source_type: EntityType
    tenant_id: str
    principal_mailbox: str          # signed-in user / mailbox the item lives in
    container: str                  # folder / chat / channel / site+drive+list
    resource_id: str                # native Graph resource id
    etag_hint: str | None = None


@dataclass(frozen=True, slots=True)
class CanonicalItemIdentity:
    """Post-hydration: stable item identity for dedup + evidence provenance."""

    source_type: EntityType
    tenant_id: str
    principal_mailbox: str
    container: str
    resource_id: str
    immutable_id: str | None = None     # mail/event only; established at hydration GET
    etag: str | None = None

    @property
    def cache_key(self) -> str:
        """Stable key for the extraction/hydration cache (§5.6).

        ``immutable_id`` is preferred when available (mail/event) because it
        survives folder moves; otherwise the native ``resource_id``.
        """
        ident = self.immutable_id or self.resource_id
        return f"{self.source_type.value}|{self.tenant_id}|{self.principal_mailbox}|{self.container}|{ident}"


@dataclass(frozen=True, slots=True)
class RouteMetadata:
    """Route-identifying fields obtained *during* hydration (§5.4).

    The binder cannot run on ``HydrationLocator`` alone — it needs these
    hydration-derived fields. Only one of ``conversation_id`` / ``series_master_id``
    / ``chat_id`` / ``site_library`` is meaningful per source type.
    """

    conversation_id: str | None = None       # mail / Teams thread
    series_master_id: str | None = None      # calendar series (NOT iCalUId)
    chat_id: str | None = None               # Teams chat
    team_id: str | None = None               # Teams channel
    channel_id: str | None = None            # Teams channel
    site_library: str | None = None          # SharePoint site+library path (DRIVE_ITEM §13.5 route key)
    extra: tuple[tuple[str, str], ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class SourceRouteIdentity:
    """Post-route-metadata: the recurring route for §13.5 registration."""

    source_type: EntityType
    tenant_id: str
    principal_mailbox: str
    container: str
    route_key: str        # conversationId | chatId/team+channel | seriesMasterId | site+library
    normalized_id: str    # normalize_thread_id / normalize_meeting_id output

    @property
    def is_registry_eligible(self) -> bool:
        """True only for entity types §13.5 supports (mail/Teams/calendar)."""
        return supports_registry_route(self.source_type)


class IdentityResolutionError(ValueError):
    """Raised when a route cannot be resolved for the given entity type."""


class ItemToRouteBinder:
    """Maps a ``CanonicalItemIdentity`` + hydration ``RouteMetadata`` to a
    ``SourceRouteIdentity`` (§5.4).

    Pure and deterministic. Supported source types: MESSAGE, CHAT_MESSAGE,
    EVENT, DRIVE_ITEM. LIST_ITEM and any future unsupported types raise
    ``IdentityResolutionError``.
    """

    def bind(self, item: CanonicalItemIdentity, route_metadata: RouteMetadata) -> SourceRouteIdentity:
        if not supports_registry_route(item.source_type):
            raise IdentityResolutionError(
                f"§13.5 has no route type for {item.source_type.value}; "
                "unsupported entity type (see specs/gaps.md §5.4)."
            )
        route_key, normalized = self._route_for(item.source_type, route_metadata)
        if not route_key or not normalized:
            raise IdentityResolutionError(
                f"missing route-identifying field for {item.source_type.value} "
                f"(got route_key={route_key!r}, normalized_id={normalized!r})"
            )
        return SourceRouteIdentity(
            source_type=item.source_type,
            tenant_id=item.tenant_id,
            principal_mailbox=item.principal_mailbox,
            container=item.container,
            route_key=route_key,
            normalized_id=normalized,
        )

    @staticmethod
    def _route_for(source_type: EntityType, route_metadata: RouteMetadata) -> tuple[str, str]:
        if source_type is EntityType.MESSAGE:
            raw = route_metadata.conversation_id or ""
            return raw, normalize_thread_id(raw) or ""
        if source_type is EntityType.CHAT_MESSAGE:
            if route_metadata.team_id and route_metadata.channel_id:
                raw = f"{route_metadata.team_id}/{route_metadata.channel_id}"
            elif route_metadata.chat_id:
                raw = route_metadata.chat_id
            elif route_metadata.conversation_id:
                raw = route_metadata.conversation_id
            else:
                return "", ""
            return raw, normalize_thread_id(raw) or ""
        if source_type is EntityType.EVENT:
            raw = route_metadata.series_master_id or ""
            return raw, normalize_meeting_id(raw) or ""
        if source_type is EntityType.DRIVE_ITEM:
            raw = route_metadata.site_library or ""
            normalized = normalize_site_library(raw) or ""
            return raw, normalized
        return "", ""


def hydration_locator_from_record(record: dict[str, Any]) -> HydrationLocator:
    """Reconstruct a ``HydrationLocator`` from a serialized record."""
    return HydrationLocator(
        source_type=EntityType(str(record["source_type"])),
        tenant_id=str(record["tenant_id"]),
        principal_mailbox=str(record["principal_mailbox"]),
        container=str(record["container"]),
        resource_id=str(record["resource_id"]),
        etag_hint=str(record["etag_hint"]) if isinstance(record.get("etag_hint"), str) else None,
    )


def hydration_locator_to_record(locator: HydrationLocator) -> dict[str, Any]:
    return {
        "source_type": locator.source_type.value,
        "tenant_id": locator.tenant_id,
        "principal_mailbox": locator.principal_mailbox,
        "container": locator.container,
        "resource_id": locator.resource_id,
        "etag_hint": locator.etag_hint,
    }


def canonical_item_identity_to_record(identity: CanonicalItemIdentity) -> dict[str, Any]:
    return {
        "source_type": identity.source_type.value,
        "tenant_id": identity.tenant_id,
        "principal_mailbox": identity.principal_mailbox,
        "container": identity.container,
        "resource_id": identity.resource_id,
        "immutable_id": identity.immutable_id,
        "etag": identity.etag,
    }


def canonical_item_identity_from_record(record: dict[str, Any]) -> CanonicalItemIdentity:
    return CanonicalItemIdentity(
        source_type=EntityType(str(record["source_type"])),
        tenant_id=str(record["tenant_id"]),
        principal_mailbox=str(record["principal_mailbox"]),
        container=str(record["container"]),
        resource_id=str(record["resource_id"]),
        immutable_id=str(record["immutable_id"]) if isinstance(record.get("immutable_id"), str) else None,
        etag=str(record["etag"]) if isinstance(record.get("etag"), str) else None,
    )


def supports_immutable_id_for(source_type: EntityType) -> bool:
    return supports_immutable_id(source_type)