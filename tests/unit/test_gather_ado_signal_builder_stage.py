"""Direct coverage for the extracted ADO signal-builder stage."""

from __future__ import annotations

from datetime import date, datetime, timezone

from src.commands.gather_pipeline import ado_signal_builder_stage
from src.core.models import RiskLevel, WorkItem
from src.core.models_v2 import Workstream


def _work_item(*, item_id: int, state: str, iteration_path: str) -> WorkItem:
    return WorkItem(
        id=item_id,
        type="Feature",
        title=f"Checkpoint {item_id}",
        state=state,
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path=iteration_path,
        target_date=date(2026, 5, 16),
        risk_level=RiskLevel.MEDIUM,
        tags=[],
        custom_fields={},
        revisions=[],
        comments=[],
        fetched_at=datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc),
    )


def test_build_analytics_signals_summarizes_scope_flow_and_history() -> None:
    workstreams = (
        Workstream(
            id="deployment_readiness",
            name="Deployment Readiness",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )

    signals = ado_signal_builder_stage.build_analytics_signals(
        rows=[
            {
                "DateSK": 20260509,
                "WorkItemId": 101,
                "WorkItemType": "Feature",
                "Title": "Checkpoint A",
                "State": "Active",
                "AreaPath": "One\\Adventure\\Acme",
                "CompletedDateSK": None,
                "CycleTimeDays": None,
                "LeadTimeDays": None,
            },
            {
                "DateSK": 20260509,
                "WorkItemId": 202,
                "WorkItemType": "Feature",
                "Title": "Checkpoint B",
                "State": "Closed",
                "AreaPath": "One\\Adventure\\Acme",
                "CompletedDateSK": 20260508,
                "CycleTimeDays": 4.0,
                "LeadTimeDays": 7.0,
            },
            {
                "DateSK": 20260510,
                "WorkItemId": 101,
                "WorkItemType": "Feature",
                "Title": "Checkpoint A",
                "State": "Closed",
                "AreaPath": "One\\Adventure\\Acme",
                "CompletedDateSK": 20260510,
                "CycleTimeDays": 5.0,
                "LeadTimeDays": 8.0,
            },
            {
                "DateSK": 20260510,
                "WorkItemId": 202,
                "WorkItemType": "Feature",
                "Title": "Checkpoint B",
                "State": "Closed",
                "AreaPath": "One\\Adventure\\Acme",
                "CompletedDateSK": 20260508,
                "CycleTimeDays": 4.0,
                "LeadTimeDays": 7.0,
            },
        ],
        program_id="acme",
        workstreams=workstreams,
        start_date_sk=20260426,
        end_date_sk=20260510,
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    assert len(signals) == 1
    assert "scope stable vs 2026-05-09" in signals[0].text
    assert "open down 1 vs 2026-05-09" in signals[0].text
    assert "burndown 1->0 open" not in signals[0].text
    assert "flow: Closed=2" in signals[0].text
    assert signals[0].metadata is not None
    assert signals[0].metadata["latest_open_item_count"] == 0
    assert signals[0].metadata["average_cycle_time_days"] == 4.5


def test_build_sprint_signals_records_previous_and_historical_metrics() -> None:
    workstreams = (
        Workstream(
            id="deployment_readiness",
            name="Deployment Readiness",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    items = (
        _work_item(item_id=501, state="Closed", iteration_path="One\\Sprint 24"),
        _work_item(item_id=502, state="Closed", iteration_path="One\\Sprint 24"),
        _work_item(item_id=503, state="Closed", iteration_path="One\\Sprint 24"),
    )

    signals = ado_signal_builder_stage.build_sprint_signals(
        iterations_by_team={
            None: (
                {
                    "id": "iteration-24",
                    "name": "Sprint 24",
                    "path": "One\\Sprint 24",
                    "attributes": {
                        "startDate": "2026-05-11T00:00:00Z",
                        "finishDate": "2026-05-15T00:00:00Z",
                        "timeFrame": "current",
                    },
                },
            )
        },
        capacities_by_team_iteration={(None, "iteration-24"): ()},
        sprint_snapshot_rows=[
            {"DateSK": 20260414, "WorkItemId": 1, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 21"},
            {"DateSK": 20260414, "WorkItemId": 2, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 21"},
            {"DateSK": 20260414, "WorkItemId": 3, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 21"},
            {"DateSK": 20260415, "WorkItemId": 1, "State": "Closed", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 21"},
            {"DateSK": 20260415, "WorkItemId": 2, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 21"},
            {"DateSK": 20260415, "WorkItemId": 3, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 21"},
            {"DateSK": 20260416, "WorkItemId": 1, "State": "Closed", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 21"},
            {"DateSK": 20260416, "WorkItemId": 2, "State": "Closed", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 21"},
            {"DateSK": 20260416, "WorkItemId": 3, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 21"},
            {"DateSK": 20260422, "WorkItemId": 101, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 22"},
            {"DateSK": 20260422, "WorkItemId": 102, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 22"},
            {"DateSK": 20260422, "WorkItemId": 103, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 22"},
            {"DateSK": 20260423, "WorkItemId": 101, "State": "Closed", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 22"},
            {"DateSK": 20260423, "WorkItemId": 102, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 22"},
            {"DateSK": 20260423, "WorkItemId": 103, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 22"},
            {"DateSK": 20260424, "WorkItemId": 101, "State": "Closed", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 22"},
            {"DateSK": 20260424, "WorkItemId": 102, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 22"},
            {"DateSK": 20260424, "WorkItemId": 103, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 22"},
            {"DateSK": 20260506, "WorkItemId": 301, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 23"},
            {"DateSK": 20260506, "WorkItemId": 302, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 23"},
            {"DateSK": 20260506, "WorkItemId": 303, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 23"},
            {"DateSK": 20260507, "WorkItemId": 301, "State": "Closed", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 23"},
            {"DateSK": 20260507, "WorkItemId": 302, "State": "Closed", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 23"},
            {"DateSK": 20260507, "WorkItemId": 303, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 23"},
            {"DateSK": 20260508, "WorkItemId": 301, "State": "Closed", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 23"},
            {"DateSK": 20260508, "WorkItemId": 302, "State": "Closed", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 23"},
            {"DateSK": 20260508, "WorkItemId": 303, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 23"},
            {"DateSK": 20260511, "WorkItemId": 501, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 24"},
            {"DateSK": 20260511, "WorkItemId": 502, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 24"},
            {"DateSK": 20260511, "WorkItemId": 503, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 24"},
            {"DateSK": 20260512, "WorkItemId": 501, "State": "Closed", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 24"},
            {"DateSK": 20260512, "WorkItemId": 502, "State": "Closed", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 24"},
            {"DateSK": 20260512, "WorkItemId": 503, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 24"},
            {"DateSK": 20260513, "WorkItemId": 501, "State": "Closed", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 24"},
            {"DateSK": 20260513, "WorkItemId": 502, "State": "Closed", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 24"},
            {"DateSK": 20260513, "WorkItemId": 503, "State": "Closed", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 24"},
        ],
        items=items,
        program_id="acme",
        workstreams=workstreams,
        as_of=datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc),
        normalize_ado_team_name_fn=lambda value: None if value is None else value.strip() or None,
    )

    assert len(signals) == 1
    assert "1 fewer open vs last sprint" in signals[0].text
    assert "0.5/day faster vs last sprint" in signals[0].text
    assert signals[0].metadata is not None
    assert signals[0].metadata["three_iteration_average_completion_per_business_day"] == 1.0
    assert signals[0].metadata["three_iteration_completion_per_business_day_history"] == (0.5, 1.0, 1.5)
    assert signals[0].metadata["historical_iteration_window_count"] == 4
    assert signals[0].metadata["historical_open_history_series"] == ((3, 2, 1), (3, 2, 2), (3, 1, 1), (3, 1, 0))
