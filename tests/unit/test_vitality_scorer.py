from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.core.leakage_detector import LeakageReport
from src.core.models import Comment, Revision, RiskLevel, WorkItem
from src.core.vitality_scorer import aggregate_vitality, score_vitality, summarize_vitality


def test_score_vitality_excludes_terminal_and_new_items() -> None:
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    scores = score_vitality(
        (
            _item(1, state="Resolved", changed_at=as_of - timedelta(days=1)),
            _item(2, state="New", changed_at=as_of - timedelta(days=3)),
            _item(3, state="Active", changed_at=as_of - timedelta(days=10)),
        ),
        as_of=as_of,
        workstream_resolver=lambda item: "ws_demo",
    )

    assert tuple(score.work_item_id for score in scores) == (3,)


def test_score_vitality_computes_richness_and_aggregates() -> None:
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    leakage = LeakageReport(
        events=(),
        signal_counts_by_item={10: 1, 11: 1},
        leakage_counts_by_item={11: 1},
        owner_leakage_ratios={"operator": 0.5},
    )
    scores = score_vitality(
        (
            _item(
                10,
                changed_at=as_of - timedelta(days=2),
                description="Blocked by dependency owner; ship mitigation update and confirm the ask by 2026-05-20. This description is detailed enough to count.",
                comments=(
                    Comment(
                        work_item_id=10,
                        comment_id=1,
                        created_by="Vertex Maintainer",
                        created_by_email="operator@example.com",
                        created_date=as_of - timedelta(days=1),
                        text="Update owner status and confirm follow up by 2026-05-20.",
                    ),
                ),
            ),
            _item(11, changed_at=as_of - timedelta(days=18), description="short"),
        ),
        as_of=as_of,
        workstream_resolver=lambda item: "ws_demo",
        leakage=leakage,
    )

    assert scores[0].freshness_grade == "green"
    assert scores[0].richness_score == 100
    assert scores[0].workiq_signal_count == 1
    assert scores[0].leakage_events == 0
    assert scores[1].freshness_grade == "red"
    assert "recent_comment" in scores[1].richness_missing
    assert scores[1].leakage_events == 1
    assert scores[1].composite_score < scores[0].composite_score

    owner_aggregates = aggregate_vitality(scores, scope_type="owner")
    summary = summarize_vitality(scores)

    assert owner_aggregates[0].scope_type == "owner"
    assert owner_aggregates[0].total_leakage == 1
    assert owner_aggregates[0].workiq_signal_count == 2
    assert owner_aggregates[0].leakage_ratio == 0.5
    assert summary.updated_this_week == 1
    assert summary.stale_owner_aliases == ("operator",)


def test_score_vitality_uses_pre_workiq_formula_when_signals_are_sparse() -> None:
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    recent_comment = (
        Comment(
            work_item_id=10,
            comment_id=1,
            created_by="Vertex Maintainer",
            created_by_email="operator@example.com",
            created_date=as_of - timedelta(days=1),
            text="Update owner status and confirm follow up by 2026-05-20.",
        ),
    )
    leakage = LeakageReport(
        events=(),
        signal_counts_by_item={10: 1, 11: 1},
        leakage_counts_by_item={11: 1},
        owner_leakage_ratios={"operator": 0.5},
    )

    scores = score_vitality(
        (
            _item(
                10,
                changed_at=as_of - timedelta(days=2),
                description="Blocked by dependency owner; ship mitigation update and confirm the ask by 2026-05-20. This description is detailed enough to count.",
                comments=recent_comment,
            ),
            _item(
                11,
                changed_at=as_of - timedelta(days=2),
                description="Blocked by dependency owner; ship mitigation update and confirm the ask by 2026-05-20. This description is detailed enough to count.",
                comments=recent_comment,
            ),
        ),
        as_of=as_of,
        workstream_resolver=lambda item: "ws_demo",
        leakage=leakage,
    )

    owner_aggregates = aggregate_vitality(scores, scope_type="owner")

    assert scores[0].composite_score == 100
    assert scores[1].composite_score == 100
    assert owner_aggregates[0].composite_score == 100


def test_score_vitality_penalizes_full_leakage_more_than_zero_leakage() -> None:
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    recent_comment = (
        Comment(
            work_item_id=10,
            comment_id=1,
            created_by="Vertex Maintainer",
            created_by_email="operator@example.com",
            created_date=as_of - timedelta(days=1),
            text="Update owner status and confirm follow up by 2026-05-20.",
        ),
    )

    zero_leakage_scores = score_vitality(
        (
            _item(
                10,
                changed_at=as_of - timedelta(days=2),
                description="Blocked by dependency owner; ship mitigation update and confirm the ask by 2026-05-20. This description is detailed enough to count.",
                comments=recent_comment,
            ),
        ),
        as_of=as_of,
        workstream_resolver=lambda item: "ws_demo",
        leakage=LeakageReport(
            events=(),
            signal_counts_by_item={10: 5},
            leakage_counts_by_item={10: 0},
            owner_leakage_ratios={"operator": 0.0},
        ),
    )
    full_leakage_scores = score_vitality(
        (
            _item(
                10,
                changed_at=as_of - timedelta(days=2),
                description="Blocked by dependency owner; ship mitigation update and confirm the ask by 2026-05-20. This description is detailed enough to count.",
                comments=recent_comment,
            ),
        ),
        as_of=as_of,
        workstream_resolver=lambda item: "ws_demo",
        leakage=LeakageReport(
            events=(),
            signal_counts_by_item={10: 5},
            leakage_counts_by_item={10: 5},
            owner_leakage_ratios={"operator": 1.0},
        ),
    )

    assert zero_leakage_scores[0].composite_score > full_leakage_scores[0].composite_score


def test_score_vitality_ignores_vertex_comments_and_changed_date_without_meaningful_update() -> None:
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)

    scores = score_vitality(
        (
            WorkItem(
                id=12,
                type="Feature",
                title="Vertex nudged item",
                state="Active",
                assigned_to="Vertex Maintainer",
                assigned_to_email="operator@example.com",
                area_path="One\\Adventure\\Acme",
                iteration_path="Sprint 1",
                target_date=(as_of + timedelta(days=10)).date(),
                risk_level=RiskLevel.HIGH,
                tags=["Safety"],
                custom_fields={
                    "changed_date": (as_of - timedelta(days=1)).isoformat(),
                    "description": "Blocked by dependency owner and ask by 2026-05-20.",
                },
                revisions=[
                    Revision(
                        work_item_id=12,
                        rev_number=1,
                        changed_by="Vertex Maintainer",
                        changed_by_email="operator@example.com",
                        changed_date=as_of - timedelta(days=18),
                        fields_changed={"System.State": ("Proposed", "Active")},
                    )
                ],
                comments=[
                    Comment(
                        work_item_id=12,
                        comment_id=1,
                        created_by="Vertex Bot",
                        created_by_email="vertex-bot@example.com",
                        created_date=as_of - timedelta(days=1),
                        text="📋 Vertex Vitality Check — WI:12",
                    )
                ],
                fetched_at=as_of,
            ),
        ),
        as_of=as_of,
        workstream_resolver=lambda item: "ws_demo",
    )

    assert scores[0].freshness_grade == "red"
    assert scores[0].freshness_days == 18
    assert "recent_comment" in scores[0].richness_missing


def _item(
    work_item_id: int,
    *,
    state: str = "Active",
    changed_at: datetime,
    description: str = "Blocked by dependency owner and ask by 2026-05-20.",
    comments: tuple[Comment, ...] = (),
) -> WorkItem:
    return WorkItem(
        id=work_item_id,
        type="Feature",
        title=f"Work item {work_item_id}",
        state=state,
        assigned_to="Vertex Maintainer",
        assigned_to_email="operator@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="Sprint 1",
        target_date=(changed_at + timedelta(days=10)).date(),
        risk_level=RiskLevel.HIGH,
        tags=["Safety"],
        custom_fields={"changed_date": changed_at.isoformat(), "description": description},
        revisions=(
            [
                Revision(
                    work_item_id=work_item_id,
                    rev_number=1,
                    changed_by="Vertex Maintainer",
                    changed_by_email="operator@example.com",
                    changed_date=changed_at,
                    fields_changed={"System.State": ("Proposed", state)},
                )
            ]
        ),
        comments=list(comments),
        fetched_at=changed_at,
    )