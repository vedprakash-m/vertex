"""REV SharePoint hydrator (Zone C) — FR-PCI-5 / REV-11.

specs/program-context-intelligence.md §5.4/§5.6. Hydrates one enumerated
SharePoint/OneDrive driveItem candidate to canonical text + chunks.

**Hydration path (§5.5)**:
    GET /drives/{driveId}/items/{itemId}/content  (file download)
    GET /drives/{driveId}/items/{itemId}           (metadata including etag)

**SharePoint identity note (§5.4)**:
SharePoint has **no §13.5 registry route type** today. The registry supports
only ``meeting_series`` / ``teams_channel`` / ``email_thread``. SharePoint
candidates proceed through Workflow C (fact discovery via triage), but
``SourceRouteIdentity`` for SharePoint is declared unsupported for §13.5
promotion until the registry gains a SharePoint ``SourceRefKind`` + promotion
branch.

**P2 operator-gated.** Requires ``Files.Read.All`` / ``Sites.Read.All`` consent
(RV-S1-SHAREPOINT). Returns ``Unsupported`` with the spike reference until
consent is granted.

Zone C: imports only ``src.core.*`` + stdlib.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
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

RUNG_FILE_CONTENT = "file_content"
RUNG_METADATA_ONLY = "metadata_only_flagged"

# Supported plain-text MIME types for direct extraction (without a parser).
_PLAIN_TEXT_TYPES = frozenset({"text/plain", "text/html", "text/markdown", "text/csv"})

_RV_S1_SHAREPOINT_GATE = (
    "SharePointHydrator: live SharePoint hydration requires RV-S1-SHAREPOINT operator spike. "
    "Confirm: (1) Files.Read.All consent granted, (2) GET /drives/{id}/items/{id}/content "
    "returns file bytes, (3) etag available for extraction-cache key. "
    "Note: SharePoint has no §13.5 registry route type today (R21). "
    "Run 'vertex rev run --mock-fixture' for P1."
)


# ---------------------------------------------------------------------------
# Graph driveItem data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GraphDriveItem:
    """A hydrated SharePoint/OneDrive drive item (§5.4/§5.5).

    ``file_content`` is the raw bytes of the downloaded file (text-type files
    only; binary files are not extracted and surface as ``metadata_only``).
    ``mime_type`` gates whether the content is decoded as text.
    """

    item_id: str
    drive_id: str
    name: str = ""
    web_url: str = ""
    etag: str = ""
    mime_type: str = ""
    file_content: bytes = b""         # populated only for text-type files
    size: int = 0
    last_modified: str = ""
    site_id: str = ""
    parent_path: str = ""


class RevSharePointClient(Protocol):
    """Mockable Graph surface for the REV SharePoint pipeline."""

    def get_drive_item(
        self,
        *,
        drive_id: str,
        item_id: str,
        correlation_id: str = "",
    ) -> PortResult[GraphDriveItem]:
        ...

    def search_drive_items(
        self,
        *,
        kql: str,
        site_id: str = "",
        limit: int = 25,
        correlation_id: str = "",
    ) -> PortResult[tuple[GraphDriveItem, ...]]:
        ...


class FakeRevSharePointClient:
    """In-memory SharePoint client for tests / ``--mock-fixture``."""

    def __init__(
        self,
        items: tuple[GraphDriveItem, ...] = (),
        *,
        rate_limited_ids: frozenset[str] = frozenset(),
        forbidden_ids: frozenset[str] = frozenset(),
    ) -> None:
        self._items: dict[str, GraphDriveItem] = {i.item_id: i for i in items}
        self._rate_limited_ids = rate_limited_ids
        self._forbidden_ids = forbidden_ids
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_drive_item(
        self,
        *,
        drive_id: str,
        item_id: str,
        correlation_id: str = "",
    ) -> PortResult[GraphDriveItem]:
        self.calls.append(("get_drive_item", {"drive_id": drive_id, "item_id": item_id}))
        if item_id in self._rate_limited_ids:
            return RateLimited(provider="graph_sharepoint", retry_after_seconds=1.0)
        if item_id in self._forbidden_ids:
            return Forbidden(scope="files", reason="forbidden_by_fake")
        item = self._items.get(item_id)
        if item is None:
            return Incomplete(GraphDriveItem(item_id=item_id, drive_id=drive_id), reason=f"item_not_found:{item_id}")
        return Success(item)

    def search_drive_items(
        self,
        *,
        kql: str,
        site_id: str = "",
        limit: int = 25,
        correlation_id: str = "",
    ) -> PortResult[tuple[GraphDriveItem, ...]]:
        self.calls.append(("search_drive_items", {"kql": kql, "site_id": site_id}))
        items = list(self._items.values())[:limit]
        return Success(tuple(items))


# ---------------------------------------------------------------------------
# SharePointHydrator
# ---------------------------------------------------------------------------


@dataclass
class SharePointContext:
    """SharePoint connection context."""

    tenant_id: str
    principal_mailbox: str
    drive_id: str = ""
    site_id: str = ""
    max_body_bytes: int = 1_048_576


class SharePointHydrator:
    """ContentHydrator for SharePoint driveItems (§5.6 fixed-order normalization).

    **P2 operator-gated** for the live Graph path. The fake client makes the
    full value chain testable without consent.

    Note: SharePoint has no §13.5 registry route type today (R21). The
    ``SourceRouteIdentity`` for SharePoint items is declared unsupported for
    registry promotion; hydrated facts still enter Workflow C (triage only).
    """

    entity_type = EntityType.DRIVE_ITEM

    def __init__(
        self,
        client: Any,    # RevSharePointClient | FakeRevSharePointClient
        context: SharePointContext,
    ) -> None:
        self._client = client
        self._context = context

    def hydrate(
        self,
        candidate: EnumeratedCandidate,
        *,
        correlation_id: str,
    ) -> PortResult[HydratedContent]:
        item_id = candidate.locator.resource_id
        drive_id = self._context.drive_id or candidate.partial_metadata.get("drive_id", "")
        result = self._client.get_drive_item(drive_id=drive_id, item_id=item_id, correlation_id=correlation_id)
        if isinstance(result, (Forbidden, RateLimited, Unsupported)):
            return result
        if isinstance(result, Incomplete):
            return self._metadata_only_fallback(candidate, correlation_id, reason=result.reason)
        item: GraphDriveItem = result.value

        # Only extract text-type files; binary files surface as metadata_only.
        if not item.file_content or item.mime_type not in _PLAIN_TEXT_TYPES:
            if item.mime_type and item.mime_type not in _PLAIN_TEXT_TYPES:
                log.info(
                    "rev.sharepoint.hydrate unsupported_mime_type item=%s mime=%s",
                    item_id, item.mime_type,
                )
            return self._metadata_only_fallback(candidate, correlation_id, reason="unsupported_or_binary_file")

        try:
            text = item.file_content.decode("utf-8", errors="replace")
        except Exception as exc:
            log.warning("rev.sharepoint.hydrate decode_error item=%s: %s", item_id, exc)
            return self._metadata_only_fallback(candidate, correlation_id, reason="decode_error")

        # Privacy gate.
        local = run_local_checks(
            text,
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

        is_html = item.mime_type == "text/html"
        norm = normalize(text, is_html=is_html, source_type=self.entity_type)
        identity = CanonicalItemIdentity(
            source_type=self.entity_type,
            tenant_id=self._context.tenant_id,
            principal_mailbox=self._context.principal_mailbox,
            container=drive_id,
            resource_id=item.item_id,
            immutable_id=None,   # SharePoint: no Outlook ImmutableId
            etag=item.etag or None,
        )
        route_meta: dict[str, Any] = {
            "name": item.name,
            "web_url": item.web_url,
            "site_id": item.site_id,
            "parent_path": item.parent_path,
            "mime_type": item.mime_type,
            "rung": RUNG_FILE_CONTENT,
            # §5.4: SharePoint has no §13.5 route type today — noted here for consumers.
            "sharepoint_no_registry_route": True,
        }
        return Success(
            HydratedContent(
                identity=identity,
                canonical_text=norm.canonical_text,
                normalized_source_hash=norm.normalized_source_hash,
                chunks=norm.chunks,
                route_metadata=route_meta,
                hydration_rung=RUNG_FILE_CONTENT,
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
        log.warning("rev.sharepoint.hydrate metadata_only cid=%s reason=%s", correlation_id, reason)
        identity = CanonicalItemIdentity(
            source_type=self.entity_type,
            tenant_id=self._context.tenant_id,
            principal_mailbox=self._context.principal_mailbox,
            container=self._context.drive_id,
            resource_id=candidate.locator.resource_id,
        )
        name = str(candidate.partial_metadata.get("name", ""))
        norm = normalize(name, is_html=False, source_type=self.entity_type)
        return Success(
            HydratedContent(
                identity=identity,
                canonical_text=norm.canonical_text,
                normalized_source_hash=norm.normalized_source_hash,
                chunks=norm.chunks,
                route_metadata={"fallback_reason": reason, "sharepoint_no_registry_route": True},
                hydration_rung=RUNG_METADATA_ONLY,
                metadata_only=True,
                retrieved_at=None,
                correlation_id=correlation_id,
            )
        )


class LiveSharePointHydrator:
    """Operator-gated live SharePointHydrator stub (RV-S1-SHAREPOINT).

    Returns ``Unsupported`` until Files.Read.All consent is granted.
    """

    entity_type = EntityType.DRIVE_ITEM

    def hydrate(
        self,
        candidate: EnumeratedCandidate,
        *,
        correlation_id: str,
    ) -> PortResult[HydratedContent]:
        log.warning(_RV_S1_SHAREPOINT_GATE)
        return Unsupported(
            entity_type=EntityType.DRIVE_ITEM.value,
            reason="rv_s1_sharepoint_gate: live SharePoint hydration requires Files.Read.All consent",
        )


__all__ = [
    "GraphDriveItem",
    "RevSharePointClient",
    "FakeRevSharePointClient",
    "SharePointContext",
    "SharePointHydrator",
    "LiveSharePointHydrator",
    "RUNG_FILE_CONTENT",
    "RUNG_METADATA_ONLY",
]
