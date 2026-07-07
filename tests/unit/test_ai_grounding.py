from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.ai.grounding import GroundingError, ground_text
from src.core.models import Comment, RiskLevel, WorkItem


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


def test_ground_text_preserves_valid_citations() -> None:
    result = ground_text(
        "Deployment velocity improved [#101].",
        (_item(101, "Deployment velocity improved"),),
    )

    assert result.grounded_text == "Deployment velocity improved [#101]."
    assert result.removed_claims == ()
    assert result.cited_work_item_ids == (101,)
    assert result.grounded_sentences[0].match_type == "explicit"


def test_ground_text_rejects_invalid_citations() -> None:
    with pytest.raises(GroundingError, match="999"):
        ground_text(
            "Deployment velocity improved [#999].",
            (_item(101, "Deployment velocity improved"),),
        )


def test_ground_text_appends_citation_when_title_matches() -> None:
    result = ground_text(
        "Cache warmup safeguard is ready.",
        (_item(101, "Cache warmup safeguard"),),
    )

    assert result.grounded_text == "Cache warmup safeguard is ready [#101]."
    assert result.removed_claims == ()
    assert result.cited_work_item_ids == (101,)
    assert result.grounded_sentences[0].match_type == "heuristic"


def test_ground_text_appends_citation_when_comment_matches() -> None:
    result = ground_text(
        "Rollout blocked on XSSE dependency.",
        (_item(202, "Deployment readiness", comment_text="Rollout blocked on XSSE dependency"),),
    )

    assert result.grounded_text == "Rollout blocked on XSSE dependency [#202]."
    assert result.removed_claims == ()
    assert result.cited_work_item_ids == (202,)


def test_ground_text_drops_unmatched_and_ambiguous_claims() -> None:
    unmatched = ground_text(
        "Completely new unsupported claim.",
        (_item(101, "Cache warmup safeguard"),),
    )
    ambiguous = ground_text(
        "Deployment readiness improved.",
        (
            _item(101, "Deployment readiness"),
            _item(102, "Deployment readiness"),
        ),
    )

    assert unmatched.grounded_text == ""
    assert unmatched.removed_claims == ("Completely new unsupported claim.",)
    assert unmatched.cited_work_item_ids == ()
    assert unmatched.grounded_sentences == ()
    assert ambiguous.grounded_text == ""
    assert ambiguous.removed_claims == ("Deployment readiness improved.",)
    assert ambiguous.cited_work_item_ids == ()
    assert ambiguous.grounded_sentences == ()