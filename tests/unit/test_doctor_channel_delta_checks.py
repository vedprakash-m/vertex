from __future__ import annotations

from datetime import datetime, timezone

from src.commands.doctor_checks.channel_delta_checks import channel_delta_check, channel_health_snapshot


def test_channel_delta_check_warns_on_new_regressions() -> None:
    check = channel_delta_check(
        previous_gathered_at=datetime(2026, 5, 10, 17, 0, tzinfo=timezone.utc),
        current_channels={
            "kusto": {"active": True, "meets_expected_min": False, "signal_count": 2},
            "transcript": {"active": True, "meets_expected_min": False, "signal_count": 0},
        },
        previous_channels={
            "kusto": {"active": True, "meets_expected_min": True, "signal_count": 5},
            "transcript": {"active": True, "meets_expected_min": True, "signal_count": 4},
        },
        current_failed_queries=["acme-deployment-p50-p90"],
        previous_query_states={},
        current_stale_queries=["acme-deployment-p50-p90"],
        current_frozen_queries=["acme-fleet-healthy-pct"],
        current_m365_discovery={"untracked_observed_thread_ids": 2, "signals_without_workstream": 3},
        previous_m365_discovery={"untracked_observed_thread_ids": 1, "signals_without_workstream": 1},
    )

    assert check.status == "warn"
    assert "Previous run completeness 100% -> current 0% (-100 points)" in check.detail
    assert "Regressed channels: kusto, transcript." in check.detail
    assert "Newly failed queries: acme-deployment-p50-p90." in check.detail
    assert "Newly stale queries: acme-deployment-p50-p90." in check.detail
    assert "Newly frozen metric queries: acme-fleet-healthy-pct." in check.detail
    assert "M365 untracked threads increased by 1." in check.detail
    assert "M365 unattributed signals increased by 2." in check.detail
    assert check.metadata is not None
    assert check.metadata["completeness_delta_pct"] == -100
    assert check.metadata["regressed_channels"] == ["kusto", "transcript"]
    assert check.metadata["channel_signal_deltas"] == {"kusto": -3, "transcript": -4}


def test_channel_delta_check_reports_improvements_without_regression() -> None:
    check = channel_delta_check(
        previous_gathered_at=datetime(2026, 5, 10, 17, 0, tzinfo=timezone.utc),
        current_channels={
            "kusto": {"active": True, "meets_expected_min": True, "signal_count": 6},
            "transcript": {"active": True, "meets_expected_min": True, "signal_count": 5},
        },
        previous_channels={
            "kusto": {"active": True, "meets_expected_min": False, "signal_count": 4},
            "transcript": {"active": True, "meets_expected_min": True, "signal_count": 5},
        },
        current_failed_queries=[],
        previous_query_states={},
        current_stale_queries=[],
        current_frozen_queries=[],
        current_m365_discovery={"untracked_observed_thread_ids": 1, "signals_without_workstream": 1},
        previous_m365_discovery={"untracked_observed_thread_ids": 1, "signals_without_workstream": 1},
    )

    assert check.status == "ok"
    assert "Previous run completeness 50% -> current 100% (+50 points)" in check.detail
    assert "Improved channels: kusto." in check.detail
    assert check.metadata is not None
    assert check.metadata["improved_channels"] == ["kusto"]
    assert check.metadata["channel_signal_deltas"] == {"kusto": 2, "transcript": 0}


def test_channel_health_snapshot_handles_empty_channels() -> None:
    active_channels, channels_at_expected_min, completeness_pct = channel_health_snapshot({})

    assert active_channels == []
    assert channels_at_expected_min == []
    assert completeness_pct == 100
