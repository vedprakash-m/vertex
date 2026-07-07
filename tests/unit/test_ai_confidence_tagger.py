from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.ai.grounding import ground_text
from src.ai.safety.confidence_tagger import ConfidenceTaggingError, tag_grounded_text
from src.core.models import Comment, Confidence, RiskLevel, WorkItem


def _item(work_item_id: int, title: str, *, comment_text: str | None = None) -> WorkItem:
    comments = []
    if comment_text is not None:
        comments.append(
            Comment(
                work_item_id=work_item_id,
                comment_id=1,
                created_by="Operator",
                created_by_email="operator@example.com",
                created_date=datetime(2026, 5, 6, 10, 0, tzinfo=timezone.utc),
                text=comment_text,
            )
        )
    return WorkItem(
        id=work_item_id,
        type="Feature",
        title=title,
        state="Active",
        assigned_to="Operator",
        assigned_to_email="operator@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="FY26\\Sprint 20",
        target_date=date(2026, 6, 1),
        risk_level=RiskLevel.MEDIUM,
        tags=[],
        custom_fields={},
        comments=comments,
    )


def test_tag_grounded_text_keeps_explicit_high_confidence() -> None:
    grounded = ground_text("Cache warmup safeguard is ready [#101].", (_item(101, "Cache warmup safeguard"),))

    result = tag_grounded_text(grounded, {101: Confidence.HIGH})

    assert result.tagged_sentences[0].confidence == Confidence.HIGH
    assert result.overall_confidence == Confidence.HIGH


def test_tag_grounded_text_downgrades_heuristic_high_confidence_to_medium() -> None:
    grounded = ground_text("Cache warmup safeguard is ready.", (_item(101, "Cache warmup safeguard"),))

    result = tag_grounded_text(grounded, {101: Confidence.HIGH})

    assert result.tagged_sentences[0].match_type == "heuristic"
    assert result.tagged_sentences[0].confidence == Confidence.MEDIUM
    assert result.overall_confidence == Confidence.MEDIUM


def test_tag_grounded_text_preserves_low_confidence() -> None:
    grounded = ground_text("Cache warmup safeguard is ready [#101].", (_item(101, "Cache warmup safeguard"),))

    result = tag_grounded_text(grounded, {101: Confidence.LOW})

    assert result.tagged_sentences[0].confidence == Confidence.LOW


def test_tag_grounded_text_uses_lowest_cited_confidence() -> None:
    grounded = ground_text(
        "Cache warmup safeguard is ready [#101] and rollout blocked [#202].",
        (
            _item(101, "Cache warmup safeguard"),
            _item(202, "Rollout blocked"),
        ),
    )

    result = tag_grounded_text(
        grounded,
        {
            101: Confidence.HIGH,
            202: Confidence.MEDIUM,
        },
    )

    assert result.tagged_sentences[0].confidence == Confidence.MEDIUM
    assert result.overall_confidence == Confidence.MEDIUM


def test_tag_grounded_text_rejects_missing_confidence_mapping() -> None:
    grounded = ground_text("Cache warmup safeguard is ready [#101].", (_item(101, "Cache warmup safeguard"),))

    with pytest.raises(ConfidenceTaggingError, match="#101"):
        tag_grounded_text(grounded, {})