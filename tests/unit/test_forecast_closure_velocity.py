from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.core import forecast_engine
from src.core.models import (
    AttributionTier, Confidence, DeltaKind, DeltaSet, EvidencePacket, ItemDelta,
    ReviewState, RiskLevel, WorkItem,
)
from src.core.view_models import WorkstreamData


@pytest.mark.parametrize("closed_state", ["Closed", "Done", "Resolved", "Completed"])
@pytest.mark.parametrize("closed_target", [None, date(2026, 10, 25)])
@pytest.mark.parametrize("owned_closure", [False, True])
def test_forecast_counts_only_member_closures_without_filtering_completed_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, closed_state: str,
    closed_target: date | None, owned_closure: bool,
) -> None:
    def item(item_id: int, state: str, target: date | None) -> WorkItem:
        return WorkItem(
            id=item_id, type="Task", title=f"Task {item_id}", state=state,
            assigned_to=None, assigned_to_email=None, area_path="Fixture",
            iteration_path="Fixture", target_date=target, risk_level=RiskLevel.LOW,
            tags=[], custom_fields={},
        )

    members = (
        *(item(item_id, "Active", date(2026, 10, 30)) for item_id in range(1, 5)),
        item(5, closed_state, closed_target),
    )
    closed_id = 5 if owned_closure else 99
    evidence = EvidencePacket(
        work_item_id=closed_id, revisions=(), comments=(), enrichments=(),
        confidence=Confidence.HIGH, tier=AttributionTier.TIER1,
        summary_for_reviewer="Observed closure",
    )
    closure = ItemDelta(
        work_item_id=closed_id, kind=DeltaKind.CLOSED, field_changes={},
        old_risk=None, new_risk=None, old_eta=None, new_eta=None, evidence=evidence,
    )
    history = Mock(return_value=(None,) * 6)
    monkeypatch.setattr(forecast_engine, "get_dimension_history", history)
    result = forecast_engine.build_forecast_assessment(
        enabled=True, edition_name="fixture_weekly",
        as_of=datetime(2026, 10, 25, tzinfo=timezone.utc),
        workstreams=(WorkstreamData(
            section_id="delivery", title="Delivery", blurb="", dependency_cascades=(),
            items=members, citations=(), review_state=ReviewState.PENDING, blocked_count=1,
        ),),
        deltas=DeltaSet(
            issue_number=7, previous_issue_number=6, new_items=(), closed_items=(closure,),
            risk_changes=(), eta_changes=(), unchanged_count=4,
        ),
        archive_root=tmp_path / "archive", slip_threshold_days=1,
    )
    assert result is not None
    assert f"closure_velocity={int(owned_closure)}" in result.formula
    assert result.confidence == (Confidence.HIGH if owned_closure else Confidence.MEDIUM)
    assert result.current_eta == date(2026, 10, 30)
    assert result.slip_days == (4 if owned_closure else 10)
    assert result.source_item_ids == (1, 2, 3)
    history.assert_called_once_with(
        "fixture_weekly", "Delivery", archive_root=tmp_path / "archive", last_n=6,
    )
