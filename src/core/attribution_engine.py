from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote

from src.core.models import AttributionTier, EvidencePacket, WorkItem
from src.core.view_models import Citation


def build_inline_citations(
    items: tuple[WorkItem, ...] | list[WorkItem],
    evidence_by_item: dict[int, EvidencePacket],
    ado_base_url: str = "https://dev.azure.com/workitems",
    max_inline: int = 5,
) -> tuple[Citation, ...]:
    return _build_citations(items, evidence_by_item, ado_base_url, AttributionTier.TIER1)[:max_inline]


def build_section_citations(
    items: tuple[WorkItem, ...] | list[WorkItem],
    evidence_by_item: dict[int, EvidencePacket],
    ado_base_url: str = "https://dev.azure.com/workitems",
) -> tuple[Citation, ...]:
    return _build_citations(items, evidence_by_item, ado_base_url, AttributionTier.TIER2)


def build_query_citation(
    *,
    title: str,
    ado_url: str,
    tier: AttributionTier = AttributionTier.TIER2,
    label: str = "ADO query",
) -> Citation:
    return Citation(
        work_item_id=None,
        title=title,
        ado_url=ado_url,
        tier=tier,
        label=label,
    )


def build_reviewer_citations(
    items: tuple[WorkItem, ...] | list[WorkItem],
    evidence_by_item: dict[int, EvidencePacket],
    ado_base_url: str = "https://dev.azure.com/workitems",
) -> tuple[Citation, ...]:
    return _build_citations(items, evidence_by_item, ado_base_url, AttributionTier.TIER3)


def build_delta_link(
    work_item_id: int,
    field_name: str,
    ado_revision_base_url: str = "https://dev.azure.com/workitems",
) -> str:
    return f"{ado_revision_base_url}/{work_item_id}/revisions?field={quote(field_name)}"


def _build_citations(
    items: tuple[WorkItem, ...] | list[WorkItem],
    evidence_by_item: dict[int, EvidencePacket],
    ado_base_url: str,
    tier: AttributionTier,
) -> tuple[Citation, ...]:
    items_by_id = {item.id: item for item in items}
    ordered_ids = sorted(
        evidence_by_item,
        key=lambda work_item_id: _latest_evidence_timestamp(evidence_by_item[work_item_id]),
        reverse=True,
    )
    citations: list[Citation] = []
    for work_item_id in ordered_ids:
        item = items_by_id.get(work_item_id)
        if item is None:
            continue
        citations.append(
            Citation(
                work_item_id=work_item_id,
                title=item.title,
                ado_url=f"{ado_base_url}/{work_item_id}",
                tier=tier,
            )
        )
    return tuple(citations)


def _latest_evidence_timestamp(evidence: EvidencePacket) -> datetime:
    timestamps = [revision.changed_date for revision in evidence.revisions]
    timestamps.extend(comment.created_date for comment in evidence.comments)
    timestamps.extend(enrichment.timestamp for enrichment in evidence.enrichments)
    if not timestamps:
        return datetime.min.replace(tzinfo=timezone.utc)
    return max(timestamps)
