"""SharePointEnumerator — CandidateEnumerator over configured engms_pages.yaml pages (RELO-W3.7).

Zone C adapter. Enumerates ``DRIVE_ITEM`` candidates from the configured
``EngMsPage`` list by resolving each page URL to a Graph driveItem through an
injected ``resolve`` callable; no network client is constructed here.

- Deterministic: pages are processed in ``page.id`` order; duplicate driveItems
  keep the first page that resolved them.
- Bounded: ``max_pages`` caps resolver calls (``budget_stop:max_pages``) and the
  smaller of ``max_candidates`` / ``intent.limit`` caps output (``page_cut``).
- Typed failures: no configured pages is ``Unsupported``; when every page fails,
  the first typed failure (or ``Incomplete`` for resolver exceptions) is returned
  instead of an empty ``Success``.

Not wired into the gather stage (``sharepoint_ingest_stage`` stays default-off,
OD-13). A live Graph resolver is operator-gated (RV-S1-SHAREPOINT).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from src.core.models_v2 import EngMsPage
from src.core.rev.entity_types import EntityType
from src.core.rev.identity import HydrationLocator
from src.core.rev.ports import EnumeratedCandidate
from src.core.rev.query_planner import RetrievalIntent
from src.core.rev.result import (
    Forbidden,
    Incomplete,
    PortResult,
    RateLimited,
    Success,
    Unsupported,
)
from src.m365.rev.sharepoint_hydrator import GraphDriveItem

__all__ = ["PageResolver", "SharePointEnumerator"]

log = logging.getLogger(__name__)

PageResolver = Callable[[EngMsPage, str], PortResult[GraphDriveItem]]

_ENUMERATOR_NAME = "engms_pages"


class SharePointEnumerator:
    """CandidateEnumerator for SharePoint driveItems named in engms_pages.yaml."""

    entity_type = EntityType.DRIVE_ITEM

    def __init__(
        self,
        pages: Sequence[EngMsPage],
        resolve: PageResolver,
        *,
        tenant_id: str,
        principal_mailbox: str,
        max_pages: int = 50,
        max_candidates: int = 100,
    ) -> None:
        if max_pages < 1 or max_candidates < 1:
            raise ValueError("max_pages and max_candidates must be >= 1")
        self._pages = tuple(sorted(pages, key=lambda p: p.id))
        self._resolve = resolve
        self._tenant_id = tenant_id
        self._principal_mailbox = principal_mailbox
        self._max_pages = max_pages
        self._max_candidates = max_candidates

    def enumerate(
        self,
        intent: RetrievalIntent,
        *,
        correlation_id: str,
    ) -> PortResult[tuple[EnumeratedCandidate, ...]]:
        if intent.entity_type is not EntityType.DRIVE_ITEM:
            return Unsupported(entity_type=intent.entity_type, reason="sharepoint_enumerator_drive_item_only")
        if not self._pages:
            return Unsupported(entity_type=EntityType.DRIVE_ITEM, reason="no_sharepoint_pages_configured")

        candidates: list[EnumeratedCandidate] = []
        seen: set[str] = set()
        failures: list[tuple[str, object]] = []
        for page in self._pages[: self._max_pages]:
            try:
                outcome = self._resolve(page, correlation_id)
            except Exception as exc:
                log.warning("rev.sharepoint.enumerate resolve_error page=%s: %s", page.id, type(exc).__name__)
                failures.append((page.id, exc))
                continue
            if not isinstance(outcome, Success):
                failures.append((page.id, outcome))
                continue
            item = outcome.value
            if item.item_id in seen:
                continue
            seen.add(item.item_id)
            candidates.append(self._candidate(page, item, correlation_id))

        if failures and not candidates:
            for _, failure in failures:
                if isinstance(failure, (Forbidden, RateLimited, Unsupported)):
                    return failure
        limit = min(self._max_candidates, intent.limit)
        kept = tuple(candidates[:limit])
        if failures:
            detail = ",".join(f"{page_id}={type(f).__name__}" for page_id, f in failures)
            return Incomplete(value=kept, reason=f"page_failures:{detail}")
        if len(self._pages) > self._max_pages:
            return Incomplete(value=kept, reason="budget_stop:max_pages")
        if len(candidates) > limit:
            return Incomplete(value=kept, reason="page_cut")
        return Success(kept)

    def _candidate(self, page: EngMsPage, item: GraphDriveItem, correlation_id: str) -> EnumeratedCandidate:
        return EnumeratedCandidate(
            locator=HydrationLocator(
                source_type=EntityType.DRIVE_ITEM,
                tenant_id=self._tenant_id,
                principal_mailbox=self._principal_mailbox,
                container=item.drive_id,
                resource_id=item.item_id,
                etag_hint=item.etag or None,
            ),
            relevance_score=1.0,
            partial_metadata={
                "page_id": page.id,
                "title": page.title,
                "url": page.url,
                "drive_id": item.drive_id,
                "site_id": item.site_id,
                "name": item.name,
                "source_subtype": page.source_subtype,
            },
            correlation_id=correlation_id,
            enumerator=_ENUMERATOR_NAME,
        )
