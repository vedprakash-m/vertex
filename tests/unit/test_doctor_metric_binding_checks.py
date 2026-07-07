from __future__ import annotations

from types import SimpleNamespace

from src.commands.doctor_checks.metric_binding_checks import build_metric_rollout_doctor_check


def test_build_metric_rollout_doctor_check_warns_for_missing_queries() -> None:
    eligible = (
        SimpleNamespace(query_id="q1"),
        SimpleNamespace(query_id="q2"),
        SimpleNamespace(query_id="q3"),
        SimpleNamespace(query_id="q4"),
    )
    missing = eligible[1:]

    check = build_metric_rollout_doctor_check(eligible, missing)

    assert check.label == "Metric Rollout"
    assert check.status == "warn"
    assert "3 eligible KPI rollout(s)" in check.detail
    assert "q2, q3, q4" in check.detail
    assert check.metadata == {
        "eligible_query_ids": ["q1", "q2", "q3", "q4"],
        "missing_query_ids": ["q2", "q3", "q4"],
    }
