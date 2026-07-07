from __future__ import annotations

from datetime import date, datetime, timezone

from src.core.evidence_engine import build_evidence
from src.core.models import AttributionTier, Comment, Confidence, Revision, RiskLevel, WorkItem


def test_build_evidence_uses_windowed_revisions_and_comments() -> None:
    item = WorkItem(
        id=101,
        type="Feature",
        title="Deployment readiness",
        state="Active",
        assigned_to="Vertex Maintainer",
        assigned_to_email="operator@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="One\\FY26\\Q4",
        target_date=date(2026, 6, 30),
        risk_level=RiskLevel.MEDIUM,
        tags=["acme"],
        custom_fields={},
        revisions=[
            Revision(
                work_item_id=101,
                rev_number=1,
                changed_by="Alice",
                changed_by_email="alice@example.com",
                changed_date=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
                fields_changed={"State": ("New", "Active")},
            ),
            Revision(
                work_item_id=101,
                rev_number=2,
                changed_by="Alice",
                changed_by_email="alice@example.com",
                changed_date=datetime(2026, 4, 20, 9, 0, tzinfo=timezone.utc),
                fields_changed={"State": ("Proposed", "New")},
            ),
        ],
        comments=[
            Comment(
                work_item_id=101,
                comment_id=1,
                created_by="Bob",
                created_by_email="bob@example.com",
                created_date=datetime(2026, 5, 2, 11, 0, tzinfo=timezone.utc),
                text="Tracked for ramp.",
            ),
        ],
    )

    evidence = build_evidence(
        item=item,
        window_start=datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc),
        window_end=datetime(2026, 5, 3, 0, 0, tzinfo=timezone.utc),
    )

    assert evidence.confidence == Confidence.HIGH
    assert evidence.tier == AttributionTier.TIER1
    assert len(evidence.revisions) == 1
    assert len(evidence.comments) == 1
    assert "Revisions (1):" in evidence.summary_for_reviewer
    assert "Comments (1):" in evidence.summary_for_reviewer


def test_build_evidence_without_activity_returns_none_confidence() -> None:
    item = WorkItem(
        id=102,
        type="Risk",
        title="Capacity dependency",
        state="Active",
        assigned_to=None,
        assigned_to_email=None,
        area_path="One\\Adventure\\Acme",
        iteration_path="One\\FY26\\Q4",
        target_date=None,
        risk_level=RiskLevel.HIGH,
        tags=[],
        custom_fields={},
    )

    evidence = build_evidence(
        item=item,
        window_start=datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc),
        window_end=datetime(2026, 5, 3, 0, 0, tzinfo=timezone.utc),
    )

    assert evidence.confidence == Confidence.NONE
    assert evidence.tier == AttributionTier.TIER3
    assert evidence.summary_for_reviewer == "No evidence in selected window."


def test_build_evidence_uses_changed_date_fallback_when_revision_history_is_unavailable() -> None:
    item = WorkItem(
        id=103,
        type="Feature",
        title="Deployment readiness",
        state="Active",
        assigned_to="Vertex Maintainer",
        assigned_to_email="operator@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="One\\FY26\\Q4",
        target_date=date(2026, 6, 30),
        risk_level=RiskLevel.MEDIUM,
        tags=["acme"],
        custom_fields={"changed_date": "2026-05-02T11:00:00+00:00"},
    )

    evidence = build_evidence(
        item=item,
        window_start=datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc),
        window_end=datetime(2026, 5, 3, 0, 0, tzinfo=timezone.utc),
    )

    assert evidence.confidence == Confidence.LOW
    assert evidence.tier == AttributionTier.TIER3
    assert evidence.summary_for_reviewer == "ADO item changed on 2026-05-02T11:00:00+00:00."

