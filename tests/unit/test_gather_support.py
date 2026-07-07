from __future__ import annotations

from datetime import date, datetime, timezone

from src.commands.gather_pipeline.support import (
    format_iteration_window,
    summarize_sprint_pace,
    summarize_sprint_throughput,
)


def test_summarize_sprint_throughput_projects_at_risk_close() -> None:
    summary = summarize_sprint_throughput(
        committed_item_count=4,
        completed_item_count=2,
        open_item_count=2,
        pace_summary={
            "elapsed_business_days": 5,
            "remaining_business_days": 2,
        },
        completion_pct=50,
    )

    assert summary is not None
    assert summary["projected_completion_pct"] == 70
    assert summary["projection_status"] == "at_risk"
    assert summary["text"] == "~70% by close at 0.4/day (1.0/day needed)"


def test_summarize_sprint_pace_marks_on_track() -> None:
    summary = summarize_sprint_pace(
        date(2026, 5, 11),
        date(2026, 5, 15),
        as_of=datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc),
        completion_pct=60,
    )

    assert summary is not None
    assert summary["elapsed_business_days"] == 3
    assert summary["remaining_business_days"] == 2
    assert summary["expected_completion_pct"] == 60
    assert summary["pace_status"] == "on_track"
    assert summary["text"] == "pace on track vs 60% elapsed"


def test_format_iteration_window_reports_remaining_days() -> None:
    assert format_iteration_window(
        date(2026, 5, 11),
        date(2026, 5, 15),
        as_of=datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc),
    ) == "window 2026-05-11 to 2026-05-15 (2d remaining)"
