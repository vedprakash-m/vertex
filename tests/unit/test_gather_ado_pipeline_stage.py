"""Direct coverage for the extracted ADO pipeline/PR gather stage."""

from __future__ import annotations

from datetime import datetime, timezone

from src.commands.gather_pipeline import ado_pipeline_stage


def test_summarize_pull_requests_returns_none_when_no_active_prs() -> None:
    assert (
        ado_pipeline_stage.summarize_pull_requests(
            repository_id="repo-1",
            pull_requests=(
                {
                    "pullRequestId": 301,
                    "title": "Closed change",
                    "status": "completed",
                    "creationDate": "2026-05-02T08:00:00Z",
                },
            ),
            as_of=datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc),
        )
        is None
    )


def test_summarize_pull_requests_builds_refs_and_p90_age() -> None:
    summary = ado_pipeline_stage.summarize_pull_requests(
        repository_id="repo-42",
        pull_requests=(
            {
                "pullRequestId": 301,
                "title": "Stabilize rollout for WI:12345",
                "status": "active",
                "creationDate": "2026-05-02T08:00:00Z",
                "isDraft": False,
                "repository": {"id": "repo-42", "name": "XStoreApp"},
            },
            {
                "pullRequestId": 302,
                "title": "Tune validation gates",
                "status": "active",
                "creationDate": "2026-05-08T08:00:00Z",
                "isDraft": True,
                "repository": {"id": "repo-42", "name": "XStoreApp"},
            },
        ),
        as_of=datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc),
    )

    assert summary is not None
    assert "repo XStoreApp has 2 open PRs; P90 age 10.0d; oldest #301 10.0d" == summary["text"]
    assert summary["entity_refs"] == ("PR:XStoreApp/301", "WI:12345", "PR:XStoreApp/302")
    assert summary["metadata"]["draft_pr_count"] == 1
    assert summary["metadata"]["p90_age_days"] == 10.0


def test_record_ado_pipeline_query_state_values_sets_frozen_warning() -> None:
    query_state_sink: dict[str, dict[str, object]] = {}

    ado_pipeline_stage._record_ado_pipeline_query_state_values(
        query_state_sink,
        source="ado/pr",
        workstream_id="deployment_readiness",
        metadata={"window_days": 14},
        as_of=datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc),
        previous_state={"value_last_4": [2.0, 2.0, 2.0]},
        row_count=2,
        numeric_value=2.0,
        value_metric="open_pr_count",
        extra_fields={"open_pr_count": 2},
        zero_rows_ok=False,
        max_data_timestamp=datetime(2026, 5, 12, 7, 0, tzinfo=timezone.utc),
    )

    state = query_state_sink["ado-pr:deployment_readiness"]
    assert state["value_last_4"] == [2.0, 2.0, 2.0, 2.0]
    assert state["value_frozen_warning"] is True
    assert state["data_freshness_ok"] is True


def test_record_ado_pipeline_query_state_values_suppresses_zero_frozen_warning() -> None:
    query_state_sink: dict[str, dict[str, object]] = {}

    ado_pipeline_stage._record_ado_pipeline_query_state_values(
        query_state_sink,
        source="ado/pipeline",
        workstream_id="deployment_readiness",
        metadata={"window_days": 14},
        as_of=datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc),
        previous_state={"value_last_4": [0.0, 0.0, 0.0]},
        row_count=2,
        numeric_value=0.0,
        value_metric="failed_run_count",
        extra_fields={"failed_run_count": 0},
        zero_rows_ok=True,
        max_data_timestamp=datetime(2026, 5, 12, 7, 0, tzinfo=timezone.utc),
        suppress_zero_frozen_warning=True,
    )

    state = query_state_sink["ado-pipeline:deployment_readiness"]
    assert state["value_last_4"] == [0.0, 0.0, 0.0, 0.0]
    assert state["value_frozen_warning"] is False
