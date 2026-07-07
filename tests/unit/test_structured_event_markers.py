from __future__ import annotations

from datetime import datetime, timezone

from src.ai.discovery._structured_event_markers import parse_structured_event_marker_result


def test_parse_structured_event_marker_result_reads_explicit_temporal_hint() -> None:
    parsed = parse_structured_event_marker_result(
        "Decision: Approve Acme ramp | decision_id=decision:acme-ramp | title=Acme ramp approved | "
        "decided_by=person:alice | forum=Weekly | occurred_at=2026-01-15T16:45:00Z"
    )

    assert parsed is not None
    assert parsed.event_type == "decision.made.v1"
    assert parsed.payload["decision_id"] == "decision:acme-ramp"
    assert parsed.occurred_at == datetime(2026, 1, 15, 16, 45, tzinfo=timezone.utc)
    assert parsed.temporal_confidence == "exact"


def test_parse_structured_event_marker_result_uses_metric_window_end_as_temporal_fallback() -> None:
    parsed = parse_structured_event_marker_result(
        "Metric: Deployment snapshot | kpi_id=kpi:deployments | value=12 | unit=count | "
        "window_end=2026-01-15 | dimensions=ring:prod"
    )

    assert parsed is not None
    assert parsed.event_type == "metric.observed.v1"
    assert parsed.payload["window_end"] == "2026-01-15"
    assert parsed.occurred_at == datetime(2026, 1, 15, 0, 0, tzinfo=timezone.utc)
    assert parsed.temporal_confidence == "approximate"


def test_parse_structured_event_marker_result_supports_milestone_completed_markers() -> None:
    parsed = parse_structured_event_marker_result(
        "Milestone: kind=completed | milestone_id=milestone:gen9-ga | completed_on=2026-02-01 | "
        "evidence=Go live approved"
    )

    assert parsed is not None
    assert parsed.event_type == "milestone.completed.v1"
    assert parsed.payload == {
        "milestone_id": "milestone:gen9-ga",
        "completed_on": "2026-02-01",
        "evidence": "Go live approved",
    }
    assert parsed.occurred_at == datetime(2026, 2, 1, 0, 0, tzinfo=timezone.utc)
    assert parsed.temporal_confidence == "approximate"
