from __future__ import annotations

from datetime import date, datetime, timezone

from src.core.attribution_engine import build_delta_link, build_inline_citations, build_reviewer_citations
from src.core.attribution_engine import build_section_citations
from src.core.models import AttributionTier, Comment, Confidence, EvidencePacket, RiskLevel, WorkItem


def _item(work_item_id: int, title: str) -> WorkItem:
    return WorkItem(
        id=work_item_id,
        type="Feature",
        title=title,
        state="Active",
        assigned_to="Operator",
        assigned_to_email="operator@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="One\\FY26\\Q4",
        target_date=date(2026, 6, 30),
        risk_level=RiskLevel.MEDIUM,
        tags=[],
        custom_fields={},
    )


def _evidence(work_item_id: int, timestamp: datetime) -> EvidencePacket:
    return EvidencePacket(
        work_item_id=work_item_id,
        revisions=(),
        comments=(
            Comment(
                work_item_id=work_item_id,
                comment_id=1,
                created_by="Alice",
                created_by_email="alice@example.com",
                created_date=timestamp,
                text="Tracked.",
            ),
        ),
        enrichments=(),
        confidence=Confidence.MEDIUM,
        tier=AttributionTier.TIER1,
        summary_for_reviewer="Tracked.",
    )


def test_attribution_engine_orders_citations_by_latest_evidence() -> None:
    items = (
        _item(1, "Earlier"),
        _item(2, "Later"),
    )
    evidence_by_item = {
        1: _evidence(1, datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)),
        2: _evidence(2, datetime(2026, 5, 3, 9, 0, tzinfo=timezone.utc)),
    }

    inline_citations = build_inline_citations(items, evidence_by_item)
    section_citations = build_section_citations(items, evidence_by_item)
    reviewer_citations = build_reviewer_citations(items, evidence_by_item)

    assert [citation.work_item_id for citation in inline_citations] == [2, 1]
    assert inline_citations[0].tier == AttributionTier.TIER1
    assert section_citations[0].tier == AttributionTier.TIER2
    assert reviewer_citations[0].tier == AttributionTier.TIER3
    assert build_delta_link(2, "State").endswith("/2/revisions?field=State")
