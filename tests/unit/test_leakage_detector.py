from __future__ import annotations

from datetime import date, datetime, timezone

from src.core.models import Comment
from src.core.leakage_detector import detect_leakage
from src.core.models import Confidence, RiskLevel, WorkItem
from src.core.models_v2 import Signal, TrajectoryPoint


def test_detect_leakage_flags_workiq_signals_without_post_signal_ado_update() -> None:
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    pre_signal_update = datetime(2026, 5, 7, 9, 0, tzinfo=timezone.utc)
    items = (
        _item(1001, area_path="One\\Adventure\\Acme", changed_at=pre_signal_update, owner="operator@example.com"),
        _item(1002, area_path="One\\Adventure\\Acme", changed_at=pre_signal_update, owner="operator@example.com"),
    )
    signals = (
        Signal(
            id="sig-1",
            timestamp=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
            source="workiq/email",
            program_id="acme",
            workstream_id=None,
            entity_refs=("WI:1001",),
            text="WorkIQ thread for WI:1001",
            raw_ref="workiq:email:1",
            confidence=Confidence.MEDIUM,
            metadata={"entity_link_confidence": "high"},
        ),
        Signal(
            id="sig-2",
            timestamp=datetime(2026, 5, 8, 13, 0, tzinfo=timezone.utc),
            source="workiq/email",
            program_id="acme",
            workstream_id=None,
            entity_refs=("WI:1002",),
            text="WorkIQ thread for WI:1002",
            raw_ref="workiq:email:2",
            confidence=Confidence.MEDIUM,
            metadata={"entity_link_confidence": "high"},
        ),
    )

    report = detect_leakage(
        items,
        signals,
        trajectory_loader=lambda work_item_id: {
            1001: (
                TrajectoryPoint(
                    date=date(2026, 5, 7),
                    state="Active",
                    assigned_to="Vertex Maintainer",
                    target_date=date(2026, 5, 20),
                    risk_level=RiskLevel.MEDIUM,
                    area_path="One\\Adventure\\Acme",
                ),
            ),
            1002: (),
        }[work_item_id],
    )

    assert report.signal_counts_by_item == {1001: 1, 1002: 1}
    assert report.leakage_counts_by_item == {1001: 1, 1002: 1}
    assert len(report.events) == 2
    assert tuple(event.work_item_id for event in report.events) == (1001, 1002)
    assert report.owner_leakage_ratios == {"operator": 1.0}


def test_detect_leakage_cancels_when_ado_update_happens_after_signal_within_window() -> None:
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    items = (
        _item(1001, area_path="One\\Adventure\\Acme", changed_at=datetime(2026, 5, 9, 9, 0, tzinfo=timezone.utc), owner="operator@example.com"),
    )
    signals = (
        Signal(
            id="sig-1",
            timestamp=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
            source="workiq/email",
            program_id="acme",
            workstream_id=None,
            entity_refs=("WI:1001",),
            text="WorkIQ thread for WI:1001",
            raw_ref="workiq:email:1",
            confidence=Confidence.MEDIUM,
            metadata={"entity_link_confidence": "high"},
        ),
    )

    report = detect_leakage(
        items,
        signals,
        trajectory_loader=lambda work_item_id: (),
    )

    assert report.signal_counts_by_item == {1001: 1}
    assert report.leakage_counts_by_item == {}
    assert report.events == ()
    assert report.owner_leakage_ratios == {"operator": 0.0}


def test_detect_leakage_ignores_low_confidence_entity_links() -> None:
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    items = (
        _item(1002, area_path="One\\Adventure\\Acme", changed_at=as_of, owner="operator@example.com"),
    )
    signals = (
        Signal(
            id="sig-low",
            timestamp=datetime(2026, 5, 8, 13, 0, tzinfo=timezone.utc),
            source="workiq/email",
            program_id="acme",
            workstream_id=None,
            entity_refs=("WI:1002",),
            text="Low-confidence WorkIQ thread for WI:1002",
            raw_ref="workiq:email:2",
            confidence=Confidence.MEDIUM,
            metadata={"entity_link_confidence": "low"},
        ),
    )

    report = detect_leakage(
        items,
        signals,
        trajectory_loader=lambda work_item_id: (),
    )

    assert report.signal_counts_by_item == {}
    assert report.leakage_counts_by_item == {}
    assert report.events == ()
    assert report.owner_leakage_ratios == {}


def test_detect_leakage_cancels_when_owner_comment_happens_after_signal() -> None:
    item = _item(
        1003,
        area_path="One\\Adventure\\Acme",
        changed_at=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
        owner="operator@example.com",
    )
    item.comments.append(
        Comment(
            work_item_id=1003,
            comment_id=1,
            created_by="Vertex Maintainer",
            created_by_email="operator@example.com",
            created_date=datetime(2026, 5, 9, 9, 0, tzinfo=timezone.utc),
            text="Updated status and next step in ADO.",
        )
    )
    signals = (
        Signal(
            id="sig-3",
            timestamp=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
            source="workiq/email",
            program_id="acme",
            workstream_id=None,
            entity_refs=("WI:1003",),
            text="WorkIQ thread for WI:1003",
            raw_ref="workiq:email:3",
            confidence=Confidence.MEDIUM,
            metadata={"entity_link_confidence": "high"},
        ),
    )

    report = detect_leakage(
        (item,),
        signals,
        trajectory_loader=lambda work_item_id: (),
    )

    assert report.leakage_counts_by_item == {}
    assert report.events == ()


def _item(work_item_id: int, *, area_path: str, changed_at: datetime, owner: str) -> WorkItem:
    return WorkItem(
        id=work_item_id,
        type="Feature",
        title=f"Work item {work_item_id}",
        state="Active",
        assigned_to=owner,
        assigned_to_email=owner,
        area_path=area_path,
        iteration_path="Sprint 1",
        target_date=(changed_at.date()),
        risk_level=RiskLevel.MEDIUM,
        tags=[],
        custom_fields={"changed_date": changed_at.isoformat(), "description": "Detailed enough to count as rich item description."},
        revisions=[],
        comments=[],
        fetched_at=changed_at,
    )