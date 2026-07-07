from __future__ import annotations

from datetime import date, datetime, timezone

from src.core.ado_enrichment import build_analytics_history, build_child_work_items, build_significant_findings, extract_child_ids_by_parent, infer_ado_risk_level, serialize_trajectory_points
from src.core.evidence_engine import build_evidence
from src.core.models import ChildWorkItem, RiskLevel, WorkItem


def test_infer_ado_risk_level_uses_risk_assessment_mapping() -> None:
    assert infer_ado_risk_level("Active", [], "On Track") is RiskLevel.LOW
    assert infer_ado_risk_level("Active", [], "At Risk") is RiskLevel.MEDIUM
    assert infer_ado_risk_level("Active", [], "Off Track") is RiskLevel.HIGH
    assert infer_ado_risk_level("Blocked", [], "On Track") is RiskLevel.HIGH


def test_extract_child_ids_and_build_child_work_items() -> None:
    relation_rows = [
        {
            "id": 100,
            "relations": [
                {"rel": "System.LinkTypes.Hierarchy-Forward", "url": "https://dev.azure.com/org/project/_apis/wit/workItems/200"},
                {"rel": "System.LinkTypes.Hierarchy-Forward", "url": "https://dev.azure.com/org/project/_apis/wit/workItems/201"},
            ],
        }
    ]

    child_ids = extract_child_ids_by_parent(relation_rows)

    assert child_ids == {100: (200, 201)}

    child_rows = [
        {
            "id": 200,
            "fields": {
                "System.Id": 200,
                "System.WorkItemType": "Task",
                "System.Title": "Fix schedule gate",
                "System.State": "Active",
                "System.AssignedTo": {"displayName": "Asha", "uniqueName": "asha@example.com"},
                "System.AreaPath": "One\\Adventure\\Acme",
                "System.IterationPath": "Sprint 10",
                "Microsoft.VSTS.Scheduling.TargetDate": "2026-05-20",
                "System.Tags": "Blocked;Ramp",
                "Custom.RiskAssessment": "Off Track",
                "Custom.RiskAssessmentComment": "Waiting on dependency",
            },
        },
        {
            "id": 201,
            "fields": {
                "System.Id": 201,
                "System.WorkItemType": "Product Backlog Item",
                "System.Title": "Close rollout gap",
                "System.State": "Active",
                "System.AreaPath": "One\\Adventure\\Acme",
                "System.IterationPath": "Sprint 10",
                "Microsoft.VSTS.Scheduling.TargetDate": "2026-05-22",
                "System.Tags": "Ramp",
                "Custom.RiskAssessment": "At Risk",
            },
        },
    ]

    children = build_child_work_items(child_rows)

    assert tuple(child.id for child in children) == (200, 201)
    assert children[0].risk_level is RiskLevel.HIGH
    assert children[1].risk_level is RiskLevel.MEDIUM


def test_build_analytics_history_and_significant_findings_capture_slip_context() -> None:
    item = WorkItem(
        id=100,
        type="Feature",
        title="Ramp gating feature",
        state="Active",
        assigned_to="Asha",
        assigned_to_email="asha@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="Sprint 10",
        target_date=date(2026, 5, 20),
        risk_level=RiskLevel.MEDIUM,
        tags=["Ramp"],
        custom_fields={},
        risk_assessment="At Risk",
        risk_assessment_comment="Target moved twice after validation gaps.",
        child_items=(
            ChildWorkItem(
                id=200,
                type="Task",
                title="Fix schedule gate",
                state="Active",
                assigned_to="Asha",
                assigned_to_email="asha@example.com",
                area_path="One\\Adventure\\Acme",
                iteration_path="Sprint 10",
                target_date=date(2026, 5, 18),
                risk_level=RiskLevel.HIGH,
            ),
        ),
    )
    snapshot_rows = [
        {"WorkItemId": 100, "DateValue": "2026-05-01", "State": "Active", "AreaPath": "One\\Adventure\\Acme", "TargetDate": "2026-05-10", "TagNames": "Ramp", "Custom_RiskAssessment": "At Risk"},
        {"WorkItemId": 100, "DateValue": "2026-05-05", "State": "Active", "AreaPath": "One\\Adventure\\Acme", "TargetDate": "2026-05-15", "TagNames": "Ramp", "Custom_RiskAssessment": "At Risk"},
        {"WorkItemId": 100, "DateValue": "2026-05-10", "State": "Active", "AreaPath": "One\\Adventure\\Acme", "TargetDate": "2026-05-20", "TagNames": "Ramp", "Custom_RiskAssessment": "Off Track"},
    ]

    analytics_history = build_analytics_history(snapshot_rows, {100: item})
    points = analytics_history[100]
    findings = build_significant_findings(item, points, as_of=date(2026, 5, 13))

    assert len(points) == 3
    assert points[-1].risk_level is RiskLevel.HIGH
    assert any("Target slipped 2 times" in finding for finding in findings)
    assert any("Linked work:" in finding for finding in findings)
    assert any("Risk assessment At Risk" in finding for finding in findings)
    assert serialize_trajectory_points(points)[-1]["risk_assessment"] == "Off Track"


def test_build_evidence_includes_risk_child_and_findings() -> None:
    item = WorkItem(
        id=100,
        type="Feature",
        title="Ramp gating feature",
        state="Active",
        assigned_to="Asha",
        assigned_to_email="asha@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="Sprint 10",
        target_date=date(2026, 5, 20),
        risk_level=RiskLevel.MEDIUM,
        tags=["Ramp"],
        custom_fields={"significant_findings": ["Target slipped 2 times in the last 90 days; current target 2026-05-20."]},
        fetched_at=datetime(2026, 5, 13, tzinfo=timezone.utc),
        risk_assessment="At Risk",
        risk_assessment_comment="Target moved twice after validation gaps.",
        child_items=(
            ChildWorkItem(
                id=200,
                type="Task",
                title="Fix schedule gate",
                state="Active",
                assigned_to="Asha",
                assigned_to_email="asha@example.com",
                area_path="One\\Adventure\\Acme",
                iteration_path="Sprint 10",
                target_date=date(2026, 5, 18),
                risk_level=RiskLevel.HIGH,
            ),
        ),
    )

    packet = build_evidence(
        item,
        window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        window_end=datetime(2026, 5, 13, 23, 59, tzinfo=timezone.utc),
    )

    assert "Risk assessment: At Risk" in packet.summary_for_reviewer
    assert "Child work: 1 item(s), 1 blocked/high-risk." in packet.summary_for_reviewer
    assert "Finding: Target slipped 2 times" in packet.summary_for_reviewer

def test_build_significant_findings_sanitizes_banned_phrases_in_linked_work_titles() -> None:
    item = WorkItem(
        id=100,
        type="Feature",
        title="Ramp gating feature",
        state="Active",
        assigned_to="Asha",
        assigned_to_email="asha@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="Sprint 10",
        target_date=date(2026, 5, 20),
        risk_level=RiskLevel.MEDIUM,
        tags=[],
        custom_fields={},
        child_items=(
            ChildWorkItem(
                id=200,
                type="Task",
                title="Update various release stages due to dependency drift",
                state="Active",
                assigned_to="Asha",
                assigned_to_email="asha@example.com",
                area_path="One\\Adventure\\Acme",
                iteration_path="Sprint 10",
                target_date=None,
                risk_level=RiskLevel.MEDIUM,
            ),
        ),
    )

    findings = build_significant_findings(item, (), as_of=date(2026, 5, 13))

    assert findings == (
        "Linked work: Task ADO#200 - Update multiple release stages after dependency drift.",
    )