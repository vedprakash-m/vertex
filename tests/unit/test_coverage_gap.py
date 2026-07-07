from __future__ import annotations

from datetime import date, datetime, timezone

from src.core.coverage_gap import build_coverage_gaps
from src.core.models import Confidence, RiskLevel, WorkItem
from src.core.models_v2 import Signal


def test_build_coverage_gaps_excludes_items_with_approved_signal_or_narrative_reference() -> None:
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    items = (
        _item(1001, "Covered by signal", changed_date="2026-05-01T00:00:00+00:00"),
        _item(1002, "Covered by narrative", changed_date="2026-05-01T00:00:00+00:00"),
        _item(1003, "Missing everywhere", changed_date="2026-05-01T00:00:00+00:00"),
        _item(1004, "Too new to flag", changed_date="2026-05-08T00:00:00+00:00"),
        _item(1005, "Proposed item", state="Proposed", changed_date="2026-05-01T00:00:00+00:00"),
    )
    approved_signals = (
        Signal(
            id="sig-1",
            timestamp=as_of,
            source="ado/revision",
            program_id="acme",
            workstream_id="demo",
            entity_refs=("WI:1001",),
            text="Covered",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
            thread_id=None,
        ),
    )

    gaps = build_coverage_gaps(
        items,
        approved_signals=approved_signals,
        narratives={"exec_summary.md": "Discussed WI:1002 in the summary."},
        as_of=as_of,
    )

    assert tuple(gap.work_item_id for gap in gaps) == (1003,)
    assert all(gap.confidence is Confidence.HIGH for gap in gaps)


def test_build_coverage_gaps_excludes_items_with_caller_supplied_coverage() -> None:
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    items = (
        _item(1001, "Covered by section narrative", changed_date="2026-05-01T00:00:00+00:00"),
        _item(1002, "Still uncovered", changed_date="2026-05-01T00:00:00+00:00"),
    )

    gaps = build_coverage_gaps(
        items,
        approved_signals=(),
        narratives={},
        as_of=as_of,
        covered_item_ids=(1001,),
    )

    assert tuple(gap.work_item_id for gap in gaps) == (1002,)
    assert gaps[0].confidence is Confidence.HIGH


def _item(work_item_id: int, title: str, *, state: str = "Active", changed_date: str) -> WorkItem:
    return WorkItem(
        id=work_item_id,
        type="Feature",
        title=title,
        state=state,
        assigned_to="owner@example.com",
        assigned_to_email="owner@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="Sprint 1",
        target_date=date(2026, 6, 1),
        risk_level=RiskLevel.MEDIUM,
        tags=[],
        custom_fields={"changed_date": changed_date},
        revisions=[],
        comments=[],
        fetched_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
    )