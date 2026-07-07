from __future__ import annotations

from src.commands.doctor_checks.m365_review_checks import (
    m365_discovery_check,
    m365_registry_review_check,
    summarize_m365_discovery_comparison,
)


def test_summarize_m365_discovery_comparison_reports_deltas() -> None:
    summary = summarize_m365_discovery_comparison(
        {
            "observed_thread_ids": 2,
            "untracked_observed_thread_ids": 1,
            "signals_without_workstream": 2,
            "registry_bootstrapped": True,
        },
        {
            "observed_thread_ids": 1,
            "untracked_observed_thread_ids": 0,
            "signals_without_workstream": 1,
            "registry_bootstrapped": False,
        },
    )

    assert "observed thread ids 1 -> 2" in summary
    assert "untracked threads 0 -> 1" in summary
    assert "unattributed WorkIQ signals 1 -> 2" in summary
    assert "registry bootstrap False -> True" in summary


def test_m365_discovery_check_includes_previous_run_summary() -> None:
    check = m365_discovery_check(
        {
            "active": True,
            "registry_bootstrapped": False,
            "untracked_observed_thread_ids": 1,
            "signals_without_workstream": 2,
            "chat_thread_id_null": 0,
            "promotion_blocked_missing_id_count": 0,
            "discovery_last_error": "",
            "first_discovery_completed_at": "2026-05-10T18:00:00+00:00",
            "observed_thread_ids": 2,
        },
        previous_entry={
            "registry_bootstrapped": True,
            "untracked_observed_thread_ids": 0,
            "signals_without_workstream": 1,
            "observed_thread_ids": 1,
        },
    )

    assert check.status == "warn"
    assert "registry bootstrap missing" in check.detail
    assert "Previous run: observed thread ids 1 -> 2" in check.detail


def test_m365_registry_review_check_surfaces_keyword_suggestions() -> None:
    check = m365_registry_review_check(
        {
            "medium_review_count": 1,
            "unclassified_count": 0,
            "missing_id_count": 0,
            "promotion_blocked_signal_yield_count": 0,
            "keyword_suggestions_by_workstream": {"acme": ["pilot readiness"]},
        }
    )

    assert check.status == "warn"
    assert "1 medium-confidence artifact(s) need PM review" in check.detail
    assert "keyword expansion suggestions -> acme: pilot readiness" in check.detail
