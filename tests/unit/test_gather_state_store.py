from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.core.gather_state_store import load_gather_query_states, load_gather_state, write_gather_state
from src.core.models_v2 import IntegrationError


def test_write_gather_state_emits_schema_2_with_query_map_and_preserves_aggregate_fields(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    path = write_gather_state(
        "acme",
        gathered_at=datetime(2026, 5, 17, 14, 2, 11, tzinfo=timezone.utc),
        scanned_items=4,
        discovered_signals=2,
        new_signals=1,
        pending_review=1,
        trajectory_updates=0,
        auto_reviews_written=0,
        ado_calls=3,
        archived_journal_files=0,
        background_proposals=0,
        integration_errors=1,
        integration_error_details=(
            IntegrationError(
                source="kusto",
                stage="gather",
                retryable=True,
                message="kusto unavailable",
                operator_action="Run vertex admin auth setup",
            ),
        ),
        gather_flags={"kusto": True, "workiq": False},
        channels={
            "kusto": {
                "active": True,
                "signal_count": 2,
                "expected_min": 10,
                "meets_expected_min": False,
                "uil_scope_health": {
                    "query-a": "ok",
                },
            },
            "workiq": {
                "active": False,
                "signal_count": 0,
                "expected_min": 8,
                "meets_expected_min": False,
                "reason_not_active": "flag_not_passed",
            },
        },
        m365_discovery={
            "active": True,
            "query_plan_count": 3,
            "observed_thread_ids": 2,
            "untracked_observed_thread_ids": 1,
            "signals_without_workstream": 1,
            "registry_bootstrapped": False,
        },
        previous_gathered_at=datetime(2026, 5, 16, 14, 2, 11, tzinfo=timezone.utc),
        previous_query_states={
            "acme-deployment-p50-p90": {
                "last_cycle_succeeded": False,
                "data_freshness_ok": False,
            }
        },
        previous_channels={
            "workiq": {
                "active": True,
                "signal_count": 6,
                "expected_min": 8,
                "meets_expected_min": False,
            }
        },
        previous_m365_discovery={
            "active": True,
            "observed_thread_ids": 1,
            "untracked_observed_thread_ids": 0,
            "signals_without_workstream": 0,
        },
        query_states={
            "acme-deployment-p50-p90": {
                "last_attempted_at": datetime(2026, 5, 17, 14, 2, 11, tzinfo=timezone.utc),
                "last_succeeded_at": datetime(2026, 5, 17, 14, 2, 14, tzinfo=timezone.utc),
                "row_count": 1,
                "duration_ms": 2873,
                "last_cycle_succeeded": True,
                "zero_rows_ok": True,
                "last_error": None,
                "value_last_4": [97.1, 97.1, 97.1, 97.1],
            }
        },
        programs_root=programs_root,
    )

    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["schema_version"] == "2.0"
    assert payload["integration_errors"] == 1
    assert payload["integration_error_details"][0]["source"] == "kusto"
    assert payload["gather_flags"] == {"kusto": True, "workiq": False}
    assert payload["channels"]["kusto"]["signal_count"] == 2
    assert payload["channels"]["kusto"]["uil_scope_health"] == {"query-a": "ok"}
    assert payload["channels"]["workiq"]["reason_not_active"] == "flag_not_passed"
    assert payload["m365_discovery"]["query_plan_count"] == 3
    assert payload["m365_discovery"]["untracked_observed_thread_ids"] == 1
    assert payload["previous_gathered_at"] == "2026-05-16T14:02:11+00:00"
    assert payload["previous_queries"]["acme-deployment-p50-p90"]["data_freshness_ok"] is False
    assert payload["previous_channels"]["workiq"]["signal_count"] == 6
    assert payload["previous_m365_discovery"]["observed_thread_ids"] == 1
    assert payload["queries"]["acme-deployment-p50-p90"]["last_attempted_at"] == "2026-05-17T14:02:11Z"
    assert payload["queries"]["acme-deployment-p50-p90"]["last_succeeded_at"] == "2026-05-17T14:02:14Z"
    assert payload["queries"]["acme-deployment-p50-p90"]["last_cycle_succeeded"] is True
    assert payload["queries"]["acme-deployment-p50-p90"]["value_last_4"] == [97.1, 97.1, 97.1, 97.1]

    state = load_gather_state("acme", programs_root=programs_root)

    assert state is not None
    assert state.channels["kusto"]["uil_scope_health"] == {"query-a": "ok"}


def test_load_gather_state_reads_legacy_schema_1_payload(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "gather_state.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "program_id": "acme",
                "gathered_at": "2026-05-17T14:02:11Z",
                "scanned_items": 4,
                "discovered_signals": 2,
                "new_signals": 1,
                "pending_review": 1,
                "trajectory_updates": 0,
                "auto_reviews_written": 0,
                "ado_calls": 3,
                "archived_journal_files": 0,
                "background_proposals": 0,
                "integration_errors": 1,
                "integration_error_details": [
                    {
                        "source": "kusto",
                        "stage": "gather",
                        "retryable": True,
                        "message": "kusto unavailable",
                        "operator_action": "Run vertex admin auth setup",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    state = load_gather_state("acme", programs_root=programs_root)

    assert state is not None
    assert state.program_id == "acme"
    assert state.integration_errors == 1
    assert state.integration_error_details[0].source == "kusto"
    assert state.query_states == {}
    assert state.gather_flags == {}
    assert state.channels == {}
    assert state.m365_discovery == {}
    assert state.previous_gathered_at is None
    assert state.previous_query_states == {}
    assert state.previous_channels == {}
    assert state.previous_m365_discovery == {}


def test_write_gather_state_carries_forward_previous_snapshots_when_omitted(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    write_gather_state(
        "acme",
        gathered_at=datetime(2026, 5, 16, 14, 2, 11, tzinfo=timezone.utc),
        scanned_items=4,
        discovered_signals=2,
        new_signals=1,
        pending_review=1,
        trajectory_updates=0,
        auto_reviews_written=0,
        ado_calls=3,
        archived_journal_files=0,
        background_proposals=0,
        channels={
            "ado": {
                "active": True,
                "signal_count": 5,
                "expected_min": 1,
                "meets_expected_min": True,
            }
        },
        query_states={
            "query-a": {
                "last_cycle_succeeded": True,
                "row_count": 5,
            }
        },
        m365_discovery={
            "observed_thread_ids": 2,
        },
        programs_root=programs_root,
    )

    write_gather_state(
        "acme",
        gathered_at=datetime(2026, 5, 17, 14, 2, 11, tzinfo=timezone.utc),
        scanned_items=5,
        discovered_signals=3,
        new_signals=2,
        pending_review=1,
        trajectory_updates=0,
        auto_reviews_written=0,
        ado_calls=4,
        archived_journal_files=0,
        background_proposals=0,
        channels={
            "ado": {
                "active": True,
                "signal_count": 4,
                "expected_min": 1,
                "meets_expected_min": True,
            }
        },
        query_states={
            "query-a": {
                "last_cycle_succeeded": False,
                "row_count": 0,
            }
        },
        m365_discovery={
            "observed_thread_ids": 3,
        },
        programs_root=programs_root,
    )

    state = load_gather_state("acme", programs_root=programs_root)

    assert state is not None
    assert state.previous_gathered_at == datetime(2026, 5, 16, 14, 2, 11, tzinfo=timezone.utc)
    assert state.previous_channels["ado"]["signal_count"] == 5
    assert state.previous_query_states["query-a"]["row_count"] == 5
    assert state.previous_m365_discovery["observed_thread_ids"] == 2


def test_load_gather_query_states_tolerates_queries_only_payload(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "gather_state.json").write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "queries": {
                    "query-a": {
                        "last_succeeded_at": "2026-05-17T14:02:14Z",
                        "last_cycle_succeeded": True,
                        "row_count": 1,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    query_states = load_gather_query_states("acme", programs_root=programs_root)

    assert query_states == {
        "query-a": {
            "last_succeeded_at": "2026-05-17T14:02:14Z",
            "last_cycle_succeeded": True,
            "row_count": 1,
        }
    }
