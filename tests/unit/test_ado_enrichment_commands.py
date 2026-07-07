from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from src.commands import freshness, gather, report
from src.core.ado_enrichment import serialize_trajectory_points
from src.core.models import RiskLevel, WorkItem
from src.core.models_v2 import TrajectoryPoint
from src.core.trajectory import backfill_trajectory_points


def test_gather_work_item_from_sources_uses_risk_assessment_mapping() -> None:
    item = gather._work_item_from_sources(
        raw={"WorkItemId": 101, "Title": "Gather item"},
        batch_row={
            "id": 101,
            "fields": {
                "System.Id": 101,
                "System.WorkItemType": "Feature",
                "System.Title": "Gather item",
                "System.State": "Active",
                "System.AreaPath": "One\\Adventure\\Acme",
                "System.IterationPath": "Sprint 10",
                "System.Tags": "Ramp",
                "Custom.RiskAssessment": "At Risk",
                "Custom.RiskAssessmentComment": "Awaiting validation closeout.",
            },
        },
        revision_rows=[],
        comment_rows=[],
        fetched_at=datetime(2026, 5, 13, tzinfo=timezone.utc),
    )

    assert item.risk_level is RiskLevel.MEDIUM
    assert item.risk_assessment == "At Risk"
    assert item.risk_assessment_comment == "Awaiting validation closeout."


def test_report_work_item_from_sources_uses_risk_assessment_mapping() -> None:
    item = report._work_item_from_sources(
        raw={"WorkItemId": 201, "Title": "Report item"},
        batch_row={
            "id": 201,
            "fields": {
                "System.Id": 201,
                "System.WorkItemType": "Feature",
                "System.Title": "Report item",
                "System.State": "Active",
                "System.AreaPath": "One\\Adventure\\Acme",
                "System.IterationPath": "Sprint 10",
                "System.Tags": "Ramp",
                "Custom.RiskAssessment": "Off Track",
                "Custom.RiskAssessmentComment": "Date slipped after dependency miss.",
            },
        },
        fetched_at=datetime(2026, 5, 13, tzinfo=timezone.utc),
    )

    assert item.risk_level is RiskLevel.HIGH
    assert item.risk_assessment == "Off Track"
    assert item.risk_assessment_comment == "Date slipped after dependency miss."


def test_freshness_work_item_from_sources_uses_risk_assessment_mapping() -> None:
    item = freshness._work_item_from_sources(
        raw={"WorkItemId": 301, "Title": "Freshness item"},
        batch_row={
            "id": 301,
            "fields": {
                "System.Id": 301,
                "System.WorkItemType": "Feature",
                "System.Title": "Freshness item",
                "System.State": "Active",
                "System.AreaPath": "One\\Adventure\\Acme",
                "System.IterationPath": "Sprint 10",
                "System.Tags": "Ramp",
                "Custom.RiskAssessment": "On Track",
                "Custom.RiskAssessmentComment": "No material drift this week.",
            },
        },
        comment_rows=[],
        revision_rows=[],
        fetched_at=datetime(2026, 5, 13, tzinfo=timezone.utc),
    )

    assert item.risk_level is RiskLevel.LOW
    assert item.risk_assessment == "On Track"
    assert item.risk_assessment_comment == "No material drift this week."


def test_report_item_trajectory_points_merges_stored_and_analytics_history(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    backfill_trajectory_points(
        "acme",
        401,
        (
            TrajectoryPoint(
                date=date(2026, 5, 1),
                state="Active",
                assigned_to="asha@example.com",
                target_date=date(2026, 5, 10),
                risk_level=RiskLevel.MEDIUM,
                area_path="One\\Adventure\\Acme",
            ),
        ),
        programs_root=programs_root,
    )
    item = WorkItem(
        id=401,
        type="Feature",
        title="Merge history item",
        state="Active",
        assigned_to="Asha",
        assigned_to_email="asha@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="Sprint 10",
        target_date=date(2026, 5, 15),
        risk_level=RiskLevel.MEDIUM,
        tags=["Ramp"],
        custom_fields={
            "analytics_history": list(
                serialize_trajectory_points(
                    (
                        TrajectoryPoint(
                            date=date(2026, 5, 5),
                            state="Active",
                            assigned_to="asha@example.com",
                            target_date=date(2026, 5, 15),
                            risk_level=RiskLevel.HIGH,
                            area_path="One\\Adventure\\Acme",
                            risk_assessment="Off Track",
                        ),
                    )
                )
            )
        },
    )

    fake_program = SimpleNamespace(id="acme", storage_backend="file")
    points = report._item_trajectory_points(item, program=fake_program, programs_root=programs_root)

    assert [point.date for point in points] == [date(2026, 5, 1), date(2026, 5, 5)]
    assert points[-1].risk_assessment == "Off Track"