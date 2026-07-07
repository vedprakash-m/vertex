from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from src.commands.gather_pipeline.m365_discovery_stage import (
    build_m365_discovery_state,
    run_m365_discovery_stage,
)
from src.commands.gather_pipeline.models import M365DiscoveryStageInput
from src.core.m365_registry_store import M365RegistryArtifact
from src.core.models import RiskLevel, WorkItem
from src.core.models_v2 import ADOConfig, M365Config, Program, Workstream, WorkstreamSignalSources, TeamsChat


def _demo_program() -> Program:
    return Program(
        schema_version="2.0",
        id="demo",
        name="Demo",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Demo",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(enabled=True, prefer_agency=True, workiq_queries={}),
    )


def _demo_workstream() -> Workstream:
    return Workstream(
        id="demo",
        name="Demo",
        signal_sources=WorkstreamSignalSources(
            teams_chats=(TeamsChat(display_name="Demo Chat", thread_id=None),),
        ),
    )


def _demo_item(current_time: datetime) -> WorkItem:
    return WorkItem(
        id=101,
        type="Feature",
        title="Demo",
        state="Active",
        assigned_to="Owner",
        assigned_to_email="owner@example.com",
        area_path="One\\Demo",
        iteration_path="One\\Iteration",
        target_date=None,
        risk_level=RiskLevel.UNKNOWN,
        tags=[],
        custom_fields={},
        fetched_at=current_time,
    )


def test_build_m365_discovery_state_uses_seeded_attempt_timestamp_for_first_completion(tmp_path: Path) -> None:
    attempted_at = "2026-05-10T08:15:00+00:00"

    state = build_m365_discovery_state(
        program_id="demo",
        programs_root=tmp_path,
        program=_demo_program(),
        workstreams=(_demo_workstream(),),
        items=(),
        workiq_signals=(),
        gather_flags={"workiq": True},
        integration_error_details=(),
        registry_artifacts=(),
        feedback_events=(),
        as_of=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
        previous_entry=None,
        count_transcript_series_state=lambda workstreams: (0, 0),
        count_chat_thread_state=lambda workstreams: (1, 1),
        tracked_m365_artifact_ids=lambda workstreams, registry_artifacts, **kwargs: set(),
        observed_m365_thread_ids=lambda signals: set(),
        load_discovery_milestones=lambda program_id, programs_root: (),
        build_workiq_query_plans=lambda **kwargs: (),
        build_m365_discovery_queries=lambda **kwargs: (),
        build_seeded_source_discovery_state=lambda **kwargs: {
            "intent_count": 1,
            "attempt_count": 1,
            "attempted_intent_count": 1,
            "candidate_count": 0,
            "pending_candidate_count": 0,
            "first_attempted_at": attempted_at,
            "latest_attempted_at": attempted_at,
            "outcome_counts": {"no_candidates": 1},
            "latest_attempts": [],
        },
        build_adaptive_workiq_state=lambda **kwargs: {"workstreams": {}},
    )

    assert state["first_discovery_completed_at"] == attempted_at
    assert state["seeded_resolution_attempt_count"] == 1
    assert state["seeded_resolution_attempted_intent_count"] == 1


def test_build_m365_discovery_state_includes_adaptive_learning_snapshot(tmp_path: Path) -> None:
    adaptive_state = {
        "workstreams": {
            "demo": {
                "effective_keywords": ["northwind"],
                "top_sources": [{"ref_id": "mail-thread-1"}],
            }
        }
    }

    state = build_m365_discovery_state(
        program_id="demo",
        programs_root=tmp_path,
        program=_demo_program(),
        workstreams=(_demo_workstream(),),
        items=(_demo_item(datetime(2026, 6, 3, 9, 0, tzinfo=timezone.utc)),),
        workiq_signals=(),
        gather_flags={"workiq": True},
        integration_error_details=(),
        registry_artifacts=(),
        feedback_events=(),
        as_of=datetime(2026, 6, 3, 9, 0, tzinfo=timezone.utc),
        previous_entry=None,
        count_transcript_series_state=lambda workstreams: (0, 0),
        count_chat_thread_state=lambda workstreams: (1, 1),
        tracked_m365_artifact_ids=lambda workstreams, registry_artifacts, **kwargs: set(),
        observed_m365_thread_ids=lambda signals: set(),
        load_discovery_milestones=lambda program_id, programs_root: (),
        build_workiq_query_plans=lambda **kwargs: (),
        build_m365_discovery_queries=lambda **kwargs: (),
        build_seeded_source_discovery_state=lambda **kwargs: {
            "intent_count": 0,
            "attempt_count": 0,
            "attempted_intent_count": 0,
            "candidate_count": 0,
            "pending_candidate_count": 0,
            "first_attempted_at": None,
            "latest_attempted_at": None,
            "outcome_counts": {},
            "latest_attempts": [],
        },
        build_adaptive_workiq_state=lambda **kwargs: adaptive_state,
    )

    assert state["adaptive_learning"] == adaptive_state


def test_run_m365_discovery_stage_returns_promotion_blockers_from_registry(monkeypatch, tmp_path: Path) -> None:
    current_time = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    blocked_artifact = M365RegistryArtifact(
        artifact_id="meet:demo-weekly",
        artifact_type="meeting_series",
        inferred_workstream="demo",
        confidence=1.0,
        confidence_source="pm_confirmed",
        pm_confirmed=True,
        promoted_to_workstreams_yaml=False,
        first_seen=current_time.date(),
        last_seen=current_time.date(),
        display_name="Demo Weekly",
        series_id=None,
    )
    monkeypatch.setattr(
        "src.commands.gather_pipeline.m365_discovery_stage.load_m365_registry",
        lambda program_id, programs_root: SimpleNamespace(artifacts=(blocked_artifact,)),
    )
    monkeypatch.setattr(
        "src.commands.gather_pipeline.m365_discovery_stage.read_m365_routing_feedback_events",
        lambda program_id, programs_root: (),
    )

    result = run_m365_discovery_stage(
        M365DiscoveryStageInput(
            program=_demo_program(),
            program_id="demo",
            workstreams=(_demo_workstream(),),
            items=(),
            workiq_signals=(),
            gather_flags={"workiq": True},
            integration_error_details=(),
            as_of=current_time,
            previous_entry=None,
            programs_root=tmp_path,
            count_transcript_series_state=lambda workstreams: (0, 0),
            count_chat_thread_state=lambda workstreams: (1, 1),
            tracked_m365_artifact_ids=lambda workstreams, registry_artifacts, **kwargs: set(),
            observed_m365_thread_ids=lambda signals: set(),
            load_discovery_milestones=lambda program_id, programs_root: (),
            build_workiq_query_plans=lambda **kwargs: (),
            build_m365_discovery_queries=lambda **kwargs: (),
            build_seeded_source_discovery_state=lambda **kwargs: {
                "intent_count": 0,
                "attempt_count": 0,
                "attempted_intent_count": 0,
                "candidate_count": 0,
                "pending_candidate_count": 0,
                "first_attempted_at": None,
                "latest_attempted_at": None,
                "outcome_counts": {},
                "latest_attempts": [],
            },
            build_adaptive_workiq_state=lambda **kwargs: {"workstreams": {}},
        )
    )

    assert result.promotion_candidates == ()
    assert result.promotion_blocked_artifacts[0].artifact_id == "meet:demo-weekly"
    assert result.m365_discovery_state["promotion_blocked_missing_id_count"] == 1
