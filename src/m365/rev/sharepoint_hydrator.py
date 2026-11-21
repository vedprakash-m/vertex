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
from dataclasses import dataclass, replace
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
    version: str = ""
    acl: tuple[Any, ...] = ()
    deleted: bool = False
    inaccessible: bool = False


@dataclass(frozen=True, slots=True)
class GraphDriveItemContent:
    """One bounded byte retrieval tied to the metadata version."""

    data: bytes
    offset: int = 0
    total_size: int = 0
    etag: str = ""
    version: str = ""
    complete: bool = True


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

    def get_drive_item_content(
        self,
        *,
        drive_id: str,
        item_id: str,
        offset: int,
        max_bytes: int,
        etag: str = "",
        correlation_id: str = "",
    ) -> PortResult[GraphDriveItemContent]:
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
        return Success(replace(item, file_content=b""))

    def get_drive_item_content(
        self,
        *,
        drive_id: str,
        item_id: str,
        offset: int,
        max_bytes: int,
        etag: str = "",
        correlation_id: str = "",
    ) -> PortResult[GraphDriveItemContent]:
        self.calls.append(
            (
                "get_drive_item_content",
                {
                    "drive_id": drive_id,
                    "item_id": item_id,
                    "offset": offset,
                    "max_bytes": max_bytes,
                    "etag": etag,
                },
            )
        )
        if item_id in self._rate_limited_ids:
            return RateLimited(provider="graph_sharepoint", retry_after_seconds=1.0)
        if item_id in self._forbidden_ids:
            return Forbidden(scope="files", reason="forbidden_by_fake")
        item = self._items.get(item_id)
        if item is None:
            return Incomplete(
                GraphDriveItemContent(data=b"", offset=offset),
                reason=f"item_not_found:{item_id}",
            )
        data = item.file_content[offset : offset + max_bytes]
        total_size = item.size or len(item.file_content)
        return Success(
            GraphDriveItemContent(
                data=data,
                offset=offset,
                total_size=total_size,
                etag=item.etag,
                version=item.version,
                complete=offset + len(data) >= total_size,
            )
        )

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
        if candidate.partial_metadata.get("deleted"):
            return self._rejected_metadata(
                candidate,
                correlation_id,
                reason=f"deleted:{candidate.locator.resource_id}",
            )
        if candidate.partial_metadata.get("inaccessible"):
            return Forbidden(scope="files", reason=f"inaccessible:{candidate.locator.resource_id}")

        item_id = candidate.locator.resource_id
        drive_id = self._context.drive_id or str(candidate.partial_metadata.get("drive_id") or candidate.locator.container)
        result = self._client.get_drive_item(drive_id=drive_id, item_id=item_id, correlation_id=correlation_id)
        if isinstance(result, (Forbidden, RateLimited, Unsupported)):
            return result
        if isinstance(result, Incomplete):
            if result.reason.startswith(("item_not_found", "deleted", "inaccessible")):
                return self._rejected_metadata(candidate, correlation_id, reason=result.reason)
            return self._metadata_only_fallback(candidate, correlation_id, reason=result.reason)
        if not isinstance(result, Success) or not isinstance(result.value, GraphDriveItem):
            return self._metadata_only_fallback(
                candidate,
                correlation_id,
                reason="malformed_metadata_result",
            )
        item: GraphDriveItem = result.value
        if item.deleted:
            return self._rejected_metadata(candidate, correlation_id, reason=f"deleted:{item.item_id}")
        if item.inaccessible:
            return Forbidden(scope="files", reason=f"inaccessible:{item.item_id}")
        candidate_etag = candidate.locator.etag_hint or str(candidate.partial_metadata.get("etag") or "")
        if candidate_etag and item.etag and candidate_etag != item.etag:
            return self._rejected_metadata(
                candidate,
                correlation_id,
                reason=f"etag_changed:{candidate_etag}->{item.etag}",
                item=item,
            )

        # Only extract text-type files; binary files surface as metadata_only.
        if item.mime_type not in _PLAIN_TEXT_TYPES:
            if item.mime_type:
                log.info(
                    "rev.sharepoint.hydrate unsupported_mime_type item=%s mime=%s",
                    item_id, item.mime_type,
                )
            return self._metadata_only_fallback(
                candidate,
                correlation_id,
                reason="unsupported_or_binary_file",
                item=item,
            )

        content_result = self._retrieve_content(
            item,
            drive_id=drive_id,
            correlation_id=correlation_id,
        )
        if isinstance(content_result, (Forbidden, RateLimited, Unsupported)):
            return content_result
        if isinstance(content_result, Incomplete):
            return self._metadata_only_fallback(
                candidate,
                correlation_id,
                reason=content_result.reason,
                item=item,
            )
        if not isinstance(content_result, Success) or not isinstance(
            content_result.value, GraphDriveItemContent
        ):
            return self._metadata_only_fallback(
                candidate,
                correlation_id,
                reason="malformed_content_result",
                item=item,
            )
        content = content_result.value
        if content.etag and item.etag and content.etag != item.etag:
            return self._rejected_metadata(
                candidate,
                correlation_id,
                reason=f"content_etag_mismatch:{item.etag}->{content.etag}",
                item=item,
                content=content,
            )
        if content.version and item.version and content.version != item.version:
            return self._rejected_metadata(
                candidate,
                correlation_id,
                reason=f"content_version_mismatch:{item.version}->{content.version}",
                item=item,
                content=content,
            )
        if not content.data:
            return self._metadata_only_fallback(
                candidate,
                correlation_id,
                reason="content_unavailable",
                item=item,
                content=content,
            )
        if not content.complete:
            return self._metadata_only_fallback(
                candidate,
                correlation_id,
                reason=f"item_over_ceiling:{self._context.max_body_bytes}",
                item=item,
                content=content,
            )

        try:
            text = content.data.decode("utf-8", errors="replace")
        except Exception as exc:
            log.warning("rev.sharepoint.hydrate decode_error item=%s: %s", item_id, exc)
            return self._metadata_only_fallback(
                candidate,
                correlation_id,
                reason="decode_error",
                item=item,
                content=content,
            )

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
            return self._metadata_only_fallback(
                candidate,
                correlation_id,
                reason=local.reason or "local_check_failed",
                item=item,
                content=content,
            )

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
        route_meta = self._route_metadata(candidate, item, content=content)
        route_meta["rung"] = RUNG_FILE_CONTENT
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

    def _retrieve_content(
        self,
        item: GraphDriveItem,
        *,
        drive_id: str,
        correlation_id: str,
    ) -> PortResult[GraphDriveItemContent]:
        getter = getattr(self._client, "get_drive_item_content", None)
        if getter is not None:
            return getter(
                drive_id=drive_id,
                item_id=item.item_id,
                offset=0,
                max_bytes=self._context.max_body_bytes,
                etag=item.etag,
                correlation_id=correlation_id,
            )
        data = item.file_content[: self._context.max_body_bytes]
        total_size = item.size or len(item.file_content)
        return Success(
            GraphDriveItemContent(
                data=data,
                offset=0,
                total_size=total_size,
                etag=item.etag,
                version=item.version,
                complete=len(data) >= total_size,
            )
        )

    def _metadata_only_fallback(
        self,
        candidate: EnumeratedCandidate,
        correlation_id: str,
        *,
        reason: str,
        item: GraphDriveItem | None = None,
        content: GraphDriveItemContent | None = None,
    ) -> PortResult[HydratedContent]:
        log.warning("rev.sharepoint.hydrate metadata_only cid=%s reason=%s", correlation_id, reason)
        return Success(
            self._metadata_only_content(
                candidate,
                correlation_id,
                reason=reason,
                item=item,
                content=content,
            )
        )

    def _rejected_metadata(
        self,
        candidate: EnumeratedCandidate,
        correlation_id: str,
        *,
        reason: str,
        item: GraphDriveItem | None = None,
        content: GraphDriveItemContent | None = None,
    ) -> Incomplete[HydratedContent]:
        return Incomplete(
            self._metadata_only_content(
                candidate,
                correlation_id,
                reason=reason,
                item=item,
                content=content,
            ),
            reason=reason,
        )

    def _metadata_only_content(
        self,
        candidate: EnumeratedCandidate,
        correlation_id: str,
        *,
        reason: str,
        item: GraphDriveItem | None = None,
        content: GraphDriveItemContent | None = None,
    ) -> HydratedContent:
        drive_id = (
            item.drive_id
            if item is not None
            else str(candidate.partial_metadata.get("drive_id") or candidate.locator.container)
        )
        identity = CanonicalItemIdentity(
            source_type=self.entity_type,
            tenant_id=self._context.tenant_id,
            principal_mailbox=self._context.principal_mailbox,
            container=drive_id,
            resource_id=candidate.locator.resource_id,
            etag=item.etag if item is not None else candidate.locator.etag_hint,
        )
        name = item.name if item is not None else str(candidate.partial_metadata.get("name", ""))
        norm = normalize(name, is_html=False, source_type=self.entity_type)
        route_metadata = self._route_metadata(candidate, item, content=content)
        route_metadata["fallback_reason"] = reason
        return HydratedContent(
            identity=identity,
            canonical_text=norm.canonical_text,
            normalized_source_hash=norm.normalized_source_hash,
            chunks=norm.chunks,
            route_metadata=route_metadata,
            hydration_rung=RUNG_METADATA_ONLY,
            metadata_only=True,
            retrieved_at=None,
            correlation_id=correlation_id,
        )

    def _route_metadata(
        self,
        candidate: EnumeratedCandidate,
        item: GraphDriveItem | None,
        *,
        content: GraphDriveItemContent | None = None,
    ) -> dict[str, Any]:
        partial = candidate.partial_metadata
        acl = item.acl if item is not None else tuple(partial.get("acl") or ())
        return {
            "name": item.name if item is not None else partial.get("name", ""),
            "web_url": item.web_url if item is not None else partial.get("web_url", ""),
            "site_id": item.site_id if item is not None else partial.get("site_id", ""),
            "page_id": partial.get("page_id"),
            "page_url": partial.get("url"),
            "parent_path": item.parent_path if item is not None else partial.get("parent_path", ""),
            "mime_type": item.mime_type if item is not None else partial.get("mime_type", ""),
            "etag": item.etag if item is not None else partial.get("etag", candidate.locator.etag_hint or ""),
            "version": item.version if item is not None else partial.get("version", ""),
            "last_modified": item.last_modified if item is not None else partial.get("last_modified", ""),
            "deleted": item.deleted if item is not None else bool(partial.get("deleted")),
            "inaccessible": item.inaccessible if item is not None else bool(partial.get("inaccessible")),
            "acl": acl,
            "acl_authorization_claimed": False,
            "sharepoint_no_registry_route": True,
            "content_offset": content.offset if content is not None else 0,
            "content_length": len(content.data) if content is not None else 0,
            "content_size": content.total_size if content is not None else (item.size if item is not None else 0),
            "content_complete": content.complete if content is not None else False,
            "content_etag": content.etag if content is not None else "",
            "content_version": content.version if content is not None else "",
        }


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
    "GraphDriveItemContent",
    "RevSharePointClient",
    "FakeRevSharePointClient",
    "SharePointContext",
    "SharePointHydrator",
    "LiveSharePointHydrator",
    "RUNG_FILE_CONTENT",
    "RUNG_METADATA_ONLY",
]
