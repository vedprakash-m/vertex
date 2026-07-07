from __future__ import annotations

from datetime import datetime, timezone

from src.core.models import Confidence
from src.core.models_v2 import Signal
from src.core.telemetry_summary import build_approved_telemetry_summary


def test_build_approved_telemetry_summary_includes_sprint_pace_when_available() -> None:
    analytics_signal = Signal(
        id="analytics-1",
        timestamp=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
        source="ado/analytics",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: analytics summary",
        raw_ref="ado-analytics:deployment_readiness:20260510:20260426:20260510",
        confidence=Confidence.HIGH,
        metadata={
            "snapshot_item_count": 5,
            "completed_item_count": 2,
            "open_delta_count": -1,
            "average_cycle_time_days": 5.0,
            "average_lead_time_days": 8.0,
        },
        thread_id=None,
    )
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "committed_item_count": 2,
            "completed_item_count": 1,
            "iteration_name": "Sprint 24",
            "completion_pct": 50,
            "open_item_count": 1,
            "elapsed_business_days": 5,
            "total_business_days": 10,
            "remaining_business_days": 5,
            "expected_completion_pct": 50,
            "pace_status": "behind",
            "pace_delta_pct": -20,
            "projection_status": "at_risk",
            "projected_completion_pct": 75,
            "observed_completion_per_business_day": 0.5,
            "required_completion_per_business_day": 1.0,
        },
        thread_id=None,
    )

    summary = build_approved_telemetry_summary((analytics_signal, sprint_signal))

    assert summary == (
        "analytics, 5 scope, 2 completed, open down 1, cycle 5.0d / lead 8.0d; "
        "sprint, Sprint 24, 2 committed, 1 completed, 50% complete, 1 open, 5/10 bd elapsed, 5 bd left, pace 20pts behind 50% elapsed, ~75% by close at 0.5/day (1.0/day needed)"
    )


def test_build_approved_telemetry_summary_preserves_expected_elapsed_progress_for_on_track_pace() -> None:
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "committed_item_count": 6,
            "completed_item_count": 3,
            "completion_pct": 50,
            "open_item_count": 3,
            "elapsed_business_days": 5,
            "total_business_days": 10,
            "remaining_business_days": 5,
            "expected_completion_pct": 50,
            "pace_status": "on_track",
        },
        thread_id=None,
    )

    summary = build_approved_telemetry_summary((sprint_signal,))

    assert summary == (
        "sprint, Sprint 24, 6 committed, 3 completed, 50% complete, 3 open, 5/10 bd elapsed, 5 bd left, pace on track vs 50% elapsed"
    )


def test_build_approved_telemetry_summary_keeps_workstream_context_coherent() -> None:
    analytics_signal = Signal(
        id="analytics-1",
        timestamp=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
        source="ado/analytics",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: analytics summary",
        raw_ref="ado-analytics:deployment_readiness:20260510:20260426:20260510",
        confidence=Confidence.HIGH,
        metadata={
            "snapshot_item_count": 5,
            "completed_item_count": 2,
            "open_delta_count": -1,
            "average_cycle_time_days": 5.0,
            "average_lead_time_days": 8.0,
        },
        thread_id=None,
    )
    unrelated_analytics_signal = Signal(
        id="analytics-2",
        timestamp=datetime(2026, 5, 10, 10, 30, tzinfo=timezone.utc),
        source="ado/analytics",
        program_id="acme",
        workstream_id="platform_readiness",
        entity_refs=(),
        text="Platform Readiness: analytics summary",
        raw_ref="ado-analytics:platform_readiness:20260510:20260426:20260510",
        confidence=Confidence.HIGH,
        metadata={
            "snapshot_item_count": 9,
            "completed_item_count": 4,
            "scope_delta_count": 3,
        },
        thread_id=None,
    )
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "committed_item_count": 2,
            "completed_item_count": 1,
            "iteration_name": "Sprint 24",
            "completion_pct": 50,
            "open_item_count": 1,
            "elapsed_business_days": 5,
            "total_business_days": 10,
            "remaining_business_days": 5,
            "expected_completion_pct": 50,
            "pace_status": "behind",
            "pace_delta_pct": -20,
            "projection_status": "at_risk",
            "projected_completion_pct": 75,
            "observed_completion_per_business_day": 0.5,
            "required_completion_per_business_day": 1.0,
        },
        thread_id=None,
    )

    summary = build_approved_telemetry_summary(
        (analytics_signal, unrelated_analytics_signal, sprint_signal)
    )

    assert summary == (
        "analytics, 5 scope, 2 completed, open down 1, cycle 5.0d / lead 8.0d; "
        "sprint, Sprint 24, 2 committed, 1 completed, 50% complete, 1 open, 5/10 bd elapsed, 5 bd left, pace 20pts behind 50% elapsed, ~75% by close at 0.5/day (1.0/day needed)"
    )


def test_build_approved_telemetry_summary_includes_pipeline_failures_without_losing_workstream_context() -> None:
    analytics_signal = Signal(
        id="analytics-1",
        timestamp=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
        source="ado/analytics",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: analytics summary",
        raw_ref="ado-analytics:deployment_readiness:20260510:20260426:20260510",
        confidence=Confidence.HIGH,
        metadata={
            "snapshot_item_count": 5,
            "completed_item_count": 2,
            "open_delta_count": -1,
        },
        thread_id=None,
    )
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "completion_pct": 50,
            "open_item_count": 1,
        },
        thread_id=None,
    )
    focused_pipeline_signal = Signal(
        id="pipeline-1",
        timestamp=datetime(2026, 5, 10, 10, 45, tzinfo=timezone.utc),
        source="ado/pipeline",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: pipeline summary",
        raw_ref="ado-pipeline:deployment_readiness:42:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "pipelines": [
                {
                    "pipeline_name": "Build Validation",
                    "recent_run_count": 3,
                    "failed_run_count": 1,
                    "latest_failure_run_id": 104,
                    "latest_run_id": 105,
                    "latest_run_result": "succeeded",
                }
            ]
        },
        thread_id=None,
    )
    unrelated_pipeline_signal = Signal(
        id="pipeline-2",
        timestamp=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        source="ado/pipeline",
        program_id="acme",
        workstream_id="platform_readiness",
        entity_refs=(),
        text="Platform Readiness: pipeline summary",
        raw_ref="ado-pipeline:platform_readiness:84:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "pipelines": [
                {
                    "pipeline_name": "Release Validation",
                    "recent_run_count": 2,
                    "failed_run_count": 2,
                    "latest_failure_run_id": 205,
                    "latest_run_id": 205,
                    "latest_run_result": "failed",
                }
            ]
        },
        thread_id=None,
    )

    summary = build_approved_telemetry_summary(
        (analytics_signal, sprint_signal, focused_pipeline_signal, unrelated_pipeline_signal)
    )

    assert summary == (
        "analytics, 5 scope, 2 completed, open down 1; "
        "sprint, Sprint 24, 50% complete, 1 open; "
        "pipeline, Build Validation, 1/3 failed, latest fail #104, latest #105 succeeded"
    )


def test_build_approved_telemetry_summary_includes_open_pull_request_pressure() -> None:
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "completion_pct": 50,
            "open_item_count": 1,
        },
        thread_id=None,
    )
    pull_request_signal = Signal(
        id="pr-1",
        timestamp=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        source="ado/pr",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("PR:XStoreApp/301",),
        text="Deployment Readiness: pull request summary",
        raw_ref="ado-pr:deployment_readiness:repo-42:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "repositories": [
                {
                    "repository_name": "XStoreApp",
                    "open_pr_count": 2,
                    "p90_age_days": 10.0,
                    "oldest_pr_id": 301,
                    "oldest_pr_age_days": 10.0,
                }
            ]
        },
        thread_id=None,
    )

    summary = build_approved_telemetry_summary((sprint_signal, pull_request_signal))

    assert summary == (
        "sprint, Sprint 24, 50% complete, 1 open; "
        "pull requests, XStoreApp, 2 open PRs, P90 age 10.0d, oldest #301 10.0d"
    )


def test_build_approved_telemetry_summary_includes_wiql_signal_summaries() -> None:
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "completion_pct": 50,
            "open_item_count": 1,
        },
        thread_id=None,
    )
    wiql_signal = Signal(
        id="wiql-1",
        timestamp=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        source="ado/wiql",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("WI:1001", "WI:1002"),
        text="SCHIE Open: 2 item(s) matched WIQL query schie-open; top WI:1001, WI:1002",
        raw_ref="ado_wiql:schie-open:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "query_id": "schie-open",
            "work_item_count": 2,
            "date": "2026-05-10",
        },
        thread_id=None,
    )

    summary = build_approved_telemetry_summary((sprint_signal, wiql_signal))

    assert summary == (
        "wiql, SCHIE Open, 2 items; "
        "sprint, Sprint 24, 50% complete, 1 open"
    )


def test_build_approved_telemetry_summary_includes_sprint_team_capacity_context() -> None:
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "completion_pct": 50,
            "open_item_count": 1,
            "team_member_count": 3,
            "members_with_capacity": 2,
            "total_capacity_per_day": 24.0,
            "days_off_entry_count": 1,
        },
        thread_id=None,
    )

    summary = build_approved_telemetry_summary((sprint_signal,))

    assert summary == (
        "sprint, Sprint 24, 50% complete, 1 open, team cap 24.0h/day across 3 members, 2 with cap, 1 day off"
    )


def test_build_approved_telemetry_summary_includes_sprint_commitment_counts() -> None:
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "committed_item_count": 8,
            "completed_item_count": 4,
            "completion_pct": 50,
            "open_item_count": 4,
        },
        thread_id=None,
    )

    summary = build_approved_telemetry_summary((sprint_signal,))

    assert summary == "sprint, Sprint 24, 8 committed, 4 completed, 50% complete, 4 open"


def test_build_approved_telemetry_summary_includes_finish_projection_rate_context() -> None:
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "committed_item_count": 8,
            "completed_item_count": 6,
            "completion_pct": 75,
            "open_item_count": 2,
            "elapsed_business_days": 8,
            "total_business_days": 10,
            "remaining_business_days": 2,
            "projection_status": "finish",
            "projected_completion_pct": 100,
            "observed_completion_per_business_day": 1.0,
            "required_completion_per_business_day": 1.0,
        },
        thread_id=None,
    )

    summary = build_approved_telemetry_summary((sprint_signal,))

    assert summary == (
        "sprint, Sprint 24, 8 committed, 6 completed, 75% complete, 2 open, 8/10 bd elapsed, 2 bd left, track to finish at 1.0/day (1.0/day needed)"
    )


def test_build_approved_telemetry_summary_includes_complete_projection_status() -> None:
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "committed_item_count": 8,
            "completed_item_count": 8,
            "completion_pct": 100,
            "open_item_count": 0,
            "elapsed_business_days": 10,
            "total_business_days": 10,
            "remaining_business_days": 0,
            "projection_status": "complete",
            "projected_completion_pct": 100,
            "observed_completion_per_business_day": 1.0,
            "required_completion_per_business_day": 0.8,
        },
        thread_id=None,
    )

    summary = build_approved_telemetry_summary((sprint_signal,))

    assert summary == (
        "sprint, Sprint 24, 8 committed, 8 completed, 100% complete, 0 open, 10/10 bd elapsed, 0 bd left, finished"
    )


def test_build_approved_telemetry_summary_includes_sprint_burndown_history() -> None:
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "committed_item_count": 8,
            "completed_item_count": 6,
            "completion_pct": 75,
            "open_item_count": 2,
            "open_history": {
                "2026-05-08": 4,
                "2026-05-09": 3,
                "2026-05-10": 2,
            },
            "completed_history": {
                "2026-05-08": 4,
                "2026-05-09": 5,
                "2026-05-10": 6,
            },
            "recent_completion_per_business_day": 1.0,
            "recent_completion_snapshot_count": 3,
        },
        thread_id=None,
    )

    summary = build_approved_telemetry_summary((sprint_signal,))

    assert summary == (
        "sprint, Sprint 24, 8 committed, 6 completed, 75% complete, 2 open, burndown 4->3->2 open, completion 4->5->6 done, recent 1.0/day over 3 snapshots"
    )


def test_build_approved_telemetry_summary_includes_analytics_state_mix() -> None:
    analytics_signal = Signal(
        id="analytics-1",
        timestamp=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
        source="ado/analytics",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: analytics summary",
        raw_ref="ado-analytics:deployment_readiness:20260510:20260426:20260510",
        confidence=Confidence.HIGH,
        metadata={
            "snapshot_item_count": 6,
            "completed_item_count": 2,
            "open_delta_count": -1,
            "average_cycle_time_days": 5.0,
            "average_lead_time_days": 8.0,
            "state_counts": {"Active": 3, "Closed": 1, "Resolved": 2},
        },
        thread_id=None,
    )

    summary = build_approved_telemetry_summary((analytics_signal,))

    assert summary == (
        "analytics, 6 scope, 2 completed, open down 1, cycle 5.0d / lead 8.0d, flow Active=3 / Resolved=2 / Closed=1"
    )


def test_build_approved_telemetry_summary_includes_analytics_scope_delta() -> None:
    analytics_signal = Signal(
        id="analytics-1",
        timestamp=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
        source="ado/analytics",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: analytics summary",
        raw_ref="ado-analytics:deployment_readiness:20260510:20260426:20260510",
        confidence=Confidence.HIGH,
        metadata={
            "snapshot_item_count": 6,
            "completed_item_count": 2,
            "scope_delta_count": 2,
            "open_delta_count": -1,
            "average_cycle_time_days": 5.0,
            "average_lead_time_days": 8.0,
        },
        thread_id=None,
    )

    summary = build_approved_telemetry_summary((analytics_signal,))

    assert summary == "analytics, 6 scope, 2 completed, scope up 2, open down 1, cycle 5.0d / lead 8.0d"


def test_build_approved_telemetry_summary_includes_analytics_burndown_history() -> None:
    analytics_signal = Signal(
        id="analytics-1",
        timestamp=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
        source="ado/analytics",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: analytics summary",
        raw_ref="ado-analytics:deployment_readiness:20260510:20260426:20260510",
        confidence=Confidence.HIGH,
        metadata={
            "snapshot_item_count": 6,
            "completed_item_count": 2,
            "open_delta_count": -2,
            "open_history": {
                "2026-05-08": 2,
                "2026-05-09": 1,
                "2026-05-10": 0,
            },
            "average_cycle_time_days": 5.0,
            "average_lead_time_days": 8.0,
        },
        thread_id=None,
    )

    summary = build_approved_telemetry_summary((analytics_signal,))

    assert summary == (
        "analytics, 6 scope, 2 completed, open down 2, burndown 2->1->0 open, cycle 5.0d / lead 8.0d"
    )


def test_build_approved_telemetry_summary_includes_sprint_throughput_trend_when_available() -> None:
    previous_sprint_signal = Signal(
        id="sprint-0",
        timestamp=datetime(2026, 5, 3, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: prior sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-23:2026-05-03",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 23",
            "completion_pct": 40,
            "open_item_count": 2,
            "observed_completion_per_business_day": 0.1,
        },
        thread_id=None,
    )
    current_sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: current sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "completion_pct": 50,
            "open_item_count": 1,
            "observed_completion_per_business_day": 0.3,
        },
        thread_id=None,
    )

    summary = build_approved_telemetry_summary((previous_sprint_signal, current_sprint_signal))

    assert summary == (
        "sprint, Sprint 24, 50% complete, 1 open, 1 fewer open vs last sprint, 0.2/day faster vs last sprint"
    )


def test_build_approved_telemetry_summary_includes_snapshot_backed_previous_sprint_throughput_comparison() -> None:
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: current sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "completion_pct": 50,
            "open_item_count": 1,
            "recent_completion_per_business_day": 1.0,
            "recent_completion_snapshot_count": 3,
            "previous_iteration_completion_per_business_day": 0.5,
        },
        thread_id=None,
    )

    summary = build_approved_telemetry_summary((sprint_signal,))

    assert summary == (
        "sprint, Sprint 24, 50% complete, 1 open, recent 1.0/day over 3 snapshots, 0.5/day faster vs last sprint"
    )


def test_build_approved_telemetry_summary_includes_snapshot_backed_previous_sprint_history() -> None:
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: current sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "completion_pct": 100,
            "open_item_count": 0,
            "recent_completion_per_business_day": 1.0,
            "recent_completion_snapshot_count": 3,
            "previous_iteration_open_item_count": 1,
            "previous_iteration_open_history": {
                "2026-05-06": 2,
                "2026-05-07": 1,
                "2026-05-08": 1,
            },
            "previous_iteration_completed_history": {
                "2026-05-06": 0,
                "2026-05-07": 1,
                "2026-05-08": 1,
            },
            "previous_iteration_completion_per_business_day": 0.5,
        },
        thread_id=None,
    )

    summary = build_approved_telemetry_summary((sprint_signal,))

    assert summary == (
        "sprint, Sprint 24, 100% complete, 0 open, recent 1.0/day over 3 snapshots, "
        "1 fewer open vs last sprint, last sprint burndown 2->1->1 open, "
        "last sprint completion 0->1->1 done, 0.5/day faster vs last sprint"
    )


def test_build_approved_telemetry_summary_includes_three_sprint_average_when_available() -> None:
    oldest_sprint_signal = Signal(
        id="sprint-0",
        timestamp=datetime(2026, 4, 26, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: older sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-22:2026-04-26",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 22",
            "completion_pct": 80,
            "open_item_count": 0,
            "observed_completion_per_business_day": 0.1,
        },
        thread_id=None,
    )
    previous_sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 3, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: prior sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-23:2026-05-03",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 23",
            "completion_pct": 40,
            "open_item_count": 2,
            "observed_completion_per_business_day": 0.2,
        },
        thread_id=None,
    )
    current_sprint_signal = Signal(
        id="sprint-2",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: current sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "completion_pct": 50,
            "open_item_count": 1,
            "observed_completion_per_business_day": 0.3,
        },
        thread_id=None,
    )

    summary = build_approved_telemetry_summary(
        (oldest_sprint_signal, previous_sprint_signal, current_sprint_signal)
    )

    assert summary == (
        "sprint, Sprint 24, 50% complete, 1 open, 1 fewer open vs last sprint, 0.1/day faster vs last sprint, 3-sprint avg 0.2/day, throughput trend up 0.2/day over 3 sprints, 3-sprint open avg 1"
    )


def test_build_approved_telemetry_summary_includes_snapshot_backed_three_sprint_history_summaries() -> None:
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: current sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "completion_pct": 100,
            "open_item_count": 0,
            "three_iteration_average_completion_per_business_day": 1.0,
            "three_iteration_completion_per_business_day_history": (0.5, 1.0, 1.5),
            "three_iteration_completed_history_series": ((0, 1, 1), (0, 2, 2), (0, 2, 3)),
            "three_iteration_throughput_trend_direction": "up",
            "three_iteration_throughput_trend_delta_per_business_day": 1.0,
            "three_iteration_average_open_item_count": 1,
            "three_iteration_open_item_count_history": (2, 1, 0),
            "three_iteration_open_history_series": ((3, 2, 2), (3, 1, 1), (3, 1, 0)),
            "three_iteration_open_trend_direction": "down",
            "three_iteration_open_trend_delta_count": -2,
        },
        thread_id=None,
    )

    summary = build_approved_telemetry_summary((sprint_signal,))

    assert summary == (
        "sprint, Sprint 24, 100% complete, 0 open, 3-sprint avg 1.0/day, 3-sprint throughput 0.5->1.0->1.5/day, "
        "throughput trend up 1.0/day over 3 sprints, 3-sprint open avg 1, 3-sprint open 2->1->0, "
        "3-sprint burndown 3->2->2 | 3->1->1 | 3->1->0 open, 3-sprint completion 0->1->1 | 0->2->2 | 0->2->3 done, "
        "open trend down 2 over 3 sprints"
    )


def test_build_approved_telemetry_summary_includes_snapshot_backed_broader_historical_sprint_window() -> None:
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: current sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "completion_pct": 100,
            "open_item_count": 0,
            "three_iteration_average_completion_per_business_day": 1.0,
            "three_iteration_completion_per_business_day_history": (0.5, 1.0, 1.5),
            "three_iteration_completed_history_series": ((0, 1, 1), (0, 2, 2), (0, 2, 3)),
            "three_iteration_throughput_trend_direction": "up",
            "three_iteration_throughput_trend_delta_per_business_day": 1.0,
            "three_iteration_average_open_item_count": 1,
            "three_iteration_open_item_count_history": (2, 1, 0),
            "three_iteration_open_history_series": ((3, 2, 2), (3, 1, 1), (3, 1, 0)),
            "three_iteration_open_trend_direction": "down",
            "three_iteration_open_trend_delta_count": -2,
            "historical_iteration_window_count": 4,
            "historical_completion_per_business_day_history": (1.0, 0.5, 1.0, 1.5),
            "historical_completed_history_series": ((0, 1, 2), (0, 1, 1), (0, 2, 2), (0, 2, 3)),
            "historical_throughput_trend_direction": None,
            "historical_throughput_trend_delta_per_business_day": None,
            "historical_open_item_count_history": (1, 2, 1, 0),
            "historical_open_history_series": ((3, 2, 1), (3, 2, 2), (3, 1, 1), (3, 1, 0)),
            "historical_open_trend_direction": None,
            "historical_open_trend_delta_count": None,
        },
        thread_id=None,
    )

    summary = build_approved_telemetry_summary((sprint_signal,))

    assert summary == (
        "sprint, Sprint 24, 100% complete, 0 open, 3-sprint avg 1.0/day, 3-sprint throughput 0.5->1.0->1.5/day, "
        "throughput trend up 1.0/day over 3 sprints, 4-sprint throughput 1.0->0.5->1.0->1.5/day, "
        "3-sprint open avg 1, 3-sprint open 2->1->0, 4-sprint open 1->2->1->0, 3-sprint burndown 3->2->2 | 3->1->1 | 3->1->0 open, "
        "3-sprint completion 0->1->1 | 0->2->2 | 0->2->3 done, 4-sprint burndown 3->2->1 | 3->2->2 | 3->1->1 | 3->1->0 open, "
        "4-sprint completion 0->1->2 | 0->1->1 | 0->2->2 | 0->2->3 done, open trend down 2 over 3 sprints"
    )


def test_build_approved_telemetry_summary_includes_sprint_capacity_utilization_trend() -> None:
    previous_sprint_signal = Signal(
        id="sprint-0",
        timestamp=datetime(2026, 5, 3, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: prior sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-23:2026-05-03",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 23",
            "completion_pct": 40,
            "open_item_count": 2,
            "observed_completion_per_business_day": 0.2,
            "required_completion_per_business_day": 0.3,
        },
        thread_id=None,
    )
    current_sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: current sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "completion_pct": 50,
            "open_item_count": 1,
            "observed_completion_per_business_day": 0.3,
            "required_completion_per_business_day": 0.4,
        },
        thread_id=None,
    )

    summary = build_approved_telemetry_summary((previous_sprint_signal, current_sprint_signal))

    assert summary == (
        "sprint, Sprint 24, 50% complete, 1 open, 1 fewer open vs last sprint, 0.1/day faster vs last sprint, capacity util 75% vs 67% last sprint"
    )


def test_build_approved_telemetry_summary_includes_three_sprint_capacity_utilization_average() -> None:
    oldest_sprint_signal = Signal(
        id="sprint-0",
        timestamp=datetime(2026, 4, 26, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: older sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-22:2026-04-26",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 22",
            "completion_pct": 80,
            "open_item_count": 0,
            "observed_completion_per_business_day": 0.1,
            "required_completion_per_business_day": 0.2,
        },
        thread_id=None,
    )
    previous_sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 3, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: prior sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-23:2026-05-03",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 23",
            "completion_pct": 40,
            "open_item_count": 2,
            "observed_completion_per_business_day": 0.2,
            "required_completion_per_business_day": 0.4,
        },
        thread_id=None,
    )
    current_sprint_signal = Signal(
        id="sprint-2",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: current sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "completion_pct": 50,
            "open_item_count": 1,
            "observed_completion_per_business_day": 0.3,
            "required_completion_per_business_day": 0.4,
        },
        thread_id=None,
    )

    summary = build_approved_telemetry_summary(
        (oldest_sprint_signal, previous_sprint_signal, current_sprint_signal)
    )

    assert summary == (
        "sprint, Sprint 24, 50% complete, 1 open, 1 fewer open vs last sprint, 0.1/day faster vs last sprint, capacity util 75% vs 50% last sprint, 3-sprint avg 0.2/day, throughput trend up 0.2/day over 3 sprints, 3-sprint cap util avg 58%, 3-sprint open avg 1"
    )


def test_build_approved_telemetry_summary_includes_sprint_open_comparison() -> None:
    previous_sprint_signal = Signal(
        id="sprint-0",
        timestamp=datetime(2026, 5, 3, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: prior sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-23:2026-05-03",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 23",
            "completion_pct": 40,
            "open_item_count": 3,
        },
        thread_id=None,
    )
    current_sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: current sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "completion_pct": 50,
            "open_item_count": 1,
        },
        thread_id=None,
    )

    summary = build_approved_telemetry_summary((previous_sprint_signal, current_sprint_signal))

    assert summary == "sprint, Sprint 24, 50% complete, 1 open, 2 fewer open vs last sprint"


def test_build_approved_telemetry_summary_includes_snapshot_backed_previous_sprint_open_comparison() -> None:
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: current sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "completion_pct": 50,
            "open_item_count": 1,
            "previous_iteration_open_item_count": 2,
        },
        thread_id=None,
    )

    summary = build_approved_telemetry_summary((sprint_signal,))

    assert summary == "sprint, Sprint 24, 50% complete, 1 open, 1 fewer open vs last sprint"


def test_build_approved_telemetry_summary_includes_three_sprint_open_trend() -> None:
    oldest_sprint_signal = Signal(
        id="sprint-0",
        timestamp=datetime(2026, 4, 26, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: older sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-22:2026-04-26",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 22",
            "completion_pct": 20,
            "open_item_count": 5,
        },
        thread_id=None,
    )
    previous_sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 3, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: prior sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-23:2026-05-03",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 23",
            "completion_pct": 35,
            "open_item_count": 3,
        },
        thread_id=None,
    )
    current_sprint_signal = Signal(
        id="sprint-2",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: current sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "completion_pct": 50,
            "open_item_count": 1,
        },
        thread_id=None,
    )

    summary = build_approved_telemetry_summary(
        (oldest_sprint_signal, previous_sprint_signal, current_sprint_signal)
    )

    assert summary == (
        "sprint, Sprint 24, 50% complete, 1 open, 2 fewer open vs last sprint, 3-sprint open avg 3, open trend down 4 over 3 sprints"
    )