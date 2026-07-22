from __future__ import annotations

import hashlib
import json
import pytest
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from cli import app
from src.commands import gather
from src.commands.gather_pipeline.models import GatherArtifacts
from src.commands.gather_pipeline import hypothesis_stage
from src.commands.gather_pipeline import ado_pipeline_stage
from src.core.discovery_intent import (
    DiscoveryAttempt,
    DiscoveryAttemptOutcome,
    SourceCandidate,
    SourceCandidateStatus,
    SourceIntentStatus,
    SourceRefKind,
    build_discovery_attempt_id,
    build_source_candidate_id,
)
from src.core.gather_state_store import load_gather_state, write_gather_state
from src.core.keyword_topic_router import M365RoutingDecision
from src.core.m365_registry_store import M365RegistryArtifact, M365RoutingFeedbackEvent, apply_m365_routing_feedback, build_auto_thread_artifact_id, ensure_m365_registry_bootstrap, load_m365_registry, upsert_m365_registry_artifacts
from src.core.action_tracker import load_actions
from src.core.analytics_store import load_contradiction_state
from src.core.claim_tracker import append_claim_entry
from src.core.decision_register import load_decisions
from src.core.exceptions import QueryError
from src.core.hypothesis_models import HypothesisKind, HypothesisStatus
from src.core.incident_journal_store import read_incident_entries
from src.core.knowledge_store import KnowledgeStore
from src.core.leakage_detector import LeakageReport
from src.core.journal import append_review_decision, append_signal, read_review_log, read_signals
from src.core.models import Comment, Confidence, Revision, RiskLevel, WorkItem
from src.core.metric_models import MetricSourceBinding
from src.core.models_v2 import ADOConfig, AIConfig, ActionItem, ActionSourceType, ActionStatus, ClaimEntry, DecisionEntry, DecisionStatus, DependencyADOQuery, EmailThreadSource, KustoConfig, KustoQuery, M365Config, Milestone, MilestoneStatus, Program, ReviewPolicy, Signal, SignalClass, SignalReviewDecision, Team, TeamsChat, TeamsMeetingSeries, TrajectoryPoint, VitalityAggregate, VitalityScore, WorkIQRetrievalConfig, Workstream, WorkstreamSignalSources
from src.core.models_v2 import IntegrationError
from src.core.reality_store import RealityStore
from src.core.sqlite_stores import SQLiteSignalStore, SQLiteTrajectoryStore
from src.core.source_candidate_store import SourceCandidateStore, candidate_evidence_json
from src.core.source_models import SourceKind
from src.core.trajectory import read_trajectory
from src.m365.agency_bridge import AgencyBridge, AgencyCapabilities


runner = CliRunner()


def test_semantic_gather_exit_codes_distinguish_optional_and_required_degradation() -> None:
    from src.core.integration_types import DiscoveryQueryResult

    base = GatherArtifacts("acme", 0, 0, 0, 0, 0, 0, 0)
    optional = replace(base, integration_errors=(IntegrationError(source="kusto", stage="hydration", message="timeout", retryable=True),))
    required = replace(base, integration_errors=(IntegrationError(source="ado", stage="discovery", message="cap reached", retryable=False),))
    required_new_stage = replace(base, integration_errors=(IntegrationError(source="ado", stage="new-stage", message="failed", retryable=False),))

    assert gather._semantic_gather_exit_code(base) == 0
    assert gather._semantic_gather_exit_code(optional) == 2
    assert gather._semantic_gather_exit_code(required) == 3
    assert gather._has_required_ado_degradation(required) is True
    assert gather._semantic_gather_exit_code(required_new_stage) == 3

    capture_time = datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc)
    skewed = replace(
        base,
        ado_query_results=(
            DiscoveryQueryResult("q1", "s1", "a" * 64, capture_time, 0, (), "b" * 64, False, "FULL"),
            DiscoveryQueryResult("q2", "s2", "c" * 64, capture_time + timedelta(seconds=301), 0, (), "d" * 64, False, "FULL"),
        ),
    )
    assert gather._has_required_scope_degradation(skewed) is True
    assert gather._semantic_gather_exit_code(skewed) == 3

    capped = replace(
        base,
        ado_query_results=(
            DiscoveryQueryResult("q1", "s1", "a" * 64, capture_time, 10_000, (), "b" * 64, True, "PARTIAL"),
        ),
    )
    assert gather._has_required_scope_degradation(capped) is True
    assert gather._semantic_gather_exit_code(capped) == 3


def test_gather_command_maps_lease_conflict_to_scheduler_exit_four(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _conflict(*_args, **_kwargs):
        raise gather.GatherLeaseConflict("another gather owns the lease")

    monkeypatch.setattr(gather, "gather_program", _conflict)

    result = runner.invoke(app, ["gather", "--program", "acme"])

    assert result.exit_code == 4
    assert "another gather owns the lease" in result.output


def test_gather_command_maps_fencing_loss_to_scheduler_exit_four(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core.workspace_lease import LeaseFencingTokenStale

    def _fenced_out(*_args, **_kwargs):
        raise LeaseFencingTokenStale(presented=4, current=5)

    monkeypatch.setattr(gather, "gather_program", _fenced_out)

    result = runner.invoke(app, ["gather", "--program", "acme"])

    assert result.exit_code == 4
    assert "stale fencing token 4" in result.output


def _read_ingestion_run_rows(program_id: str, *, db_root: Path) -> list[tuple[str, str, int]]:
    store = RealityStore(program_id, db_root=db_root)
    store.initialize()
    with sqlite3.connect(store.db_path) as connection:
        rows = connection.execute(
            "SELECT source_ref, status, signals_written FROM reality_ingestion_runs ORDER BY rowid ASC"
        ).fetchall()
    latest_by_source: dict[str, tuple[str, str, int]] = {}
    for source_ref, status, signals_written in rows:
        latest_by_source[str(source_ref)] = (str(source_ref), str(status), int(signals_written))
    return [latest_by_source[source_ref] for source_ref in sorted(latest_by_source)]


def _read_ingestion_run_details(program_id: str, *, db_root: Path) -> list[tuple[str, str, int, str | None, str | None]]:
    store = RealityStore(program_id, db_root=db_root)
    store.initialize()
    with sqlite3.connect(store.db_path) as connection:
        rows = connection.execute(
            "SELECT source_ref, status, signals_written, query_hash, captured_window FROM reality_ingestion_runs ORDER BY rowid ASC"
        ).fetchall()
    latest_by_source: dict[str, tuple[str, str, int, str | None, str | None]] = {}
    for source_ref, status, signals_written, query_hash, captured_window in rows:
        latest_by_source[str(source_ref)] = (
            str(source_ref),
            str(status),
            int(signals_written),
            str(query_hash) if query_hash is not None else None,
            str(captured_window) if captured_window is not None else None,
        )
    return [latest_by_source[source_ref] for source_ref in sorted(latest_by_source)]


def test_gather_command_is_idempotent_for_repeated_ado_runs(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    db_root = tmp_path / "vertex-db"
    monkeypatch.setattr(gather, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setenv("VERTEX_DB_PATH", str(db_root))

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
            signal_sources=WorkstreamSignalSources(
                teams_meeting_series=(
                    TeamsMeetingSeries(display_name="Acme Weekly Ops Review", series_id=None),
                    TeamsMeetingSeries(display_name="Adventure Ramp Weekly Sync", series_id="meeting-series-1"),
                ),
                teams_chats=(
                    TeamsChat(display_name="Acme Eng Core Chat", thread_id=None),
                    TeamsChat(display_name="Acme Incident Triage", thread_id="configured-chat-1"),
                ),
            ),
        ),
    )

    revision = Revision(
        work_item_id=1234,
        rev_number=7,
        changed_by="priya@example.com",
        changed_by_email="priya@example.com",
        changed_date=datetime(2026, 5, 8, 9, 0, tzinfo=timezone.utc),
        fields_changed={
            "Microsoft.VSTS.Scheduling.TargetDate": ("2026-05-10", "2026-05-17"),
            "System.State": ("Proposed", "Active"),
        },
    )
    item = WorkItem(
        id=1234,
        type="Feature",
        title="Ramp checkpoint",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="One\\FY26\\Q4",
        target_date=date(2026, 5, 17),
        risk_level=RiskLevel.MEDIUM,
        tags=["acme"],
        custom_fields={},
        revisions=[revision],
        comments=[],
        fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_resolve_uil_channel_binding_for_gather", lambda *_a, **_k: object())
    monkeypatch.setattr(gather, "_load_ado_items_via_uil", lambda _p, _ws, _ao, **__: ((item,), (item,), 0))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    first = runner.invoke(app, ["gather", "--program", "acme"])
    second = runner.invoke(app, ["gather", "--program", "acme"])

    signals = read_signals("acme", programs_root=programs_root)
    reviews = read_review_log("acme", programs_root=programs_root)
    trajectory = read_trajectory("acme", 1234, programs_root=programs_root)

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert "[1/7] prepare" in first.output
    assert "[7/7] finalize" in first.output
    assert "pending review) for acme" in first.output
    assert "[1/7] prepare" in second.output
    assert "[7/7] finalize" in second.output
    assert "0 new, 0 pending review" in second.output
    assert len(signals) == 2
    assert len(reviews) == 2
    assert len(trajectory) == 1
    assert _read_ingestion_run_rows("acme", db_root=db_root) == [
        ("ado/comment", "success", 0),
        ("ado/dependency", "success", 0),
        ("ado/revision", "success", 0),
        ("vertex/freshness", "success", 0),
    ]


def test_record_optional_source_ingestion_runs_counts_ado_revision_signals(monkeypatch, tmp_path: Path) -> None:
    db_root = tmp_path / "vertex-db"
    monkeypatch.setenv("VERTEX_DB_PATH", str(db_root))

    as_of = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    gather._record_optional_source_ingestion_runs(
        "acme",
        as_of=as_of,
        programs_root=tmp_path / "programs",
        include_workiq=False,
        include_analytics=False,
        include_sprints=False,
        include_pipelines=False,
        include_icm=False,
        signals=(
            Signal(
                id="rev-1",
                timestamp=datetime(2026, 5, 10, 7, 0, tzinfo=timezone.utc),
                source="ado/revision",
                program_id="acme",
                workstream_id="acme",
                entity_refs=("WI:1",),
                text="Revision 1",
                raw_ref="wi:1:rev:1",
                confidence=Confidence.HIGH,
            ),
            Signal(
                id="comment-1",
                timestamp=datetime(2026, 5, 10, 7, 30, tzinfo=timezone.utc),
                source="ado/comment",
                program_id="acme",
                workstream_id="acme",
                entity_refs=("WI:1",),
                text="Comment 1",
                raw_ref="wi:1:comment:1",
                confidence=Confidence.HIGH,
            ),
            Signal(
                id="fresh-1",
                timestamp=datetime(2026, 5, 10, 7, 45, tzinfo=timezone.utc),
                source="vertex/freshness",
                program_id="acme",
                workstream_id="acme",
                entity_refs=("WI:1",),
                text="Freshness 1",
                raw_ref="wi:1:freshness",
                confidence=Confidence.HIGH,
            ),
            Signal(
                id="dep-1",
                timestamp=datetime(2026, 5, 10, 7, 50, tzinfo=timezone.utc),
                source="ado/dependency",
                program_id="acme",
                workstream_id="acme",
                entity_refs=("WI:2",),
                text="Dependency 1",
                raw_ref="dependency:onedepoy:2",
                confidence=Confidence.HIGH,
            ),
        ),
        integration_error_details=(),
    )

    assert _read_ingestion_run_details("acme", db_root=db_root) == [
        (
            "ado/comment",
            "success",
            1,
            None,
            "2026-05-10T07:30:00+00:00/2026-05-10T07:30:00+00:00",
        ),
        (
            "ado/dependency",
            "success",
            1,
            None,
            "2026-05-10T07:50:00+00:00/2026-05-10T07:50:00+00:00",
        ),
        (
            "ado/revision",
            "success",
            1,
            None,
            "2026-05-10T07:00:00+00:00/2026-05-10T07:00:00+00:00",
        ),
        (
            "vertex/freshness",
            "success",
            1,
            None,
            "2026-05-10T07:45:00+00:00/2026-05-10T07:45:00+00:00",
        ),
    ]
    
def test_gather_command_surfaces_m365_promotion_candidates(monkeypatch) -> None:
    monkeypatch.setattr(
        gather,
        "gather_program",
        lambda *args, **kwargs: gather.GatherArtifacts(
            program_id="acme",
            scanned_items=0,
            discovered_signals=0,
            new_signals=0,
            pending_review=0,
            trajectory_updates=0,
            auto_reviews_written=0,
            ado_calls=0,
            promotion_candidates=(
                gather.M365PromotionCandidate(
                    artifact_id="chan:acme-promote-ready",
                    display_name="Promotion Ready Chat",
                    workstream_id="acme",
                    confidence=1.0,
                    signal_yield_last_3=(1, 1, 1),
                ),
            ),
        ),
    )

    result = runner.invoke(app, ["gather", "--program", "acme"])

    assert result.exit_code == 0
    assert "[PROMOTION CANDIDATE] artifact 'chan:acme-promote-ready' (Promotion Ready Chat) is ready for current promotion." in result.output
    assert "workstream: acme, confidence: 1.00, yield: [1, 1, 1]" in result.output
    assert "Run 'vertex registry promote chan:acme-promote-ready --program acme' to add to workstreams.yaml." in result.output


def test_gather_command_surfaces_m365_promotion_blockers(monkeypatch) -> None:
    monkeypatch.setattr(
        gather,
        "gather_program",
        lambda *args, **kwargs: gather.GatherArtifacts(
            program_id="acme",
            scanned_items=0,
            discovered_signals=0,
            new_signals=0,
            pending_review=0,
            trajectory_updates=0,
            auto_reviews_written=0,
            ado_calls=0,
            promotion_blocked_artifacts=(
                gather.M365PromotionBlockedArtifact(
                    artifact_id="meet:acme-null-series",
                    artifact_type="meeting_series",
                    display_name="Confirmed missing series",
                    workstream_id="acme",
                    blocker_reason="missing series_id/thread_id",
                ),
            ),
        ),
    )

    result = runner.invoke(app, ["gather", "--program", "acme"])

    assert result.exit_code == 0
    assert "[PROMOTION BLOCKED] artifact 'meet:acme-null-series' (Confirmed missing series) is not ready for current promotion." in result.output
    assert "workstream: acme, blocker: missing series_id/thread_id" in result.output


def test_gather_command_daily_cadence_enables_reduced_profile(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_gather_program(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return gather.GatherArtifacts(
            program_id="acme",
            scanned_items=0,
            discovered_signals=0,
            new_signals=0,
            pending_review=0,
            trajectory_updates=0,
            auto_reviews_written=0,
            ado_calls=0,
        )

    monkeypatch.setattr(gather, "gather_program", _fake_gather_program)

    result = runner.invoke(app, ["gather", "--program", "acme", "--cadence", "daily"])

    assert result.exit_code == 0
    assert captured["args"] == ("acme",)
    assert captured["kwargs"] == {
        "include_workiq": False,
        "include_kusto": False,
        "probe_kusto": False,
        "include_analytics": False,
        "include_sprints": False,
        "include_pipelines": False,
        "include_icm": True,
        "include_dependency_scout": False,
        "include_engms": False,
        "extract_evidence": False,
        "include_sharepoint": False,
        "include_lt_deck": False,
        "force_refresh": False,
        "force_discovery": False,
        "accept_shrinkage": False,
        "source_export_counts": {},
        "progress_callback": captured["kwargs"]["progress_callback"],
    }


def test_gather_command_weekly_cadence_enables_full_profile(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_gather_program(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return gather.GatherArtifacts(
            program_id="acme",
            scanned_items=0,
            discovered_signals=0,
            new_signals=0,
            pending_review=0,
            trajectory_updates=0,
            auto_reviews_written=0,
            ado_calls=0,
        )

    monkeypatch.setattr(gather, "gather_program", _fake_gather_program)

    result = runner.invoke(app, ["gather", "--program", "acme", "--cadence", "weekly"])

    assert result.exit_code == 0
    assert captured["args"] == ("acme",)
    assert captured["kwargs"] == {
        "include_workiq": True,
        "include_kusto": True,
        "probe_kusto": False,
        "include_analytics": True,
        "include_sprints": False,
        "include_pipelines": False,
        "include_icm": True,
        "include_dependency_scout": False,
        "include_engms": False,
        "extract_evidence": False,
        "include_sharepoint": True,
        "include_lt_deck": False,
        "force_refresh": False,
        "force_discovery": False,
        "accept_shrinkage": False,
        "source_export_counts": {},
        "progress_callback": captured["kwargs"]["progress_callback"],
    }


def test_gather_command_parses_repeated_source_export_options(monkeypatch) -> None:
    """D-19/AG-2.12: `--source-export scope=count` (repeatable) parses into a
    ``{scope_id: count}`` map threaded straight into ``gather_program``."""
    captured: dict[str, object] = {}

    def _fake_gather_program(*args, **kwargs):
        captured["kwargs"] = kwargs
        return gather.GatherArtifacts(
            program_id="acme",
            scanned_items=0,
            discovered_signals=0,
            new_signals=0,
            pending_review=0,
            trajectory_updates=0,
            auto_reviews_written=0,
            ado_calls=0,
        )

    monkeypatch.setattr(gather, "gather_program", _fake_gather_program)

    result = runner.invoke(
        app,
        [
            "gather",
            "--program",
            "acme",
            "--source-export",
            "scope-a=10",
            "--source-export",
            "scope-b=25",
        ],
    )

    assert result.exit_code == 0
    assert captured["kwargs"]["source_export_counts"] == {"scope-a": 10, "scope-b": 25}


def test_gather_command_rejects_malformed_source_export_option(monkeypatch) -> None:
    result = runner.invoke(app, ["gather", "--program", "acme", "--source-export", "scope-a"])

    assert result.exit_code != 0
    assert "Invalid --source-export value" in result.output


def test_gather_command_rejects_non_integer_source_export_count(monkeypatch) -> None:
    result = runner.invoke(app, ["gather", "--program", "acme", "--source-export", "scope-a=not-a-number"])

    assert result.exit_code != 0
    assert "is not an integer" in result.output


def test_gather_command_verbose_writes_trace_file(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        gather,
        "get_command_trace_path",
        lambda scope_id, command_name, **kwargs: tmp_path / scope_id / "observability" / f"{command_name}.trace.jsonl",
    )

    def _fake_gather_program(*args, **kwargs):
        captured["kwargs"] = kwargs
        progress_callback = kwargs["progress_callback"]
        progress_callback(
            gather.GatherProgressEvent(
                step_index=1,
                step_count=6,
                step_name="prepare",
                elapsed_seconds=0.12,
                detail="workstreams=1, archived=0",
            )
        )
        return gather.GatherArtifacts(
            program_id="acme",
            scanned_items=1,
            discovered_signals=1,
            new_signals=1,
            pending_review=1,
            trajectory_updates=0,
            auto_reviews_written=0,
            ado_calls=1,
        )

    monkeypatch.setattr(gather, "gather_program", _fake_gather_program)

    result = runner.invoke(app, ["gather", "--program", "acme", "--verbose"])

    assert result.exit_code == 0
    trace_path = tmp_path / "acme" / "observability" / "gather.trace.jsonl"
    assert trace_path.exists()
    trace_lines = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(entry.get("stage") == "prepare" for entry in trace_lines)
    assert f"Trace: {trace_path}" in result.output


def test_gather_command_explicit_flags_extend_cadence_profile(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_gather_program(*args, **kwargs):
        captured["kwargs"] = kwargs
        return gather.GatherArtifacts(
            program_id="acme",
            scanned_items=0,
            discovered_signals=0,
            new_signals=0,
            pending_review=0,
            trajectory_updates=0,
            auto_reviews_written=0,
            ado_calls=0,
        )

    monkeypatch.setattr(gather, "gather_program", _fake_gather_program)

    result = runner.invoke(app, ["gather", "--program", "acme", "--cadence", "daily", "--analytics", "--pipelines"])

    assert result.exit_code == 0
    assert captured["kwargs"]["include_icm"] is True
    assert captured["kwargs"]["include_analytics"] is True
    assert captured["kwargs"]["include_pipelines"] is True
    assert captured["kwargs"]["include_workiq"] is False


def test_gather_command_rejects_unknown_cadence(monkeypatch) -> None:
    monkeypatch.setattr(
        gather,
        "gather_program",
        lambda *args, **kwargs: gather.GatherArtifacts(
            program_id="acme",
            scanned_items=0,
            discovered_signals=0,
            new_signals=0,
            pending_review=0,
            trajectory_updates=0,
            auto_reviews_written=0,
            ado_calls=0,
        ),
    )

    result = runner.invoke(app, ["gather", "--program", "acme", "--cadence", "monthly"])

    assert result.exit_code != 0
    assert "Unsupported cadence 'monthly'" in result.output


def test_gather_command_can_refresh_dependency_scout(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr(gather, "PROGRAMS_ROOT", programs_root)

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
            signal_sources=WorkstreamSignalSources(
                teams_meeting_series=(
                    TeamsMeetingSeries(display_name="Acme Weekly Ops Review", series_id=None),
                    TeamsMeetingSeries(display_name="Adventure Ramp Weekly Sync", series_id="meeting-series-1"),
                ),
                teams_chats=(
                    TeamsChat(display_name="Acme Eng Core Chat", thread_id=None),
                    TeamsChat(display_name="Acme Incident Triage", thread_id="configured-chat-1"),
                ),
            ),
        ),
    )
    item = WorkItem(
        id=1234,
        type="Feature",
        title="Ramp checkpoint",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="One\\FY26\\Q4",
        target_date=date(2026, 5, 17),
        risk_level=RiskLevel.MEDIUM,
        tags=["acme"],
        custom_fields={},
        revisions=(),
        comments=[],
        fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_resolve_uil_channel_binding_for_gather", lambda *_a, **_k: object())
    monkeypatch.setattr(gather, "_load_ado_items_via_uil", lambda _p, _ws, _ao, **__: ((item,), (item,), 0))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(
        "src.commands.gather_pipeline.projection_stage.refresh_dependency_scout_state",
        lambda program_id, **kwargs: 2,
    )

    result = runner.invoke(app, ["gather", "--program", "acme", "--dependency-scout"])

    assert result.exit_code == 0
    assert "[8/8] finalize" in result.output
    assert "Dependency scout refreshed 2 proposal(s)." in result.output


def test_gather_command_uses_sqlite_stores_when_program_configured(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr(gather, "PROGRAMS_ROOT", programs_root)

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        storage_backend="sqlite",
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
            signal_sources=WorkstreamSignalSources(
                teams_meeting_series=(
                    TeamsMeetingSeries(display_name="Acme Weekly Ops Review", series_id=None),
                    TeamsMeetingSeries(display_name="Adventure Ramp Weekly Sync", series_id="meeting-series-1"),
                ),
                teams_chats=(
                    TeamsChat(display_name="Acme Eng Core Chat", thread_id=None),
                    TeamsChat(display_name="Acme Incident Triage", thread_id="configured-chat-1"),
                ),
            ),
        ),
    )

    revision = Revision(
        work_item_id=1234,
        rev_number=7,
        changed_by="priya@example.com",
        changed_by_email="priya@example.com",
        changed_date=datetime(2026, 5, 8, 9, 0, tzinfo=timezone.utc),
        fields_changed={
            "Microsoft.VSTS.Scheduling.TargetDate": ("2026-05-10", "2026-05-17"),
            "System.State": ("Proposed", "Active"),
        },
    )
    item = WorkItem(
        id=1234,
        type="Feature",
        title="Ramp checkpoint",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="One\\FY26\\Q4",
        target_date=date(2026, 5, 17),
        risk_level=RiskLevel.MEDIUM,
        tags=["acme"],
        custom_fields={},
        revisions=[revision],
        comments=[],
        fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_resolve_uil_channel_binding_for_gather", lambda *_a, **_k: object())
    monkeypatch.setattr(gather, "_load_ado_items_via_uil", lambda _p, _ws, _ao, **__: ((item,), (item,), 0))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    result = runner.invoke(app, ["gather", "--program", "acme"])

    signal_store = SQLiteSignalStore(programs_root=programs_root)
    trajectory_store = SQLiteTrajectoryStore(programs_root=programs_root)

    assert result.exit_code == 0
    assert "Gathered 2 signals (2 new, 0 pending review) for acme" in result.output
    assert len(signal_store.read("acme")) == 2
    assert len(signal_store.read_reviews("acme")) == 2
    assert len(trajectory_store.read("acme", 1234)) == 1
    assert read_signals("acme", programs_root=programs_root) == ()
    assert read_trajectory("acme", 1234, programs_root=programs_root) == ()


def test_is_echo_chamber_revision_uses_legacy_service_identity_aliases(monkeypatch) -> None:
    monkeypatch.delenv("VERTEX_SERVICE_IDENTITY", raising=False)
    monkeypatch.delenv("VERTEX_SERVICE_IDENTITIES", raising=False)
    monkeypatch.setenv("VERTEX_SERVICE_IDENTITY", "vertex-bot@example.com")

    revision = Revision(
        work_item_id=1234,
        rev_number=7,
        changed_by="Vertex Bot",
        changed_by_email="vertex-bot@example.com",
        changed_date=datetime(2026, 5, 8, 9, 0, tzinfo=timezone.utc),
        fields_changed={"System.State": ("Proposed", "Active")},
    )

    assert gather._is_echo_chamber_revision(revision, gather._vertex_service_identities()) is True


def test_gather_program_auto_archives_stale_weekly_journal_files(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    archived_timestamp = datetime(2025, 5, 5, 12, 0, tzinfo=timezone.utc)
    retained_timestamp = datetime(2025, 5, 12, 12, 0, tzinfo=timezone.utc)

    append_signal(
        Signal(
            id="sig-archived",
            timestamp=archived_timestamp,
            source="manual",
            program_id="acme",
            workstream_id=None,
            entity_refs=(),
            text="Archive this weekly partition.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
        ),
        programs_root=programs_root,
        partition_at=archived_timestamp,
    )
    append_signal(
        Signal(
            id="sig-retained",
            timestamp=retained_timestamp,
            source="manual",
            program_id="acme",
            workstream_id=None,
            entity_refs=(),
            text="Keep this weekly partition.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
        ),
        programs_root=programs_root,
        partition_at=retained_timestamp,
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_resolve_uil_channel_binding_for_gather", lambda *_a, **_k: object())
    monkeypatch.setattr(gather, "_load_ado_items_via_uil", lambda _p, _ws, _ao, **__: ((), (), 0))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    artifacts = gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 11, 10, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    assert artifacts.archived_journal_files == 1
    assert (programs_root / "acme" / "journal_archive" / "2025-W19.jsonl").exists()
    assert not (programs_root / "acme" / "journal" / "2025-W19.jsonl").exists()
    assert (programs_root / "acme" / "journal" / "2025-W20.jsonl").exists()


def test_gather_program_auto_archives_using_retention_policy_when_authored(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text(
        (
            'schema_version: "2.0"\n'
            'id: acme\n'
            'name: Adventure + DD on PF\n'
            'retention_days:\n'
            '  default: 365\n'
            '  manual: 30\n'
        ),
        encoding="utf-8",
    )

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    mixed_manual_timestamp = datetime(2025, 1, 7, 12, 0, tzinfo=timezone.utc)
    mixed_ado_timestamp = datetime(2025, 1, 8, 12, 0, tzinfo=timezone.utc)
    eligible_manual_timestamp = datetime(2025, 1, 14, 12, 0, tzinfo=timezone.utc)
    retained_manual_timestamp = datetime(2025, 3, 1, 12, 0, tzinfo=timezone.utc)

    append_signal(
        Signal(
            id="sig-mixed-manual",
            timestamp=mixed_manual_timestamp,
            source="manual",
            program_id="acme",
            workstream_id=None,
            entity_refs=(),
            text="Mixed manual signal.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
        ),
        programs_root=programs_root,
        partition_at=mixed_manual_timestamp,
    )
    append_signal(
        Signal(
            id="sig-mixed-ado",
            timestamp=mixed_ado_timestamp,
            source="ado/revision",
            program_id="acme",
            workstream_id=None,
            entity_refs=(),
            text="Mixed ADO signal.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
        ),
        programs_root=programs_root,
        partition_at=mixed_ado_timestamp,
    )
    append_signal(
        Signal(
            id="sig-eligible-manual",
            timestamp=eligible_manual_timestamp,
            source="manual",
            program_id="acme",
            workstream_id=None,
            entity_refs=(),
            text="Eligible manual signal.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
        ),
        programs_root=programs_root,
        partition_at=eligible_manual_timestamp,
    )
    append_signal(
        Signal(
            id="sig-retained-manual",
            timestamp=retained_manual_timestamp,
            source="manual",
            program_id="acme",
            workstream_id=None,
            entity_refs=(),
            text="Retained manual signal.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
        ),
        programs_root=programs_root,
        partition_at=retained_manual_timestamp,
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_resolve_uil_channel_binding_for_gather", lambda *_a, **_k: object())
    monkeypatch.setattr(gather, "_load_ado_items_via_uil", lambda _p, _ws, _ao, **__: ((), (), 0))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    artifacts = gather.gather_program(
        "acme",
        as_of=datetime(2025, 3, 15, 12, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    assert artifacts.archived_journal_files == 1
    assert (programs_root / "acme" / "journal_archive" / "2025-W03.jsonl").exists()
    assert (programs_root / "acme" / "journal" / "2025-W02.jsonl").exists()
    assert (programs_root / "acme" / "journal" / "2025-W09.jsonl").exists()


def test_gather_program_dedupes_extracted_actions_across_repeated_runs(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr(gather, "PROGRAMS_ROOT", programs_root)

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    revision = Revision(
        work_item_id=1234,
        rev_number=7,
        changed_by="priya@example.com",
        changed_by_email="priya@example.com",
        changed_date=datetime(2026, 5, 8, 9, 0, tzinfo=timezone.utc),
        fields_changed={"System.State": ("Proposed", "Active")},
    )
    item = WorkItem(
        id=1234,
        type="Feature",
        title="Ramp checkpoint",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="One\\FY26\\Q4",
        target_date=date(2026, 5, 17),
        risk_level=RiskLevel.MEDIUM,
        tags=["acme"],
        custom_fields={},
        revisions=[revision],
        comments=[],
        fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_resolve_uil_channel_binding_for_gather", lambda *_a, **_k: object())
    monkeypatch.setattr(gather, "_load_ado_items_via_uil", lambda _p, _ws, _ao, **__: ((item,), (item,), 0))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    def _fake_extract_actions(signals, program_id):
        if not signals:
            return ()
        return (
            ActionItem(
                id="action-1",
                program_id=program_id,
                text="Follow up with Priya",
                owner_alias="priya",
                due_date=date(2026, 5, 20),
                status=ActionStatus.PROPOSED,
                source_signal_id=signals[0].id,
                source_type=ActionSourceType.SIGNAL,
                linked_work_item_ids=(1234,),
                linked_claim_id=None,
                linked_risk_id=None,
                workstream_id="acme",
                created_at=signals[0].timestamp,
                resolved_at=None,
                resolution_note=None,
            ),
        )

    monkeypatch.setattr(
        "src.commands.gather_pipeline.persistence_stage.extract_actions_from_signals",
        _fake_extract_actions,
    )

    first = runner.invoke(app, ["gather", "--program", "acme"])
    second = runner.invoke(app, ["gather", "--program", "acme"])

    actions = load_actions("acme", programs_root=programs_root)

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert len(actions) == 1
    assert actions[0].id == "action-1"


def test_refresh_dependency_scout_state_reads_dependencies_from_program_facts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.commands.gather_pipeline.projection_stage import refresh_dependency_scout_state

    programs_root = tmp_path / "programs"
    sentinel_snapshot = object()
    sentinel_dependency = object()
    captured: dict[str, object] = {}

    def _load_program_facts(
        program_id: str,
        *,
        programs_root: Path,
        fact_types: tuple[str, ...],
    ):
        captured["program_id"] = program_id
        captured["programs_root"] = programs_root
        captured["fact_types"] = fact_types
        return sentinel_snapshot

    monkeypatch.setattr("src.commands.gather_pipeline.projection_stage.load_program_facts", _load_program_facts)
    monkeypatch.setattr(
        "src.commands.gather_pipeline.projection_stage.project_dependencies",
        lambda snapshot: (sentinel_dependency,) if snapshot is sentinel_snapshot else (),
    )
    monkeypatch.setattr("src.commands.gather_pipeline.projection_stage.load_dependency_proposals", lambda program_id, programs_root: ())
    monkeypatch.setattr("src.commands.gather_pipeline.projection_stage.load_item_trajectories", lambda *args, **kwargs: {})

    def _scout_dependency_proposals(**kwargs):
        captured["existing_dependencies"] = kwargs["existing_dependencies"]
        return ()

    monkeypatch.setattr("src.commands.gather_pipeline.projection_stage.scout_dependency_proposals", _scout_dependency_proposals)
    monkeypatch.setattr("src.commands.gather_pipeline.projection_stage.save_dependency_proposals", lambda *args, **kwargs: None)

    class _SignalStore:
        def read(self, program_id: str):
            return ()

        def read_reviews(self, program_id: str):
            return ()

    generated = refresh_dependency_scout_state(
        "acme",
        items=(),
        workstreams=(),
        signal_store=_SignalStore(),
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        trajectories_by_item={},
    )

    assert generated == 0
    assert captured == {
        "program_id": "acme",
        "programs_root": programs_root,
        "fact_types": ("dependency.link",),
        "existing_dependencies": (sentinel_dependency,),
    }


def test_refresh_contradiction_state_wires_dependencies_into_contradiction_packets(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """ADF-W2.10 P6: `_refresh_contradiction_state` must load the program's
    dependencies and pass them into `build_contradiction_packets` so a
    dependency-status claim can actually surface a contradiction (built but
    never wired is the failure mode this guards against)."""
    from src.core.models_v2 import Dependency, DependencyStatus, DependencyType

    programs_root = tmp_path / "programs"
    item = WorkItem(
        id=3001,
        type="Feature",
        title="Dependency wiring case",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Demo\\Deployment",
        iteration_path="Sprint 1",
        target_date=None,
        risk_level=RiskLevel.MEDIUM,
        tags=[],
        custom_fields={},
    )
    workstream = Workstream(
        id="deployment",
        name="Deployment",
        area_paths=("One\\Demo\\Deployment",),
        signal_sources=WorkstreamSignalSources(),
    )
    append_claim_entry(
        ClaimEntry(
            id="claim-dep-1",
            program_id="demo",
            edition_id="demo_weekly",
            issue_number=77,
            workstream_id="deployment",
            text="The dependency on team Rome is now broken.",
            entity_refs=("DEP:dep-rome",),
            claim_date=date(2026, 5, 20),
            owner_alias=None,
            due_date=None,
            claimed_status_family="dependency",
            claimed_status_value="broken",
        ),
        programs_root=programs_root,
    )
    dependency = Dependency(
        id="dep-rome",
        from_program_id="demo",
        from_workstream_id=None,
        from_item_id=3001,
        from_milestone_id=None,
        to_program_id="demo",
        to_workstream_id=None,
        to_item_id=None,
        to_milestone_id=None,
        dependency_type=DependencyType.BLOCKS,
        risk_if_broken="Downstream execution slips.",
        mitigation=None,
        status=DependencyStatus.ACTIVE,
        owner_alias=None,
    )
    monkeypatch.setattr(
        "src.commands.gather.load_current_dependencies",
        lambda program_id, *, programs_root: (dependency,) if program_id == "demo" else (),
    )

    class _SignalStore:
        def read(self, program_id: str, *, start, end):
            return ()

        def read_reviews(self, program_id: str):
            return {}

    as_of = datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc)
    gather._refresh_contradiction_state(
        "demo",
        items=(item,),
        workstreams=(workstream,),
        signal_store=_SignalStore(),
        signal_window_start=as_of - timedelta(days=30),
        as_of=as_of,
        programs_root=programs_root,
    )

    packets = load_contradiction_state("demo", programs_root=programs_root)
    assert len(packets) == 1
    assert packets[0].work_item_id == 3001
    dependency_contradictions = [c for c in packets[0].contradictions if c.field == "dependency_status"]
    assert len(dependency_contradictions) == 1


def test_compute_and_persist_plane1_changes_reads_plane1_state_from_program_facts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    programs_root = tmp_path / "programs"
    gathered_at = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    sentinel_snapshot = object()
    milestone = object()
    risk = object()
    decision = object()
    assumption = object()
    workstream = object()
    last_seen = object()
    current_snapshot = object()
    change = object()
    captured: dict[str, object] = {}

    def _load_program_facts(
        program_id: str,
        *,
        programs_root: Path,
        fact_types: tuple[str, ...],
    ):
        captured["program_id"] = program_id
        captured["programs_root"] = programs_root
        captured["fact_types"] = fact_types
        return sentinel_snapshot

    monkeypatch.setattr(gather, "load_program_facts", _load_program_facts)
    monkeypatch.setattr(gather, "project_milestones", lambda snapshot: (milestone,) if snapshot is sentinel_snapshot else ())
    monkeypatch.setattr(gather, "project_risk_entries", lambda snapshot: (risk,) if snapshot is sentinel_snapshot else ())
    monkeypatch.setattr(gather, "project_decision_entries", lambda snapshot: (decision,) if snapshot is sentinel_snapshot else ())
    monkeypatch.setattr(gather, "project_assumptions", lambda snapshot: (assumption,) if snapshot is sentinel_snapshot else ())
    monkeypatch.setattr(gather, "project_workstreams", lambda snapshot: (workstream,) if snapshot is sentinel_snapshot else ())
    monkeypatch.setattr(gather, "load_plane1_last_seen", lambda *_args, **_kwargs: last_seen)

    def _compute_plane1_changes(
        program_id: str,
        milestones: list[object],
        risks: list[object],
        workstreams: list[object],
        decisions: list[object],
        assumptions: list[object],
        loaded_last_seen: object,
        *,
        gather_run_id: str,
        gathered_at: datetime,
    ) -> list[object]:
        captured["compute_args"] = {
            "program_id": program_id,
            "milestones": milestones,
            "risks": risks,
            "workstreams": workstreams,
            "decisions": decisions,
            "assumptions": assumptions,
            "last_seen": loaded_last_seen,
            "gather_run_id": gather_run_id,
            "gathered_at": gathered_at,
        }
        return [change]

    monkeypatch.setattr(gather, "compute_plane1_changes", _compute_plane1_changes)

    def _build_plane1_snapshot(
        milestones: list[object],
        risks: list[object],
        workstreams: list[object],
        decisions: list[object],
        assumptions: list[object],
    ) -> object:
        captured["snapshot_args"] = {
            "milestones": milestones,
            "risks": risks,
            "workstreams": workstreams,
            "decisions": decisions,
            "assumptions": assumptions,
        }
        return current_snapshot

    monkeypatch.setattr(gather, "build_plane1_snapshot", _build_plane1_snapshot)
    monkeypatch.setattr(
        gather,
        "append_plane1_changes",
        lambda program_id, changes, *, programs_root: captured.setdefault(
            "append_args",
            {"program_id": program_id, "changes": changes, "programs_root": programs_root},
        ),
    )
    monkeypatch.setattr(
        gather,
        "shadow_write_plane1_snapshot",
        lambda program_id, snapshot, *, recorded_at, db_root=None, **_kwargs: captured.setdefault(
            "shadow_args",
            {"program_id": program_id, "snapshot": snapshot, "recorded_at": recorded_at, "db_root": db_root},
        ),
    )
    monkeypatch.setattr(
        gather,
        "persist_program_fact_snapshot",
        lambda snapshot, *, recorded_at, db_root=None: captured.setdefault(
            "persist_args",
            {"snapshot": snapshot, "recorded_at": recorded_at, "db_root": db_root},
        ),
    )
    monkeypatch.setattr(
        gather,
        "write_plane1_last_seen",
        lambda program_id, snapshot, *, programs_root: captured.setdefault(
            "write_args",
            {"program_id": program_id, "snapshot": snapshot, "programs_root": programs_root},
        ),
    )

    gather._compute_and_persist_plane1_changes("acme", programs_root, gathered_at)

    assert captured == {
        "program_id": "acme",
        "programs_root": programs_root,
        "fact_types": (
            "action.item",
            "dependency.link",
            "milestone.entry",
            "risk.entry",
            "decision.entry",
            "assumption.entry",
            "workstream.entry",
        ),
        "compute_args": {
            "program_id": "acme",
            "milestones": [milestone],
            "risks": [risk],
            "workstreams": [workstream],
            "decisions": [decision],
            "assumptions": [assumption],
            "last_seen": last_seen,
            "gather_run_id": "20260510T080000Z",
            "gathered_at": gathered_at,
        },
        "snapshot_args": {
            "milestones": [milestone],
            "risks": [risk],
            "workstreams": [workstream],
            "decisions": [decision],
            "assumptions": [assumption],
        },
        "append_args": {"program_id": "acme", "changes": [change], "programs_root": programs_root},
        "shadow_args": {
            "program_id": "acme",
            "snapshot": current_snapshot,
            "recorded_at": gathered_at,
            "db_root": programs_root.parent / "vertex-db",
        },
        "persist_args": {
            "snapshot": sentinel_snapshot,
            "recorded_at": gathered_at,
            "db_root": programs_root.parent / "vertex-db",
        },
        "write_args": {"program_id": "acme", "snapshot": current_snapshot, "programs_root": programs_root},
    }


def test_gather_program_writes_freshness_signals_for_all_active_items(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    changed_item = WorkItem(
        id=1234,
        type="Feature",
        title="Ramp checkpoint",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="One\\FY26\\Q4",
        target_date=date(2026, 5, 17),
        risk_level=RiskLevel.MEDIUM,
        tags=["acme"],
        custom_fields={},
        revisions=[
            Revision(
                work_item_id=1234,
                rev_number=7,
                changed_by="priya@example.com",
                changed_by_email="priya@example.com",
                changed_date=datetime(2026, 5, 8, 9, 0, tzinfo=timezone.utc),
                fields_changed={"System.State": ("Proposed", "Active")},
            )
        ],
        comments=[],
        fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )
    stale_unowned_item = WorkItem(
        id=5678,
        type="Feature",
        title="Forgotten deployment work item",
        state="Active",
        assigned_to=None,
        assigned_to_email=None,
        area_path="One\\Adventure\\Acme",
        iteration_path="One\\FY26\\Q4",
        target_date=None,
        risk_level=RiskLevel.MEDIUM,
        tags=["acme"],
        custom_fields={},
        revisions=[
            Revision(
                work_item_id=5678,
                rev_number=2,
                changed_by="system",
                changed_by_email="system@example.com",
                changed_date=datetime(2026, 4, 10, 8, 0, tzinfo=timezone.utc),
                fields_changed={"System.State": ("Proposed", "Active")},
            )
        ],
        comments=[],
        fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    artifacts = gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((changed_item,), 3),
        freshness_loader=lambda program, workstreams, as_of, **_: ((changed_item, stale_unowned_item), 2),
    )

    signals = read_signals("acme", programs_root=programs_root)
    reviews = read_review_log("acme", programs_root=programs_root)
    freshness_signals = tuple(signal for signal in signals if signal.source == "vertex/freshness")
    freshness_reviews = {
        review.signal_id: review
        for review in reviews
        if getattr(review, "record_type", None) == "review"
    }

    assert artifacts.discovered_signals == 4
    assert artifacts.new_signals == 4
    assert artifacts.pending_review == 0
    assert artifacts.ado_calls == 5
    assert len(freshness_signals) == 3
    assert {signal.metadata["finding_type"] for signal in freshness_signals if signal.metadata is not None} == {"FR-22", "FR-43", "FR-46"}
    assert all(signal.workstream_id is not None for signal in freshness_signals)
    assert all(f"WS:{signal.workstream_id}" in signal.entity_refs for signal in freshness_signals)
    assert all(freshness_reviews[signal.id].decision == "approved" for signal in freshness_signals)
    assert all(freshness_reviews[signal.id].reviewed_by == "system" for signal in freshness_signals)


def test_gather_program_refreshes_contradiction_cache(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Demo\\Deployment",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="deployment",
            name="Deployment",
            area_paths=("One\\Demo\\Deployment",),
            dri_email="maintainer@example.com",
        ),
    )
    item = WorkItem(
        id=1234,
        type="Feature",
        title="Deployment chunking",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Demo\\Deployment",
        iteration_path="One\\FY26\\Q4",
        target_date=date(2026, 6, 10),
        risk_level=RiskLevel.HIGH,
        tags=["acme"],
        custom_fields={},
        revisions=[],
        comments=[],
        fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    append_claim_entry(
        ClaimEntry(
            id="claim-1",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            workstream_id="deployment",
            text="Expected by 2026-06-01",
            entity_refs=("WI:1234",),
            claim_date=date(2026, 5, 20),
            owner_alias="priya",
            due_date=date(2026, 6, 1),
        ),
        programs_root=programs_root,
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((item,), 1),
        freshness_loader=lambda program, workstreams, as_of, **_: ((item,), 1),
    )

    packets = load_contradiction_state("acme", programs_root=programs_root)

    assert len(packets) == 1
    assert packets[0].work_item_id == 1234
    assert packets[0].workstream_id == "deployment"
    assert packets[0].contradictions[0].source_b == "journal/claim"


def test_gather_program_proposes_delivery_date_hypotheses_from_open_claims(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr(gather, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "vertex-db"))

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Demo\\Deployment",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="deployment",
            name="Deployment",
            area_paths=("One\\Demo\\Deployment",),
            dri_email="maintainer@example.com",
        ),
    )
    item = WorkItem(
        id=1234,
        type="Feature",
        title="Deployment chunking",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Demo\\Deployment",
        iteration_path="One\\FY26\\Q4",
        target_date=date(2026, 6, 10),
        risk_level=RiskLevel.HIGH,
        tags=["acme"],
        custom_fields={},
        revisions=[],
        comments=[],
        fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    append_claim_entry(
        ClaimEntry(
            id="claim-1",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            workstream_id="deployment",
            text="Expected by 2026-06-01",
            entity_refs=("WI:1234",),
            claim_date=date(2026, 5, 20),
            owner_alias="priya",
            due_date=date(2026, 6, 1),
        ),
        programs_root=programs_root,
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((item,), 1),
        freshness_loader=lambda program, workstreams, as_of, **_: ((item,), 1),
    )

    store = RealityStore("acme", db_root=tmp_path / "vertex-db")
    proposed = store.list_hypotheses(statuses=(HypothesisStatus.PROPOSED,))

    assert len(proposed) == 1
    assert proposed[0].kind is HypothesisKind.DELIVERY_DATE
    assert proposed[0].linked_claim_id == "claim-1"
    assert proposed[0].linked_ado_item_id == 1234


def test_run_hypothesis_proposers_uses_registered_runner(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    append_claim_entry(
        ClaimEntry(
            id="claim-1",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            workstream_id="deployment",
            text="Expected by 2026-06-01",
            entity_refs=("WI:1234",),
            claim_date=date(2026, 5, 20),
            owner_alias="priya",
            due_date=date(2026, 6, 1),
        ),
        programs_root=programs_root,
    )
    captured: dict[str, object] = {}

    def _fake_runner(*, store, claims, proposed_at):
        captured["store_program_id"] = store.program_id
        captured["claim_ids"] = tuple(claim.id for claim in claims)
        captured["proposed_at"] = proposed_at
        return ("sentinel",)

    monkeypatch.setattr(hypothesis_stage, "run_registered_hypothesis_proposers", _fake_runner)

    result = gather._run_hypothesis_proposers(
        "acme",
        programs_root=programs_root,
        proposed_at=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        reality_store=RealityStore("acme", db_root=tmp_path / "vertex-db"),
    )

    assert result == ("sentinel",)
    assert captured["store_program_id"] == "acme"
    assert captured["claim_ids"] == ("claim-1",)

def test_gather_program_emits_ado_comment_signals(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    db_root = tmp_path / "vertex-db"
    monkeypatch.setenv("VERTEX_DB_PATH", str(db_root))

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(id="acme", name="Acme", area_paths=("One\\Adventure\\Acme",)),
    )
    item = WorkItem(
        id=1234,
        type="Feature",
        title="Ramp checkpoint",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="One\\FY26\\Q4",
        target_date=date(2026, 5, 17),
        risk_level=RiskLevel.MEDIUM,
        tags=["acme"],
        custom_fields={},
        revisions=[],
        comments=[
            Comment(
                work_item_id=1234,
                comment_id=42,
                created_by="Priya",
                created_by_email="priya@example.com",
                created_date=datetime(2026, 5, 10, 7, 0, tzinfo=timezone.utc),
                text="SCHIE gap resolved per 5/20 call.",
            )
        ],
        fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_resolve_uil_channel_binding_for_gather", lambda *_a, **_k: object())
    monkeypatch.setattr(gather, "_load_ado_items_via_uil", lambda _p, _ws, _ao, **__: ((item,), (), 0))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    artifacts = gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    signals = read_signals("acme", programs_root=programs_root)

    assert artifacts.new_signals == 1
    assert len(signals) == 1
    assert signals[0].source == "ado/comment"
    assert signals[0].workstream_id is not None
    assert f"WS:{signals[0].workstream_id}" in signals[0].entity_refs
    assert signals[0].metadata is not None
    assert signals[0].metadata["work_item_id"] == 1234
    assert signals[0].metadata["comment_id"] == 42
    assert signals[0].metadata["author"] == "Priya"
    assert signals[0].metadata["signal_class"] == SignalClass.STATUS.value


def test_load_dependency_program_items_reads_configured_area_paths_and_ids(monkeypatch) -> None:
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(
                dependency_ado_queries=(
                    DependencyADOQuery(
                        label="OneDeploy stager",
                        area_path="One\\Azure Compute\\OneDeploy\\Stager",
                        resolution_path="cross_org_onedeploy",
                    ),
                    DependencyADOQuery(
                        label="SCHIE gap owners",
                        resolution_path="cross_org_compute_pf",
                        work_item_ids=(9001,),
                    ),
                )
            ),
        ),
    )

    created_clients: list[object] = []

    class _FakeDependencyADOClient:
        def __init__(self, *args, **kwargs) -> None:
            self.calls: list[tuple[str, object]] = []
            created_clients.append(self)

        def query_all(self, *, filter_expression: str, select_fields: tuple[str, ...]) -> list[dict[str, object]]:
            self.calls.append(("query_all", filter_expression))
            return [{"WorkItemId": 1001}]

        def query_work_items_batch(self, work_item_ids: list[int], fields: tuple[str, ...]) -> list[dict[str, object]]:
            self.calls.append(("query_work_items_batch", tuple(work_item_ids)))
            return [
                {
                    "id": 1001,
                    "fields": {
                        "System.Id": 1001,
                        "System.WorkItemType": "Feature",
                        "System.Title": "OneDeploy staging blocker",
                        "System.State": "Active",
                        "System.AreaPath": "One\\Azure Compute\\OneDeploy\\Stager",
                        "System.IterationPath": "One\\FY26\\Q4",
                        "System.AssignedTo": {"displayName": "Owner", "uniqueName": "owner@example.com"},
                        "Microsoft.VSTS.Scheduling.TargetDate": "2026-05-01",
                    },
                },
                {
                    "id": 9001,
                    "fields": {
                        "System.Id": 9001,
                        "System.WorkItemType": "Feature",
                        "System.Title": "SCHIE owner action",
                        "System.State": "Active",
                        "System.AreaPath": "One\\Adventure\\Acme",
                        "System.IterationPath": "One\\FY26\\Q4",
                        "System.AssignedTo": {"displayName": "Owner", "uniqueName": "owner@example.com"},
                        "Microsoft.VSTS.Scheduling.TargetDate": "2026-05-01",
                    },
                },
            ]

    monkeypatch.setattr(gather, "ADOClient", _FakeDependencyADOClient)

    groups, ado_calls = gather._load_dependency_program_items(
        program,
        workstreams,
        datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    assert ado_calls == 3
    assert [(group.label, [item.id for item in group.items]) for group in groups] == [
        ("OneDeploy stager", [1001]),
        ("SCHIE gap owners", [9001]),
    ]
    assert created_clients[0].calls[0][0] == "query_all"
    assert "startswith(Area/AreaPath, 'One\\Azure Compute\\OneDeploy\\Stager')" in str(created_clients[0].calls[0][1])
    assert "WorkItemType eq 'Feature'" in str(created_clients[0].calls[0][1])
    assert "ChangedDate ge 2026-04-26T08:00:00Z" in str(created_clients[0].calls[0][1])
    assert created_clients[0].calls[1:] == [
        ("query_work_items_batch", (1001,)),
        ("query_work_items_batch", (9001,)),
    ]


def test_gather_program_emits_dependency_signals(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    db_root = tmp_path / "vertex-db"
    monkeypatch.setenv("VERTEX_DB_PATH", str(db_root))

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(id="acme", name="Acme", area_paths=("One\\Adventure\\Acme",)),
    )
    dependency_item = WorkItem(
        id=4321,
        type="Feature",
        title="Cross-org blocker",
        state="Active",
        assigned_to="Owner",
        assigned_to_email="owner@example.com",
        area_path="One\\Azure Compute\\OneDeploy\\Stager",
        iteration_path="One\\FY26\\Q4",
        target_date=date(2026, 5, 1),
        risk_level=RiskLevel.MEDIUM,
        tags=["acme"],
        custom_fields={},
        revisions=[],
        comments=[],
        fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_resolve_uil_channel_binding_for_gather", lambda *_a, **_k: object())
    monkeypatch.setattr(gather, "_load_ado_items_via_uil", lambda _p, _ws, _ao, **__: ((), (), 0))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(
        gather,
        "_load_dependency_program_items",
        lambda program, workstreams, as_of, **_: (
            (
                gather._DependencyQueryItems(
                    workstream_id="acme",
                    label="OneDeploy stager",
                    resolution_path="cross_org_onedeploy",
                    items=(dependency_item,),
                ),
            ),
            1,
        ),
    )

    artifacts = gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
    )
    signals = read_signals("acme", programs_root=programs_root)

    assert artifacts.new_signals == 1
    assert len(signals) == 1
    assert signals[0].source == "ado/dependency"
    assert signals[0].workstream_id is not None
    assert f"WS:{signals[0].workstream_id}" in signals[0].entity_refs
    assert signals[0].metadata is not None
    assert signals[0].metadata["dependency_label"] == "OneDeploy stager"
    assert signals[0].metadata["resolution_path"] == "cross_org_onedeploy"
    assert signals[0].metadata["work_item_id"] == 4321
    assert signals[0].metadata["finding_type"] == "FR-21"
    assert signals[0].metadata["severity"] == "block"
    assert signals[0].metadata["date"] == "2026-05-10"
    assert signals[0].metadata["signal_class"] == SignalClass.DEPENDENCY.value


def test_gather_program_records_frozen_dependency_query_state_history(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(id="acme", name="Acme", area_paths=("One\\Adventure\\Acme",)),
    )
    dependency_item = WorkItem(
        id=4321,
        type="Feature",
        title="OneDeploy staging blocked",
        state="Active",
        assigned_to="Taylor",
        assigned_to_email="taylor@example.com",
        area_path="One\\Azure Compute\\OneDeploy\\Stager",
        iteration_path="One\\Sprint 24",
        target_date=date(2026, 5, 20),
        risk_level=RiskLevel.HIGH,
        tags=["acme"],
        custom_fields={"changed_date": "2026-05-09T12:00:00+00:00"},
        revisions=[],
        comments=[],
        fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_resolve_uil_channel_binding_for_gather", lambda *_a, **_k: object())
    monkeypatch.setattr(gather, "_load_ado_items_via_uil", lambda _p, _ws, _ao, **__: ((), (), 0))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(
        gather,
        "_load_dependency_program_items",
        lambda program, workstreams, as_of, **_: (
            (
                gather._DependencyQueryItems(
                    workstream_id="acme",
                    label="OneDeploy stager",
                    resolution_path="cross_org_onedeploy",
                    items=(dependency_item,),
                ),
            ),
            1,
        ),
    )

    for day in range(4):
        gather.gather_program(
            "acme",
            as_of=datetime(2026, 5, 10 + day, 8, 0, tzinfo=timezone.utc),
            programs_root=programs_root,
        )

    gather_state = load_gather_state("acme", programs_root=programs_root)

    assert gather_state is not None
    query_state = gather_state.query_states["ado-dependency:acme:OneDeploy stager"]
    assert query_state["last_cycle_succeeded"] is True
    assert query_state["row_count"] == 1
    assert query_state["signal_count"] == 1
    assert query_state["dependency_label"] == "OneDeploy stager"
    assert query_state["resolution_path"] == "cross_org_onedeploy"
    assert query_state["data_freshness_ok"] is True
    assert query_state["value_last_4"] == [1.0, 1.0, 1.0, 1.0]
    assert query_state["value_frozen_warning"] is True

def test_slice_contract_saved_query_clauses_apply_tag_expression(tmp_path: Path) -> None:
        contract_path = tmp_path / "slice_contracts.yaml"
        contract_path.write_text(
                "\n".join(
                        [
                                'schema_version: "1.0"',
                                "slices:",
                                "  - id: demo.slice",
                                '    scorecard_name: "Demo"',
                                "    section: demo",
                                "    workstream: Demo",
                                "    slice_kind: scorecard_dimension",
                                '    title: "Deployment"',
                                "    source_of_truth: ado_primary",
                                "    owners:",
                                '      primary: "Owner"',
                                "    source_contract:",
                                "      ado:",
                                "        saved_queries:",
                                "          - query-1",
                                "        tag_expression:",
                                "          all_of: [RAMPP1]",
                                "          any_of: [Contoso, Acme]",
                                "        explicit_work_item_ids: []",
                                "        required_fields: [state]",
                                "    freshness:",
                                "      warn_days: 5",
                                "      block_days: 10",
                                "    degradation:",
                                "      blank_filter_is_error: true",
                        ]
                ),
                encoding="utf-8",
        )

        contracts = gather.load_slice_contract(contract_path)

        assert gather._slice_contract_saved_query_clauses(contracts) == {
                "query-1": "([System.Tags] Contains Words 'RAMPP1' and ([System.Tags] Contains Words 'Contoso' or [System.Tags] Contains Words 'Acme'))"
        }


def test_load_ado_items_via_uil_discovers_hydrates_and_marks_verified(monkeypatch, tmp_path: Path) -> None:
        from src.core.integration_types import (
                ADOHydrationOutput,
                ChannelBinding,
                ChannelConfig,
                ChannelRegistration,
                DiscoveredRef,
                DiscoveryCompleteness,
                DiscoveryResult,
                HydrationResult,
                RegistrationBinding,
                RegistrationStatus,
        )

        current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
        program = Program(
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
        )
        workstreams = (
                Workstream(
                        id="demo.slice",
                        name="Demo",
                        area_paths=("One\\Demo",),
                        dri_email="owner@example.com",
                ),
        )
        item = WorkItem(
                id=101,
                type="Feature",
                title="Hydrated",
                state="Active",
                assigned_to="Owner",
                assigned_to_email="owner@example.com",
                area_path="One\\Demo",
                iteration_path="One\\Iteration",
                target_date=None,
                risk_level=RiskLevel.UNKNOWN,
                tags=["RAMPP1"],
                custom_fields={"workstream_ids": ("demo.slice",)},
                fetched_at=current_time,
        )

        class _DiscoveryProvider:
                def discover(self, program_id, config, existing, run_ctx=None):
                        del config, existing, run_ctx
                        registration = ChannelRegistration(
                                channel="ado",
                                program_id=program_id,
                                provider_instance_id="default",
                                ref_id="101",
                                ref_kind="work_item",
                                status=RegistrationStatus.ACTIVE,
                                first_discovered_at=current_time,
                                last_seen_at=current_time,
                        )
                        return DiscoveryResult(
                                channel="ado",
                                program_id=program_id,
                                discovered_refs=(
                                        DiscoveredRef(
                                                registration=registration,
                                                bindings=(
                                                        RegistrationBinding(
                                                                workstream_id="demo.slice",
                                                                scope_id="scope",
                                                                source_type="wiql_saved_query",
                                                                confidence=1.0,
                                                                confidence_source="wiql_saved_query",
                                                        ),
                                                ),
                                        ),
                                ),
                                completeness=DiscoveryCompleteness.FULL,
                                scope_statuses={},
                                scope_state_updates={},
                                errors=(),
                                computed_at=current_time,
                        )

        class _HydrationProvider:
                def hydrate(self, registrations, since, program_id, config, mode=None, run_ctx=None):
                        del since, program_id, config, mode, run_ctx
                        assert registrations[0].workstream_ids == ("demo.slice",)
                        return HydrationResult(
                                channel="ado",
                                resources=ADOHydrationOutput(work_items=(item,), freshness_items=(item,)),
                                api_call_count=1,
                                errors=(),
                                hydrated_ref_ids=(("101", "work_item"),),
                                failed_ref_ids=(),
                        )

        binding = ChannelBinding(
                config=ChannelConfig(channel="ado", enabled=True, discovery_threshold_hours=24, ttl_days=30),
                discovery_provider=_DiscoveryProvider(),
                hydration_provider=_HydrationProvider(),
                signal_extractor=object(),
                discovery_config=object(),
                hydration_config=object(),
        )
        monkeypatch.setattr("src.commands.channel_wiring.resolve_channel_bindings", lambda *args, **kwargs: (binding,))

        items, freshness_items, ado_calls = gather._load_ado_items_via_uil(
                program,
                workstreams,
                current_time,
                since=current_time - timedelta(days=14),
                programs_root=tmp_path,
                integration_error_sink=[],
        )

        assert items == (item,)
        assert freshness_items == (item,)
        assert ado_calls == 1


def test_load_ado_items_via_uil_surfaces_hydration_failure_without_raising(monkeypatch, tmp_path: Path) -> None:
        from src.core.integration_types import (
            ChannelBinding,
            ChannelConfig,
            ChannelRegistration,
            DiscoveredRef,
            DiscoveryCompleteness,
            DiscoveryResult,
            RegistrationBinding,
            RegistrationStatus,
        )

        current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
        program = Program(
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
        )
        workstreams = (
            Workstream(
                id="demo.slice",
                name="Demo",
                area_paths=("One\\Demo",),
                dri_email="owner@example.com",
            ),
        )

        class _DiscoveryProvider:
            def discover(self, program_id, config, existing, run_ctx=None):
                del config, existing, run_ctx
                registration = ChannelRegistration(
                    channel="ado",
                    program_id=program_id,
                    provider_instance_id="default",
                    ref_id="101",
                    ref_kind="work_item",
                    status=RegistrationStatus.ACTIVE,
                    first_discovered_at=current_time,
                    last_seen_at=current_time,
                )
                return DiscoveryResult(
                    channel="ado",
                    program_id=program_id,
                    discovered_refs=(
                        DiscoveredRef(
                            registration=registration,
                            bindings=(
                                RegistrationBinding(
                                    workstream_id="demo.slice",
                                    scope_id="scope",
                                    source_type="wiql_saved_query",
                                    confidence=1.0,
                                    confidence_source="wiql_saved_query",
                                ),
                            ),
                        ),
                    ),
                    completeness=DiscoveryCompleteness.FULL,
                    scope_statuses={},
                    scope_state_updates={},
                    errors=(),
                    computed_at=current_time,
                )

        class _HydrationProvider:
            def hydrate(self, registrations, since, program_id, config, mode=None, run_ctx=None):
                del registrations, since, program_id, config, mode, run_ctx
                raise QueryError("ado hydrate failed")

        binding = ChannelBinding(
            config=ChannelConfig(channel="ado", enabled=True, discovery_threshold_hours=24, ttl_days=30),
            discovery_provider=_DiscoveryProvider(),
            hydration_provider=_HydrationProvider(),
            signal_extractor=object(),
            discovery_config=object(),
            hydration_config=object(),
        )
        monkeypatch.setattr("src.commands.channel_wiring.resolve_channel_bindings", lambda *args, **kwargs: (binding,))
        errors: list[IntegrationError] = []

        items, freshness_items, ado_calls = gather._load_ado_items_via_uil(
            program,
            workstreams,
            current_time,
            since=current_time - timedelta(days=14),
            programs_root=tmp_path,
            integration_error_sink=errors,
        )

        assert items == ()
        assert freshness_items == ()
        assert ado_calls == 0
        assert len(errors) == 1
        assert errors[0].stage == "hydration"


def test_load_ado_items_via_uil_uses_legacy_runtime_when_gather_v2_disabled(monkeypatch, tmp_path: Path) -> None:
        current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
        program = Program(
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
        )
        item = WorkItem(
            id=101,
            type="Feature",
            title="Hydrated",
            state="Active",
            assigned_to="Owner",
            assigned_to_email="owner@example.com",
            area_path="One\\Demo",
            iteration_path="One\\Iteration",
            target_date=None,
            risk_level=RiskLevel.UNKNOWN,
            tags=["RAMPP1"],
            custom_fields={},
            fetched_at=current_time,
        )
        hydration_result = SimpleNamespace(
            resources=SimpleNamespace(work_items=(item,), freshness_items=(item,)),
            api_call_count=1,
        )
        called = {"legacy": False}

        def _fake_run_channel(*args, **kwargs):
            called["legacy"] = True
            return hydration_result, None

        monkeypatch.setenv("VERTEX_GATHER_V2", "0")
        monkeypatch.setattr(gather, "_run_channel", _fake_run_channel)

        items, freshness_items, ado_calls = gather._load_ado_items_via_uil(
            program,
            (),
            current_time,
            since=current_time - timedelta(days=14),
            programs_root=tmp_path,
            binding=SimpleNamespace(),
        )

        assert called["legacy"] is True
        assert items == (item,)
        assert freshness_items == (item,)
        assert ado_calls == 1


def test_load_ado_items_via_uil_uses_gather_pipeline_runtime_when_enabled(monkeypatch, tmp_path: Path) -> None:
        import src.commands.gather_pipeline as gather_pipeline

        current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
        program = Program(
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
        )
        item = WorkItem(
            id=101,
            type="Feature",
            title="Hydrated",
            state="Active",
            assigned_to="Owner",
            assigned_to_email="owner@example.com",
            area_path="One\\Demo",
            iteration_path="One\\Iteration",
            target_date=None,
            risk_level=RiskLevel.UNKNOWN,
            tags=["RAMPP1"],
            custom_fields={},
            fetched_at=current_time,
        )
        hydration_result = SimpleNamespace(
            resources=SimpleNamespace(work_items=(item,), freshness_items=(item,)),
            api_call_count=1,
        )
        called = {"v2": False}

        def _fake_v2_run_channel(*args, **kwargs):
            called["v2"] = True
            return hydration_result, None

        monkeypatch.setenv("VERTEX_GATHER_V2", "1")
        monkeypatch.setattr(gather, "_run_channel", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy path used")))
        monkeypatch.setattr(gather_pipeline, "run_channel", _fake_v2_run_channel)

        items, freshness_items, ado_calls = gather._load_ado_items_via_uil(
            program,
            (),
            current_time,
            since=current_time - timedelta(days=14),
            programs_root=tmp_path,
            binding=SimpleNamespace(),
        )

        assert called["v2"] is True
        assert items == (item,)
        assert freshness_items == (item,)
        assert ado_calls == 1


def test_load_kusto_signals_via_uil_discovers_hydrates_and_extracts(monkeypatch, tmp_path: Path) -> None:
        from src.core.integration_types import (
                ChannelBinding,
                ChannelConfig,
                ChannelRegistration,
                DiscoveredRef,
                DiscoveryCompleteness,
                DiscoveryResult,
                ExtractionResult,
                HydrationResult,
                KustoHydrationOutput,
                KustoResultSet,
                RegistrationBinding,
                RegistrationStatus,
        )
        from src.core.kusto_discovery import KustoDiscoveryConfig
        from src.core.kusto_hydration import KustoHydrationConfig

        current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
        program = Program(
                schema_version="3.0",
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
                kusto=KustoConfig(enabled=True),
        )

        class _DiscoveryProvider:
                def discover(self, program_id, config, existing, run_ctx=None):
                        del config, existing, run_ctx
                        registration = ChannelRegistration(
                                channel="kusto",
                                program_id=program_id,
                                provider_instance_id="default",
                                ref_id="query-a",
                                ref_kind="kusto_query",
                                status=RegistrationStatus.ACTIVE,
                                first_discovered_at=current_time,
                                last_seen_at=current_time,
                        )
                        return DiscoveryResult(
                                channel="kusto",
                                program_id=program_id,
                                discovered_refs=(
                                        DiscoveredRef(
                                                registration=registration,
                                                bindings=(
                                                        RegistrationBinding(
                                                                workstream_id="demo.slice",
                                                                scope_id="query-a",
                                                                source_type="kusto_query",
                                                                confidence=1.0,
                                                                confidence_source="manual_config",
                                                        ),
                                                ),
                                        ),
                                ),
                                completeness=DiscoveryCompleteness.FULL,
                                scope_statuses={},
                                scope_state_updates={},
                                errors=(),
                                computed_at=current_time,
                        )

        query = KustoQuery(
                id="query-a",
                cluster="https://cluster",
                database="db",
                kql="StormEvents | take 1",
                section="A",
                render_as="table",
                confidence="high",
                workstream_ids=("demo.slice",),
                validated=True,
        )

        class _HydrationProvider:
                def __init__(self) -> None:
                        self._query_loader = lambda program_id, programs_root: (query,)
                        self._executor = lambda rendered_query: [{"Value": 1}]

        class _SignalExtractor:
                def extract(self, resources, program_id):
                        del resources
                        return ExtractionResult(
                                channel="kusto",
                                signals=(
                                        Signal(
                                                id="kusto/query-a/demo.slice",
                                                timestamp=current_time,
                                                source="kusto",
                                                program_id=program_id,
                                                workstream_id="demo.slice",
                                                entity_refs=("kusto:query-a",),
                                                text="Kusto query result.",
                                                raw_ref="kusto/query-a/demo.slice",
                                                confidence=Confidence.HIGH,
                                                review_policy=None,
                                                metadata={"query_id": "query-a"},
                                        ),
                                ),
                                trajectory_points=(),
                                side_artifacts={},
                                errors=(),
                        )

        binding = ChannelBinding(
                config=ChannelConfig(channel="kusto", enabled=True, discovery_threshold_hours=24, ttl_days=30),
                discovery_provider=_DiscoveryProvider(),
                hydration_provider=_HydrationProvider(),
                signal_extractor=_SignalExtractor(),
                discovery_config=KustoDiscoveryConfig(programs_root=tmp_path),
                hydration_config=KustoHydrationConfig(programs_root=tmp_path),
        )
        monkeypatch.setattr("src.commands.channel_wiring.resolve_channel_bindings", lambda *args, **kwargs: (binding,))

        signals, api_calls = gather._load_kusto_signals_via_uil(
                program,
                (),
                current_time,
                programs_root=tmp_path,
                integration_error_sink=[],
        )

        assert api_calls == 1
        assert len(signals) == 1
        assert signals[0].source == "kusto"
        assert signals[0].entity_refs == ("kusto:query-a",)


def test_load_kusto_signals_via_uil_records_query_state(monkeypatch, tmp_path: Path) -> None:
        from src.core.integration_types import (
                ChannelBinding,
                ChannelConfig,
                ChannelRegistration,
                DiscoveredRef,
                DiscoveryCompleteness,
                DiscoveryResult,
                ExtractionResult,
                HydrationResult,
                KustoHydrationOutput,
                KustoResultSet,
                RegistrationBinding,
                RegistrationStatus,
        )
        from src.core.kusto_discovery import KustoDiscoveryConfig
        from src.core.kusto_hydration import KustoHydrationConfig

        current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
        program = Program(
                schema_version="3.0",
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
                kusto=KustoConfig(enabled=True),
        )

        query = KustoQuery(
                id="query-a",
                cluster="https://cluster",
                database="db",
                kql="StormEvents | take 1",
                section="A",
                render_as="table",
                confidence="high",
                workstream_ids=("demo.slice",),
                validated=True,
        )

        class _DiscoveryProvider:
                def discover(self, program_id, config, existing, run_ctx=None):
                        del config, existing, run_ctx
                        registration = ChannelRegistration(
                                channel="kusto",
                                program_id=program_id,
                                provider_instance_id="default",
                                ref_id="query-a",
                                ref_kind="kusto_query",
                                status=RegistrationStatus.ACTIVE,
                                first_discovered_at=current_time,
                                last_seen_at=current_time,
                        )
                        return DiscoveryResult(
                                channel="kusto",
                                program_id=program_id,
                                discovered_refs=(
                                        DiscoveredRef(
                                                registration=registration,
                                                bindings=(
                                                        RegistrationBinding(
                                                                workstream_id="demo.slice",
                                                                scope_id="query-a",
                                                                source_type="kusto_query",
                                                                confidence=1.0,
                                                                confidence_source="manual_config",
                                                        ),
                                                ),
                                        ),
                                ),
                                completeness=DiscoveryCompleteness.FULL,
                                scope_statuses={},
                                scope_state_updates={},
                                errors=(),
                                computed_at=current_time,
                        )

        class _HydrationProvider:
                def __init__(self) -> None:
                        self._query_loader = lambda program_id, programs_root: (query,)
                        self._executor = lambda rendered_query: [{"Value": 1, "Timestamp": "2026-05-24T11:00:00Z"}]

        class _SignalExtractor:
                def extract(self, resources, program_id):
                        del resources
                        return ExtractionResult(
                                channel="kusto",
                                signals=(
                                        Signal(
                                                id="kusto/query-a/demo.slice",
                                                timestamp=current_time,
                                                source="kusto",
                                                program_id=program_id,
                                                workstream_id="demo.slice",
                                                entity_refs=("kusto:query-a",),
                                                text="Kusto query result.",
                                                raw_ref="kusto/query-a/demo.slice",
                                                confidence=Confidence.HIGH,
                                                review_policy=None,
                                                metadata={"query_id": "query-a"},
                                        ),
                                ),
                                trajectory_points=(),
                                side_artifacts={},
                                errors=(),
                        )

        binding = ChannelBinding(
                config=ChannelConfig(channel="kusto", enabled=True, discovery_threshold_hours=24, ttl_days=30),
                discovery_provider=_DiscoveryProvider(),
                hydration_provider=_HydrationProvider(),
                signal_extractor=_SignalExtractor(),
                discovery_config=KustoDiscoveryConfig(programs_root=tmp_path),
                hydration_config=KustoHydrationConfig(programs_root=tmp_path),
        )
        monkeypatch.setattr("src.commands.channel_wiring.resolve_channel_bindings", lambda *args, **kwargs: (binding,))

        query_states: dict[str, dict[str, object]] = {}
        signals, api_calls = gather._load_kusto_signals_via_uil(
                program,
                (),
                current_time,
                programs_root=tmp_path,
                integration_error_sink=[],
                query_state_sink=query_states,
                previous_query_states={},
        )

        assert api_calls == 1
        assert len(signals) == 1
        assert query_states["query-a"]["last_cycle_succeeded"] is True
        assert query_states["query-a"]["row_count"] == 1
        assert query_states["query-a"]["data_freshness_ok"] is True


def test_load_kusto_signals_via_uil_skips_refresh_on_gather_queries(monkeypatch, tmp_path: Path) -> None:
        from src.core.integration_types import ChannelBinding, ChannelConfig
        from src.core.kusto_discovery import KustoDiscoveryConfig, KustoDiscoveryProvider
        from src.core.kusto_hydration import KustoHydrationConfig, KustoHydrationProvider
        from src.core.kusto_signal_extractor import KustoSignalExtractor

        program = Program(
                schema_version="3.0",
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
                kusto=KustoConfig(enabled=True),
        )
        query = KustoQuery(
                id="query-a",
                cluster="https://cluster",
                database="db",
                kql="StormEvents | take 1",
                section="A",
                render_as="table",
                confidence="high",
                workstream_ids=("demo.slice",),
                validated=True,
                refresh_on_gather=True,
        )
        query_loader = lambda program_id, programs_root: (query,)
        binding = ChannelBinding(
                config=ChannelConfig(channel="kusto", enabled=True, discovery_threshold_hours=24, ttl_days=30),
                discovery_provider=KustoDiscoveryProvider(query_loader=query_loader),
                hydration_provider=KustoHydrationProvider(
                        executor=lambda rendered_query: [{"Value": 1}],
                        query_loader=query_loader,
                ),
                signal_extractor=KustoSignalExtractor(),
                discovery_config=KustoDiscoveryConfig(programs_root=tmp_path),
                hydration_config=KustoHydrationConfig(programs_root=tmp_path),
        )
        monkeypatch.setattr("src.commands.channel_wiring.resolve_channel_bindings", lambda *args, **kwargs: (binding,))

        signals, api_calls = gather._load_kusto_signals_via_uil(
                program,
                (),
                datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc),
                programs_root=tmp_path,
                integration_error_sink=[],
        )

        assert signals == ()
        assert api_calls == 0


def test_resolve_uil_channel_binding_for_gather_requires_explicit_channel_config(monkeypatch, tmp_path: Path) -> None:
        program = Program(
                schema_version="3.0",
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
                kusto=KustoConfig(enabled=True),
        )

        monkeypatch.setenv("VERTEX_UIL_ADO", "1")
        monkeypatch.setenv("VERTEX_UIL_KUSTO", "1")

        assert gather._resolve_uil_channel_binding_for_gather(program, (), "ado", programs_root=tmp_path) is None
        assert gather._resolve_uil_channel_binding_for_gather(program, (), "kusto", programs_root=tmp_path) is None


def test_run_channel_sanitizes_titles_and_enriches_workstream_ids(monkeypatch, tmp_path: Path) -> None:
        from src.core.channel_registry_store import ChannelRegistryStore
        from src.core.integration_types import (
            ADOHydrationOutput,
            ChannelBinding,
            ChannelConfig,
            ChannelRegistration,
            DiscoveredRef,
            DiscoveryCompleteness,
            DiscoveryResult,
            HydrationResult,
            RegistrationBinding,
            RegistrationStatus,
            RunContext,
        )

        current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
        monkeypatch.setattr("src.commands.gather_pipeline.support.filter_text", lambda text: "SCRUBBED" if text else text)
        store = ChannelRegistryStore(tmp_path / "demo" / "channel_registry.sqlite3", "demo")
        item = WorkItem(
            id=101,
            type="Feature",
            title="Hydrated",
            state="Active",
            assigned_to="Owner",
            assigned_to_email="owner@example.com",
            area_path="One\\Demo",
            iteration_path="One\\Iteration",
            target_date=None,
            risk_level=RiskLevel.UNKNOWN,
            tags=["RAMPP1"],
            custom_fields={},
            fetched_at=current_time,
        )

        class _DiscoveryProvider:
            def discover(self, program_id, config, existing, run_ctx=None):
                del config, existing, run_ctx
                registration = ChannelRegistration(
                    channel="ado",
                    program_id=program_id,
                    provider_instance_id="default",
                    ref_id="101",
                    ref_kind="work_item",
                    status=RegistrationStatus.ACTIVE,
                    first_discovered_at=current_time,
                    last_seen_at=current_time,
                    ref_title="secret@example.com",
                )
                return DiscoveryResult(
                    channel="ado",
                    program_id=program_id,
                    discovered_refs=(
                        DiscoveredRef(
                            registration=registration,
                            bindings=(
                                RegistrationBinding(
                                    workstream_id="demo.slice",
                                    scope_id="scope",
                                    source_type="wiql_saved_query",
                                    confidence=1.0,
                                    confidence_source="wiql_saved_query",
                                ),
                            ),
                        ),
                    ),
                    completeness=DiscoveryCompleteness.FULL,
                    scope_statuses={},
                    scope_state_updates={},
                    errors=(),
                    computed_at=current_time,
                )

        class _HydrationProvider:
            def hydrate(self, registrations, since, program_id, config, mode=None, run_ctx=None):
                del registrations, since, program_id, config, mode, run_ctx
                return HydrationResult(
                    channel="ado",
                    resources=ADOHydrationOutput(work_items=(item,), freshness_items=(item,)),
                    api_call_count=1,
                    errors=(),
                    hydrated_ref_ids=(("101", "work_item"),),
                    failed_ref_ids=(),
                )

        binding = ChannelBinding(
            config=ChannelConfig(channel="ado", enabled=True, discovery_threshold_hours=24, ttl_days=30),
            discovery_provider=_DiscoveryProvider(),
            hydration_provider=_HydrationProvider(),
            signal_extractor=object(),
            discovery_config=object(),
            hydration_config=object(),
        )

        hydration_result, _ = gather._run_channel(
            binding,
            store,
            program_id="demo",
            since=current_time - timedelta(days=14),
            verified_at=current_time,
            run_ctx=RunContext(),
            integration_error_sink=[],
        )

        assert hydration_result is not None
        assert hydration_result.resources.work_items[0].custom_fields["workstream_ids"] == ("demo.slice",)
        assert store.active_registrations("ado")[0].ref_title == "SCRUBBED"


def test_run_channel_dry_run_computes_delta_without_registry_writes(monkeypatch, tmp_path: Path) -> None:
        from src.core.channel_registry_store import ChannelRegistryStore
        from src.core.integration_types import (
            ADOHydrationOutput,
            ChannelBinding,
            ChannelConfig,
            ChannelRegistration,
            DiscoveredRef,
            DiscoveryCompleteness,
            DiscoveryResult,
            HydrationResult,
            RegistrationBinding,
            RegistrationStatus,
            RunContext,
        )

        current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
        store = ChannelRegistryStore(tmp_path / "demo" / "channel_registry.sqlite3", "demo")
        item = WorkItem(
            id=101,
            type="Feature",
            title="Hydrated",
            state="Active",
            assigned_to="Owner",
            assigned_to_email="owner@example.com",
            area_path="One\\Demo",
            iteration_path="One\\Iteration",
            target_date=None,
            risk_level=RiskLevel.UNKNOWN,
            tags=["RAMPP1"],
            custom_fields={},
            fetched_at=current_time,
        )

        class _DiscoveryProvider:
            def discover(self, program_id, config, existing, run_ctx=None):
                del config, existing, run_ctx
                registration = ChannelRegistration(
                    channel="ado",
                    program_id=program_id,
                    provider_instance_id="default",
                    ref_id="101",
                    ref_kind="work_item",
                    status=RegistrationStatus.ACTIVE,
                    first_discovered_at=current_time,
                    last_seen_at=current_time,
                    ref_title="Hydrated item",
                )
                return DiscoveryResult(
                    channel="ado",
                    program_id=program_id,
                    discovered_refs=(
                        DiscoveredRef(
                            registration=registration,
                            bindings=(
                                RegistrationBinding(
                                    workstream_id="demo.slice",
                                    scope_id="scope",
                                    source_type="wiql_saved_query",
                                    confidence=1.0,
                                    confidence_source="wiql_saved_query",
                                ),
                            ),
                        ),
                    ),
                    completeness=DiscoveryCompleteness.FULL,
                    scope_statuses={},
                    scope_state_updates={},
                    errors=(),
                    computed_at=current_time,
                )

        seen_registrations: list[tuple[object, ...]] = []

        class _HydrationProvider:
            def hydrate(self, registrations, since, program_id, config, mode=None, run_ctx=None):
                del since, program_id, config, mode, run_ctx
                seen_registrations.append(tuple(registrations))
                return HydrationResult(
                    channel="ado",
                    resources=ADOHydrationOutput(work_items=(item,), freshness_items=(item,)),
                    api_call_count=1,
                    errors=(),
                    hydrated_ref_ids=(("101", "work_item"),),
                    failed_ref_ids=(("101", "work_item"),),
                )

        binding = ChannelBinding(
            config=ChannelConfig(channel="ado", enabled=True, discovery_threshold_hours=24, ttl_days=30),
            discovery_provider=_DiscoveryProvider(),
            hydration_provider=_HydrationProvider(),
            signal_extractor=object(),
            discovery_config=object(),
            hydration_config=object(),
        )

        mark_verified_calls: list[tuple[tuple[str, str], ...]] = []
        mark_failed_calls: list[tuple[tuple[str, str], ...]] = []
        monkeypatch.setattr(store, "mark_verified", lambda channel, ref_id_kind_pairs, verified_at: mark_verified_calls.append(ref_id_kind_pairs))
        monkeypatch.setattr(store, "mark_hydration_failed", lambda channel, ref_id_kind_pairs: mark_failed_calls.append(ref_id_kind_pairs))

        errors: list[IntegrationError] = []
        hydration_result, delta = gather._run_channel(
            binding,
            store,
            program_id="demo",
            since=current_time - timedelta(days=14),
            verified_at=current_time,
            run_ctx=RunContext(dry_run=True, force_discovery=True),
            integration_error_sink=errors,
        )

        assert hydration_result is not None
        assert delta is not None
        assert delta.summary == "+1 -0 ~0 =0"
        assert store.registration_count("ado") == 0
        assert seen_registrations == [()]
        assert mark_verified_calls == []
        assert mark_failed_calls == []
        assert len(errors) == 1
        assert errors[0].stage == "hydration"
        assert errors[0].message == "Failed to hydrate work_item:101"


def test_run_channel_with_extraction_surfaces_extractor_errors(monkeypatch, tmp_path: Path) -> None:
        from src.core.channel_registry_store import ChannelRegistryStore
        from src.core.integration_types import (
            ADOHydrationOutput,
            ChannelBinding,
            ChannelConfig,
            ChannelRegistration,
            DiscoveredRef,
            DiscoveryCompleteness,
            DiscoveryResult,
            ExtractionResult,
            HydrationResult,
            IntegrationError,
            RegistrationBinding,
            RegistrationStatus,
            RunContext,
        )

        current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
        store = ChannelRegistryStore(tmp_path / "demo" / "channel_registry.sqlite3", "demo")
        item = WorkItem(
            id=101,
            type="Feature",
            title="Hydrated",
            state="Active",
            assigned_to="Owner",
            assigned_to_email="owner@example.com",
            area_path="One\\Demo",
            iteration_path="One\\Iteration",
            target_date=None,
            risk_level=RiskLevel.UNKNOWN,
            tags=["RAMPP1"],
            custom_fields={},
            fetched_at=current_time,
        )

        class _DiscoveryProvider:
            def discover(self, program_id, config, existing, run_ctx=None):
                del config, existing, run_ctx
                registration = ChannelRegistration(
                    channel="ado",
                    program_id=program_id,
                    provider_instance_id="default",
                    ref_id="101",
                    ref_kind="work_item",
                    status=RegistrationStatus.ACTIVE,
                    first_discovered_at=current_time,
                    last_seen_at=current_time,
                    ref_title="Hydrated item",
                )
                return DiscoveryResult(
                    channel="ado",
                    program_id=program_id,
                    discovered_refs=(
                        DiscoveredRef(
                            registration=registration,
                            bindings=(
                                RegistrationBinding(
                                    workstream_id="demo.slice",
                                    scope_id="scope",
                                    source_type="wiql_saved_query",
                                    confidence=1.0,
                                    confidence_source="wiql_saved_query",
                                ),
                            ),
                        ),
                    ),
                    completeness=DiscoveryCompleteness.FULL,
                    scope_statuses={},
                    scope_state_updates={},
                    errors=(),
                    computed_at=current_time,
                )

        class _HydrationProvider:
            def hydrate(self, registrations, since, program_id, config, mode=None, run_ctx=None):
                del registrations, since, program_id, config, mode, run_ctx
                return HydrationResult(
                    channel="ado",
                    resources=ADOHydrationOutput(work_items=(item,), freshness_items=(item,)),
                    api_call_count=1,
                    errors=(),
                    hydrated_ref_ids=(("101", "work_item"),),
                    failed_ref_ids=(),
                )

        class _SignalExtractor:
            def extract(self, resources, program_id):
                del resources, program_id
                return ExtractionResult(
                    channel="ado",
                    signals=(),
                    trajectory_points=(),
                    side_artifacts={},
                    errors=(
                        IntegrationError(
                            source="ado",
                            stage="extract",
                            retryable=False,
                            message="extractor failed",
                        ),
                    ),
                )

        binding = ChannelBinding(
            config=ChannelConfig(channel="ado", enabled=True, discovery_threshold_hours=24, ttl_days=30),
            discovery_provider=_DiscoveryProvider(),
            hydration_provider=_HydrationProvider(),
            signal_extractor=_SignalExtractor(),
            discovery_config=object(),
            hydration_config=object(),
        )

        errors: list[IntegrationError] = []
        hydration_result, extraction_result, delta = gather._run_channel_with_extraction(
            binding,
            store,
            program_id="demo",
            since=current_time - timedelta(days=14),
            verified_at=current_time,
            run_ctx=RunContext(),
            integration_error_sink=errors,
        )

        assert hydration_result is not None
        assert extraction_result is not None
        assert delta is not None
        assert extraction_result.errors[0].message == "extractor failed"
        assert len(errors) == 1
        assert errors[0].stage == "extract"
        assert errors[0].message == "extractor failed"


def test_run_channel_records_scope_health_when_shrinkage_guard_blocks_update(tmp_path: Path) -> None:
        from src.core.channel_registry_store import ChannelRegistryStore
        from src.core.integration_types import (
            ADOHydrationOutput,
            ChannelBinding,
            ChannelConfig,
            ChannelRegistration,
            DiscoveredRef,
            DiscoveryCompleteness,
            DiscoveryResult,
            HydrationResult,
            RegistrationBinding,
            RegistrationStatus,
            RunContext,
            ScopeStatus,
            ScopeStatusKind,
        )

        current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
        store = ChannelRegistryStore(tmp_path / "demo" / "channel_registry.sqlite3", "demo")
        seed_refs = tuple(
            DiscoveredRef(
                registration=ChannelRegistration(
                    channel="ado",
                    program_id="demo",
                    provider_instance_id="instance-a",
                    ref_id=str(ref_id),
                    ref_kind="work_item",
                    status=RegistrationStatus.ACTIVE,
                    first_discovered_at=current_time,
                    last_seen_at=current_time,
                ),
                bindings=(
                    RegistrationBinding(
                        workstream_id="demo.slice",
                        scope_id="scope",
                        source_type="wiql_saved_query",
                        confidence=1.0,
                        confidence_source="wiql_saved_query",
                    ),
                ),
            )
            for ref_id in range(100, 106)
        )
        store.apply_discovery_result(
            DiscoveryResult(
                channel="ado",
                program_id="demo",
                discovered_refs=seed_refs,
                completeness=DiscoveryCompleteness.FULL,
                scope_statuses={
                    "scope": ScopeStatus(
                        scope_id="scope",
                        status=ScopeStatusKind.SUCCESS,
                        completeness=DiscoveryCompleteness.FULL,
                        item_count=len(seed_refs),
                    )
                },
                scope_state_updates={},
                errors=(),
                computed_at=current_time - timedelta(hours=1),
            )
        )

        item = WorkItem(
            id=100,
            type="Feature",
            title="Hydrated",
            state="Active",
            assigned_to="Owner",
            assigned_to_email="owner@example.com",
            area_path="One\\Demo",
            iteration_path="One\\Iteration",
            target_date=None,
            risk_level=RiskLevel.UNKNOWN,
            tags=["RAMPP1"],
            custom_fields={},
            fetched_at=current_time,
        )

        class _DiscoveryProvider:
            def discover(self, program_id, config, existing, run_ctx=None):
                del config, existing, run_ctx
                registration = ChannelRegistration(
                    channel="ado",
                    program_id=program_id,
                    provider_instance_id="instance-a",
                    ref_id="100",
                    ref_kind="work_item",
                    status=RegistrationStatus.ACTIVE,
                    first_discovered_at=current_time,
                    last_seen_at=current_time,
                )
                return DiscoveryResult(
                    channel="ado",
                    program_id=program_id,
                    discovered_refs=(
                        DiscoveredRef(
                            registration=registration,
                            bindings=(
                                RegistrationBinding(
                                    workstream_id="demo.slice",
                                    scope_id="scope",
                                    source_type="wiql_saved_query",
                                    confidence=1.0,
                                    confidence_source="wiql_saved_query",
                                ),
                            ),
                        ),
                    ),
                    completeness=DiscoveryCompleteness.FULL,
                    scope_statuses={
                        "scope": ScopeStatus(
                            scope_id="scope",
                            status=ScopeStatusKind.ERROR,
                            completeness=DiscoveryCompleteness.PARTIAL,
                            item_count=1,
                            error_message="partial failure",
                        )
                    },
                    scope_state_updates={},
                    errors=(),
                    computed_at=current_time,
                )

        class _HydrationProvider:
            def hydrate(self, registrations, since, program_id, config, mode=None, run_ctx=None):
                del registrations, since, program_id, config, mode, run_ctx
                return HydrationResult(
                    channel="ado",
                    resources=ADOHydrationOutput(work_items=(item,), freshness_items=(item,)),
                    api_call_count=1,
                    errors=(),
                    hydrated_ref_ids=(("100", "work_item"),),
                    failed_ref_ids=(),
                )

        binding = ChannelBinding(
            config=ChannelConfig(
                channel="ado",
                enabled=True,
                discovery_threshold_hours=24,
                ttl_days=30,
                extra={"instance_id": "instance-a"},
            ),
            discovery_provider=_DiscoveryProvider(),
            hydration_provider=_HydrationProvider(),
            signal_extractor=object(),
            discovery_config=object(),
            hydration_config=object(),
        )

        hydration_result, delta = gather._run_channel(
            binding,
            store,
            program_id="demo",
            since=current_time - timedelta(days=14),
            verified_at=current_time,
            run_ctx=RunContext(force_discovery=True),
            integration_error_sink=[],
        )

        assert hydration_result is not None
        assert delta is not None
        assert store.recent_scope_health("ado", provider_instance_id="instance-a") == {"scope": "error_1x"}


def test_run_channel_uses_provider_instance_for_discovery_staleness(tmp_path: Path) -> None:
        from src.core.channel_registry_store import ChannelRegistryStore
        from src.core.integration_types import (
                ADOHydrationOutput,
                ChannelBinding,
                ChannelConfig,
                ChannelRegistration,
                DiscoveredRef,
                DiscoveryCompleteness,
                DiscoveryResult,
                HydrationResult,
                RegistrationBinding,
                RegistrationStatus,
                RunContext,
        )

        current_time = datetime.now(timezone.utc)
        store = ChannelRegistryStore(tmp_path / "demo" / "channel_registry.sqlite3", "demo")
        store.apply_discovery_result(
                DiscoveryResult(
                        channel="ado",
                        program_id="demo",
                        discovered_refs=(
                                DiscoveredRef(
                                        registration=ChannelRegistration(
                                                channel="ado",
                                                program_id="demo",
                                                provider_instance_id="default",
                                                ref_id="100",
                                                ref_kind="work_item",
                                                status=RegistrationStatus.ACTIVE,
                                                first_discovered_at=current_time,
                                                last_seen_at=current_time,
                                                ref_title="Default item",
                                        ),
                                        bindings=(
                                                RegistrationBinding(
                                                        workstream_id="demo.slice",
                                                        scope_id="scope-default",
                                                        source_type="wiql_saved_query",
                                                        confidence=1.0,
                                                        confidence_source="wiql_saved_query",
                                                ),
                                        ),
                                ),
                        ),
                        completeness=DiscoveryCompleteness.FULL,
                        scope_statuses={},
                        scope_state_updates={},
                        errors=(),
                        computed_at=current_time,
                )
        )

        discovery_calls: list[str] = []

        class _DiscoveryProvider:
                def discover(self, program_id, config, existing, run_ctx=None):
                        del config, run_ctx
                        discovery_calls.append(program_id)
                        assert existing == ()
                        return DiscoveryResult(
                                channel="ado",
                                program_id=program_id,
                                discovered_refs=(
                                        DiscoveredRef(
                                                registration=ChannelRegistration(
                                                        channel="ado",
                                                        program_id=program_id,
                                                        provider_instance_id="instance-a",
                                                        ref_id="101",
                                                        ref_kind="work_item",
                                                        status=RegistrationStatus.ACTIVE,
                                                        first_discovered_at=current_time,
                                                        last_seen_at=current_time,
                                                        ref_title="Instance A item",
                                                ),
                                                bindings=(
                                                        RegistrationBinding(
                                                                workstream_id="demo.slice",
                                                                scope_id="scope-a",
                                                                source_type="wiql_saved_query",
                                                                confidence=1.0,
                                                                confidence_source="wiql_saved_query",
                                                        ),
                                                ),
                                        ),
                                ),
                                completeness=DiscoveryCompleteness.FULL,
                                scope_statuses={},
                                scope_state_updates={},
                                errors=(),
                                computed_at=current_time,
                        )

        class _HydrationProvider:
                def hydrate(self, registrations, since, program_id, config, mode=None, run_ctx=None):
                        del since, program_id, config, mode, run_ctx
                        assert tuple((registration.ref_id, registration.provider_instance_id) for registration in registrations) == (
                                ("101", "instance-a"),
                        )
                        item = WorkItem(
                                id=101,
                                type="Feature",
                                title="Hydrated",
                                state="Active",
                                assigned_to="Owner",
                                assigned_to_email="owner@example.com",
                                area_path="One\\Demo",
                                iteration_path="One\\Iteration",
                                target_date=None,
                                risk_level=RiskLevel.UNKNOWN,
                                tags=["RAMPP1"],
                                custom_fields={},
                                fetched_at=current_time,
                        )
                        return HydrationResult(
                                channel="ado",
                                resources=ADOHydrationOutput(work_items=(item,), freshness_items=(item,)),
                                api_call_count=1,
                                errors=(),
                                hydrated_ref_ids=(("101", "work_item"),),
                                failed_ref_ids=(),
                        )

        binding = ChannelBinding(
                config=ChannelConfig(
                        channel="ado",
                        enabled=True,
                        discovery_threshold_hours=24,
                        ttl_days=30,
                        extra={"instance_id": "instance-a"},
                ),
                discovery_provider=_DiscoveryProvider(),
                hydration_provider=_HydrationProvider(),
                signal_extractor=object(),
                discovery_config=object(),
                hydration_config=object(),
        )

        hydration_result, delta = gather._run_channel(
                binding,
                store,
                program_id="demo",
                since=current_time - timedelta(days=14),
                verified_at=current_time,
                run_ctx=RunContext(),
                integration_error_sink=[],
        )

        assert hydration_result is not None
        assert delta is not None
        assert discovery_calls == ["demo"]
        assert store.registration_count("ado", provider_instance_id="default") == 1
        assert store.registration_count("ado", provider_instance_id="instance-a") == 1


def test_run_channel_normalizes_empty_result_to_binding_provider_instance(tmp_path: Path) -> None:
        from src.core.channel_registry_store import ChannelRegistryStore
        from src.core.integration_types import (
                ADOHydrationOutput,
                ChannelBinding,
                ChannelConfig,
                ChannelRegistration,
                DiscoveredRef,
                DiscoveryCompleteness,
                DiscoveryResult,
                HydrationResult,
                RegistrationBinding,
                RegistrationStatus,
                RunContext,
        )

        current_time = datetime.now(timezone.utc)
        store = ChannelRegistryStore(tmp_path / "demo" / "channel_registry.sqlite3", "demo")
        store.apply_discovery_result(
                DiscoveryResult(
                        channel="ado",
                        program_id="demo",
                        discovered_refs=(
                                DiscoveredRef(
                                        registration=ChannelRegistration(
                                                channel="ado",
                                                program_id="demo",
                                                provider_instance_id="instance-a",
                                                ref_id="101",
                                                ref_kind="work_item",
                                                status=RegistrationStatus.ACTIVE,
                                                first_discovered_at=current_time,
                                                last_seen_at=current_time,
                                                ref_title="Instance A item",
                                        ),
                                        bindings=(
                                                RegistrationBinding(
                                                        workstream_id="demo.slice",
                                                        scope_id="scope-a",
                                                        source_type="wiql_saved_query",
                                                        confidence=1.0,
                                                        confidence_source="wiql_saved_query",
                                                ),
                                        ),
                                ),
                        ),
                        completeness=DiscoveryCompleteness.FULL,
                        scope_statuses={},
                        scope_state_updates={},
                        errors=(),
                        computed_at=current_time,
                        provider_instance_id="instance-a",
                )
        )

        class _DiscoveryProvider:
                def discover(self, program_id, config, existing, run_ctx=None):
                        del program_id, config, existing, run_ctx
                        return DiscoveryResult(
                                channel="ado",
                                program_id="demo",
                                discovered_refs=(),
                                completeness=DiscoveryCompleteness.FULL,
                                scope_statuses={},
                                scope_state_updates={},
                                errors=(),
                                computed_at=current_time + timedelta(minutes=5),
                        )

        class _HydrationProvider:
                def hydrate(self, registrations, since, program_id, config, mode=None, run_ctx=None):
                        del since, program_id, config, mode, run_ctx
                        assert registrations == ()
                        return HydrationResult(
                                channel="ado",
                                resources=ADOHydrationOutput(work_items=(), freshness_items=()),
                                api_call_count=0,
                                errors=(),
                                hydrated_ref_ids=(),
                                failed_ref_ids=(),
                        )

        binding = ChannelBinding(
                config=ChannelConfig(
                        channel="ado",
                        enabled=True,
                        discovery_threshold_hours=24,
                        ttl_days=30,
                        extra={"instance_id": "instance-a"},
                ),
                discovery_provider=_DiscoveryProvider(),
                hydration_provider=_HydrationProvider(),
                signal_extractor=object(),
                discovery_config=object(),
                hydration_config=object(),
        )

        hydration_result, delta = gather._run_channel(
                binding,
                store,
                program_id="demo",
                since=current_time - timedelta(days=14),
                verified_at=current_time,
                run_ctx=RunContext(force_discovery=True),
                integration_error_sink=[],
        )

        assert hydration_result is not None
        assert delta is not None
        assert delta.summary == "+0 -1 ~0 =0"
        assert store.active_registrations("ado", provider_instance_id="instance-a") == ()


def test_run_channel_marks_hydration_results_for_one_provider_instance_only(tmp_path: Path) -> None:
        from src.core.channel_registry_store import ChannelRegistryStore
        from src.core.integration_types import (
                ADOHydrationOutput,
                ChannelBinding,
                ChannelConfig,
                ChannelRegistration,
                DiscoveredRef,
                DiscoveryCompleteness,
                DiscoveryResult,
                HydrationResult,
                RegistrationBinding,
                RegistrationStatus,
                RunContext,
        )

        current_time = datetime.now(timezone.utc)
        store = ChannelRegistryStore(tmp_path / "demo" / "channel_registry.sqlite3", "demo")
        for provider_instance_id in ("default", "instance-a"):
                store.apply_discovery_result(
                        DiscoveryResult(
                                channel="ado",
                                program_id="demo",
                                discovered_refs=(
                                        DiscoveredRef(
                                                registration=ChannelRegistration(
                                                        channel="ado",
                                                        program_id="demo",
                                                        provider_instance_id=provider_instance_id,
                                                        ref_id="101",
                                                        ref_kind="work_item",
                                                        status=RegistrationStatus.ACTIVE,
                                                        first_discovered_at=current_time,
                                                        last_seen_at=current_time,
                                                        ref_title=f"{provider_instance_id} item",
                                                ),
                                                bindings=(
                                                        RegistrationBinding(
                                                                workstream_id="demo.slice",
                                                                scope_id=f"scope-{provider_instance_id}",
                                                                source_type="wiql_saved_query",
                                                                confidence=1.0,
                                                                confidence_source="wiql_saved_query",
                                                        ),
                                                ),
                                        ),
                                ),
                                completeness=DiscoveryCompleteness.FULL,
                                scope_statuses={},
                                scope_state_updates={},
                                errors=(),
                                computed_at=current_time,
                        )
                )

        class _DiscoveryProvider:
                def discover(self, program_id, config, existing, run_ctx=None):
                        del program_id, config, existing, run_ctx
                        raise AssertionError("discovery should be skipped when provider instance is fresh")

        class _HydrationProvider:
                def hydrate(self, registrations, since, program_id, config, mode=None, run_ctx=None):
                        del since, program_id, config, mode, run_ctx
                        assert tuple((registration.ref_id, registration.provider_instance_id) for registration in registrations) == (
                                ("101", "instance-a"),
                        )
                        item = WorkItem(
                                id=101,
                                type="Feature",
                                title="Hydrated",
                                state="Active",
                                assigned_to="Owner",
                                assigned_to_email="owner@example.com",
                                area_path="One\\Demo",
                                iteration_path="One\\Iteration",
                                target_date=None,
                                risk_level=RiskLevel.UNKNOWN,
                                tags=["RAMPP1"],
                                custom_fields={},
                                fetched_at=current_time,
                        )
                        return HydrationResult(
                                channel="ado",
                                resources=ADOHydrationOutput(work_items=(item,), freshness_items=(item,)),
                                api_call_count=1,
                                errors=(),
                                hydrated_ref_ids=(("101", "work_item"),),
                                failed_ref_ids=(),
                        )

        binding = ChannelBinding(
                config=ChannelConfig(
                        channel="ado",
                        enabled=True,
                        discovery_threshold_hours=24,
                        ttl_days=30,
                        extra={"instance_id": "instance-a"},
                ),
                discovery_provider=_DiscoveryProvider(),
                hydration_provider=_HydrationProvider(),
                signal_extractor=object(),
                discovery_config=object(),
                hydration_config=object(),
        )

        hydration_result, delta = gather._run_channel(
                binding,
                store,
                program_id="demo",
                since=current_time - timedelta(days=14),
                verified_at=current_time + timedelta(minutes=15),
                run_ctx=RunContext(),
                integration_error_sink=[],
        )

        assert hydration_result is not None
        assert delta is None
        default_registration = store.all_registrations("ado", provider_instance_id="default")[0]
        instance_a_registration = store.all_registrations("ado", provider_instance_id="instance-a")[0]
        assert default_registration.last_verified_at is None
        assert instance_a_registration.last_verified_at == current_time + timedelta(minutes=15)


def test_build_uil_ado_channel_state_reports_registry_health(monkeypatch, tmp_path: Path) -> None:
        from src.core.channel_registry_store import ChannelRegistryStore
        from src.core.integration_types import (
                ChannelRegistration,
                DiscoveredRef,
                DiscoveryCompleteness,
                DiscoveryResult,
                RegistrationBinding,
                RegistrationStatus,
        )

        current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
        program_dir = tmp_path / "demo"
        program_dir.mkdir(parents=True)
        (program_dir / "program.yaml").write_text(
                "\n".join(
                        [
                                "schema_version: '3.0'",
                                "id: demo",
                                "name: Demo",
                                "channels:",
                                "  ado:",
                                "    enabled: true",
                                "    discovery_threshold_hours: 24",
                                "    ttl_days: 30",
                        ]
                ),
                encoding="utf-8",
        )
        store = ChannelRegistryStore(tmp_path / "demo" / "channel_registry.sqlite3", "demo")
        registration = ChannelRegistration(
                channel="ado",
                program_id="demo",
                provider_instance_id="default",
                ref_id="101",
                ref_kind="work_item",
                status=RegistrationStatus.ACTIVE,
                first_discovered_at=current_time,
                last_seen_at=current_time,
        )
        store.apply_discovery_result(
                DiscoveryResult(
                        channel="ado",
                        program_id="demo",
                        discovered_refs=(
                                DiscoveredRef(
                                        registration=registration,
                                        bindings=(
                                                RegistrationBinding(
                                                        workstream_id="demo.slice",
                                                        scope_id="scope",
                                                        source_type="wiql_saved_query",
                                                        confidence=1.0,
                                                        confidence_source="wiql_saved_query",
                                                ),
                                        ),
                                ),
                        ),
                        completeness=DiscoveryCompleteness.FULL,
                        scope_statuses={},
                        scope_state_updates={},
                        errors=(),
                        computed_at=current_time,
                )
        )
        monkeypatch.setenv("VERTEX_UIL_ADO", "1")

        state = gather._build_uil_ado_channel_state("demo", programs_root=tmp_path)

        assert state["uil_enabled"] is True
        assert state["uil_registry_file_present"] is True
        assert state["uil_health"] == "ok"
        assert state["uil_registry_size"] == 1
        assert state["uil_last_delta_summary"] == "+1 -0 ~0 =0"


def test_build_uil_channel_state_uses_configured_provider_instance(monkeypatch, tmp_path: Path) -> None:
        from src.core.channel_registry_store import ChannelRegistryStore
        from src.core.integration_types import (
                ChannelRegistration,
                DiscoveredRef,
                DiscoveryCompleteness,
                DiscoveryResult,
                RegistrationBinding,
                RegistrationStatus,
                ScopeStatus,
                ScopeStatusKind,
        )

        program_dir = tmp_path / "demo"
        program_dir.mkdir(parents=True)
        (program_dir / "program.yaml").write_text(
                "\n".join(
                        [
                                "schema_version: '3.0'",
                                "id: demo",
                                "name: Demo",
                                "channels:",
                                "  ado:",
                                "    enabled: true",
                                "    discovery_threshold_hours: 24",
                                "    ttl_days: 30",
                                "    extra:",
                                "      instance_id: instance-a",
                        ]
                ),
                encoding="utf-8",
        )
        store = ChannelRegistryStore(program_dir / "channel_registry.sqlite3", "demo")
        current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)

        store.apply_discovery_result(
                DiscoveryResult(
                        channel="ado",
                        program_id="demo",
                        discovered_refs=(
                                DiscoveredRef(
                                        registration=ChannelRegistration(
                                                channel="ado",
                                                program_id="demo",
                                                provider_instance_id="instance-a",
                                                ref_id="101",
                                                ref_kind="work_item",
                                                status=RegistrationStatus.ACTIVE,
                                                first_discovered_at=current_time,
                                                last_seen_at=current_time,
                                                ref_title="Instance A item 1",
                                        ),
                                        bindings=(
                                                RegistrationBinding(
                                                        workstream_id="demo.slice",
                                                        scope_id="scope-a",
                                                        source_type="wiql_saved_query",
                                                        confidence=1.0,
                                                        confidence_source="wiql_saved_query",
                                                ),
                                        ),
                                ),
                                DiscoveredRef(
                                        registration=ChannelRegistration(
                                                channel="ado",
                                                program_id="demo",
                                                provider_instance_id="instance-a",
                                                ref_id="102",
                                                ref_kind="work_item",
                                                status=RegistrationStatus.ACTIVE,
                                                first_discovered_at=current_time,
                                                last_seen_at=current_time,
                                                ref_title="Instance A item 2",
                                        ),
                                        bindings=(
                                                RegistrationBinding(
                                                        workstream_id="demo.slice",
                                                        scope_id="scope-a",
                                                        source_type="wiql_saved_query",
                                                        confidence=1.0,
                                                        confidence_source="wiql_saved_query",
                                                ),
                                        ),
                                ),
                        ),
                        completeness=DiscoveryCompleteness.FULL,
                        scope_statuses={
                                "scope-a": ScopeStatus(
                                        scope_id="scope-a",
                                        status=ScopeStatusKind.SUCCESS,
                                        completeness=DiscoveryCompleteness.FULL,
                                        item_count=2,
                                )
                        },
                        scope_state_updates={},
                        errors=(),
                        computed_at=current_time,
                ),
                ttl_days=30,
        )
        later_time = current_time + timedelta(minutes=5)
        store.apply_discovery_result(
                DiscoveryResult(
                        channel="ado",
                        program_id="demo",
                        discovered_refs=(
                                DiscoveredRef(
                                        registration=ChannelRegistration(
                                                channel="ado",
                                                program_id="demo",
                                                provider_instance_id="default",
                                                ref_id="999",
                                                ref_kind="work_item",
                                                status=RegistrationStatus.ACTIVE,
                                                first_discovered_at=later_time,
                                                last_seen_at=later_time,
                                                ref_title="Default item",
                                        ),
                                        bindings=(
                                                RegistrationBinding(
                                                        workstream_id="demo.slice",
                                                        scope_id="scope-default",
                                                        source_type="wiql_saved_query",
                                                        confidence=1.0,
                                                        confidence_source="wiql_saved_query",
                                                ),
                                        ),
                                ),
                        ),
                        completeness=DiscoveryCompleteness.FULL,
                        scope_statuses={},
                        scope_state_updates={},
                        errors=(),
                        computed_at=later_time,
                ),
                ttl_days=30,
        )

        monkeypatch.setenv("VERTEX_UIL_ADO", "1")

        state = gather._build_uil_channel_state("demo", "ado", enabled=True, programs_root=tmp_path)

        assert state["uil_enabled"] is True
        assert state["uil_health"] == "ok"
        assert state["uil_registry_size"] == 2
        assert state["uil_last_delta_summary"] == "+2 -0 ~0 =0"
        assert state["uil_scope_health"] == {"scope-a": "ok"}


def test_build_uil_channel_state_requires_explicit_channel_config(monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setenv("VERTEX_UIL_ADO", "1")

        state = gather._build_uil_channel_state("demo", "ado", enabled=True, programs_root=tmp_path)

        assert state["uil_enabled"] is False
        assert state["uil_registry_file_present"] is False
        assert "uil_health" not in state


def test_build_uil_channel_state_reports_kusto_registry_health(monkeypatch, tmp_path: Path) -> None:
        from src.core.channel_registry_store import ChannelRegistryStore
        from src.core.integration_types import (
                ChannelRegistration,
                DiscoveredRef,
                DiscoveryCompleteness,
                DiscoveryResult,
                RegistrationBinding,
                RegistrationStatus,
        )

        current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
        program_dir = tmp_path / "demo"
        program_dir.mkdir(parents=True)
        (program_dir / "program.yaml").write_text(
                "\n".join(
                        [
                                "schema_version: '3.0'",
                                "id: demo",
                                "name: Demo",
                                "channels:",
                                "  kusto:",
                                "    enabled: true",
                                "    discovery_threshold_hours: 168",
                        ]
                ),
                encoding="utf-8",
        )
        store = ChannelRegistryStore(tmp_path / "demo" / "channel_registry.sqlite3", "demo")
        registration = ChannelRegistration(
                channel="kusto",
                program_id="demo",
                provider_instance_id="default",
                ref_id="query-a",
                ref_kind="kusto_query",
                status=RegistrationStatus.ACTIVE,
                first_discovered_at=current_time,
                last_seen_at=current_time,
        )
        store.apply_discovery_result(
                DiscoveryResult(
                        channel="kusto",
                        program_id="demo",
                        discovered_refs=(
                                DiscoveredRef(
                                        registration=registration,
                                        bindings=(
                                                RegistrationBinding(
                                                        workstream_id="demo.slice",
                                                        scope_id="query-a",
                                                        source_type="kusto_query",
                                                        confidence=1.0,
                                                        confidence_source="manual_config",
                                                ),
                                        ),
                                ),
                        ),
                        completeness=DiscoveryCompleteness.FULL,
                        scope_statuses={},
                        scope_state_updates={},
                        errors=(),
                        computed_at=current_time,
                ),
                ttl_days=30,
        )

        monkeypatch.setenv("VERTEX_UIL_KUSTO", "1")

        state = gather._build_uil_channel_state("demo", "kusto", enabled=True, programs_root=tmp_path)

        assert state["uil_enabled"] is True
        assert state["uil_registry_file_present"] is True
        assert state["uil_health"] == "ok"
        assert state["uil_registry_size"] == 1
        assert state["uil_last_delta_summary"] == "+1 -0 ~0 =0"


def test_gather_program_uses_uil_kusto_path_when_enabled(monkeypatch, tmp_path: Path) -> None:
        from src.core.integration_types import (
                ChannelBinding,
                ChannelConfig,
                ChannelRegistration,
                DiscoveredRef,
                DiscoveryCompleteness,
                DiscoveryResult,
                ExtractionResult,
                RegistrationBinding,
                RegistrationStatus,
        )
        from src.core.kusto_discovery import KustoDiscoveryConfig
        from src.core.kusto_hydration import KustoHydrationConfig

        programs_root = tmp_path / "programs"
        program_dir = programs_root / "demo"
        program_dir.mkdir(parents=True)
        (program_dir / "program.yaml").write_text(
                "\n".join(
                        [
                                "schema_version: '3.0'",
                                "id: demo",
                                "name: Demo",
                                "channels:",
                                "  kusto:",
                                "    enabled: true",
                                "    discovery_threshold_hours: 24",
                                "    ttl_days: 30",
                        ]
                ),
                encoding="utf-8",
        )
        monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "db"))
        monkeypatch.setenv("VERTEX_UIL_KUSTO", "1")
        current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)

        program = Program(
                schema_version="3.0",
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
                kusto=KustoConfig(enabled=True),
        )
        query = KustoQuery(
                id="query-a",
                cluster="https://cluster",
                database="db",
                kql="StormEvents | take 1",
                section="A",
                render_as="table",
                confidence="high",
                workstream_ids=("demo.slice",),
                validated=True,
                program_ids=("demo",),
        )

        class _DiscoveryProvider:
                def discover(self, program_id, config, existing, run_ctx=None):
                        del config, existing, run_ctx
                        registration = ChannelRegistration(
                                channel="kusto",
                                program_id=program_id,
                                provider_instance_id="default",
                                ref_id="query-a",
                                ref_kind="kusto_query",
                                status=RegistrationStatus.ACTIVE,
                                first_discovered_at=current_time,
                                last_seen_at=current_time,
                        )
                        return DiscoveryResult(
                                channel="kusto",
                                program_id=program_id,
                                discovered_refs=(
                                        DiscoveredRef(
                                                registration=registration,
                                                bindings=(
                                                        RegistrationBinding(
                                                                workstream_id="demo.slice",
                                                                scope_id="query-a",
                                                                source_type="kusto_query",
                                                                confidence=1.0,
                                                                confidence_source="manual_config",
                                                        ),
                                                ),
                                        ),
                                ),
                                completeness=DiscoveryCompleteness.FULL,
                                scope_statuses={},
                                scope_state_updates={},
                                errors=(),
                                computed_at=current_time,
                        )

        class _HydrationProvider:
                def __init__(self) -> None:
                        self._query_loader = lambda program_id, programs_root: (query,)
                        self._executor = lambda rendered_query: [{"Value": 1, "Timestamp": "2026-05-24T11:00:00Z"}]

        class _SignalExtractor:
                def extract(self, resources, program_id):
                        del resources
                        return ExtractionResult(
                                channel="kusto",
                                signals=(
                                        Signal(
                                                id="kusto/query-a/demo.slice",
                                                timestamp=current_time,
                                                source="kusto",
                                                program_id=program_id,
                                                workstream_id="demo.slice",
                                                entity_refs=("kusto:query-a",),
                                                text="Kusto query result.",
                                                raw_ref="kusto/query-a/demo.slice",
                                                confidence=Confidence.HIGH,
                                                review_policy=None,
                                                metadata={"query_id": "query-a"},
                                        ),
                                ),
                                trajectory_points=(),
                                side_artifacts={},
                                errors=(),
                        )

        binding = ChannelBinding(
                config=ChannelConfig(channel="kusto", enabled=True, discovery_threshold_hours=24, ttl_days=30),
                discovery_provider=_DiscoveryProvider(),
                hydration_provider=_HydrationProvider(),
                signal_extractor=_SignalExtractor(),
                discovery_config=KustoDiscoveryConfig(programs_root=programs_root),
                hydration_config=KustoHydrationConfig(programs_root=programs_root),
        )

        monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, ()))
        monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
        monkeypatch.setattr(
                gather,
                "load_program_knowledge",
                lambda program_id, programs_root: KnowledgeStore(
                        people_directory=(),
                        people_profiles=(),
                        teams=(),
                        products=(),
                        golden_queries=(query,),
                ),
        )
        monkeypatch.setattr("src.commands.channel_wiring.resolve_channel_bindings", lambda *args, **kwargs: (binding,))

        artifacts = gather.gather_program(
                "demo",
                as_of=current_time,
                programs_root=programs_root,
                loader=lambda program, workstreams, as_of, **_: ((), 0),
                freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
                include_kusto=True,
        )

        gather_state = load_gather_state("demo", programs_root=programs_root)

        assert artifacts.discovered_signals == 1
        assert gather_state is not None
        assert gather_state.channels["kusto"]["uil_enabled"] is True
        assert gather_state.channels["kusto"]["uil_registry_size"] == 1
        assert gather_state.query_states["query-a"]["last_cycle_succeeded"] is True
        assert gather_state.query_states["query-a"]["row_count"] == 1


def test_gather_program_falls_back_to_legacy_kusto_when_uil_env_enabled_but_channel_unconfigured(monkeypatch, tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "db"))
        monkeypatch.setenv("VERTEX_UIL_KUSTO", "1")
        current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)

        program = Program(
                schema_version="3.0",
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
                kusto=KustoConfig(enabled=True),
        )
        query = KustoQuery(
                id="query-a",
                cluster="https://cluster",
                database="db",
                kql="StormEvents | take 1",
                section="A",
                render_as="table",
                confidence="high",
                workstream_ids=("demo.slice",),
                validated=True,
                program_ids=("demo",),
        )
        legacy_signal = Signal(
                id="legacy-kusto/query-a/demo.slice",
                timestamp=current_time,
                source="kusto",
                program_id="demo",
                workstream_id="demo.slice",
                entity_refs=("kusto:query-a",),
                text="Legacy Kusto query result.",
                raw_ref="legacy-kusto/query-a/demo.slice",
                confidence=Confidence.HIGH,
                review_policy=None,
                metadata={"query_id": "query-a"},
        )

        monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, ()))
        monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
        monkeypatch.setattr(
                gather,
                "load_program_knowledge",
                lambda program_id, programs_root: KnowledgeStore(
                        people_directory=(),
                        people_profiles=(),
                        teams=(),
                        products=(),
                        golden_queries=(query,),
                ),
        )
        monkeypatch.setattr(gather, "_load_kusto_queries", lambda *args, **kwargs: (query,))
        monkeypatch.setattr(gather, "_load_refresh_kpi_queries", lambda *args, **kwargs: ())
        monkeypatch.setattr(gather, "_build_kusto_signals", lambda **kwargs: (legacy_signal,))
        monkeypatch.setattr(gather, "_build_kusto_kpi_signals", lambda **kwargs: ())
        monkeypatch.setattr(
                gather,
                "_load_kusto_signals_via_uil",
                lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("UIL Kusto path should not run without a configured channel")),
        )

        artifacts = gather.gather_program(
                "demo",
                as_of=current_time,
                programs_root=programs_root,
                loader=lambda program, workstreams, as_of, **_: ((), 0),
                freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
                include_kusto=True,
        )

        gather_state = load_gather_state("demo", programs_root=programs_root)

        assert artifacts.discovered_signals == 1
        assert gather_state is not None
        assert gather_state.channels["kusto"]["uil_enabled"] is False
        assert gather_state.query_states == {}


def test_gather_program_falls_back_to_legacy_ado_when_uil_env_enabled_but_channel_unconfigured(monkeypatch, tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "db"))
        monkeypatch.setenv("VERTEX_UIL_ADO", "1")
        current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)

        program = Program(
                schema_version="3.0",
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
        )
        item = WorkItem(
                id=101,
                type="Feature",
                title="Legacy ADO item",
                state="Active",
                assigned_to="Owner",
                assigned_to_email="owner@example.com",
                area_path="One\\Demo",
                iteration_path="One\\Iteration",
                target_date=None,
                risk_level=RiskLevel.UNKNOWN,
                tags=["RAMPP1"],
                custom_fields={},
                fetched_at=current_time,
        )

        monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, ()))
        monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
        monkeypatch.setattr(gather, "_load_dependency_program_items", lambda *args, **kwargs: ((), 0))

        artifacts = gather.gather_program(
                "demo",
                as_of=current_time,
                programs_root=programs_root,
        )

        gather_state = load_gather_state("demo", programs_root=programs_root)

        # With UIL env enabled but no channel configured (no program.yaml),
        # _resolve_uil_channel_binding_for_gather returns None → no items loaded.
        assert artifacts.scanned_items == 0
        assert artifacts.ado_calls == 0
        assert gather_state is not None
        assert gather_state.channels["ado"]["uil_enabled"] is False


def test_build_gather_channel_states_preserves_non_ado_uil_metadata_from_previous_state(monkeypatch, tmp_path: Path) -> None:
        program_dir = tmp_path / "demo"
        program_dir.mkdir(parents=True)
        (program_dir / "program.yaml").write_text(
                "\n".join(
                        [
                                "schema_version: '3.0'",
                                "id: demo",
                                "name: Demo",
                                "channels:",
                                "  kusto:",
                                "    enabled: true",
                                "    discovery_threshold_hours: 168",
                        ]
                ),
                encoding="utf-8",
        )
        monkeypatch.setenv("VERTEX_UIL_KUSTO", "1")
        previous_channels = {
                "kusto": {
                        "active": False,
                        "signal_count": 0,
                        "expected_min": 10,
                        "meets_expected_min": False,
                        "reason_not_active": "flag_not_passed",
                        "uil_enabled": True,
                        "uil_registry_file_present": True,
                        "uil_health": "ok",
                        "uil_registry_size": 3,
                        "uil_last_delta_summary": "+1 -0 ~0 =2",
                }
        }

        states = gather._build_gather_channel_states(
                program_id="demo",
                programs_root=tmp_path,
                workstreams=(),
                ado_signals=(),
                kusto_signals=(),
                workiq_signals=(),
                icm_signals=(),
                gather_flags={"kusto": False, "workiq": False, "icm": False},
                previous_channels=previous_channels,
        )

        assert states["kusto"]["uil_enabled"] is True
        assert states["kusto"]["uil_registry_size"] == 3
        assert states["kusto"]["uil_last_delta_summary"] == "+1 -0 ~0 =2"


def test_build_gather_channel_states_does_not_preserve_uil_metadata_when_channel_is_unconfigured(tmp_path: Path) -> None:
        previous_channels = {
                "kusto": {
                        "active": False,
                        "signal_count": 0,
                        "expected_min": 10,
                        "meets_expected_min": False,
                        "reason_not_active": "flag_not_passed",
                        "uil_enabled": True,
                        "uil_registry_file_present": True,
                        "uil_health": "ok",
                        "uil_registry_size": 3,
                        "uil_last_delta_summary": "+1 -0 ~0 =2",
                }
        }

        states = gather._build_gather_channel_states(
                program_id="demo",
                programs_root=tmp_path,
                workstreams=(),
                ado_signals=(),
                kusto_signals=(),
                workiq_signals=(),
                icm_signals=(),
                gather_flags={"kusto": False, "workiq": False, "icm": False},
                previous_channels=previous_channels,
        )

        assert states["kusto"]["uil_enabled"] is False
        assert "uil_registry_size" not in states["kusto"]
        assert "uil_last_delta_summary" not in states["kusto"]

def test_load_program_context_reads_nested_workiq_queries(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text(
        """
schema_version: "2.0"
id: demo
name: Demo Program
ado:
  organization: your-org
  project: One
  area_paths: ['One\\Demo']
  work_item_types: ["Feature"]
  excluded_states: ["Removed"]
  date_window_days: 14
  api_timeout_seconds: 30
m365:
  enabled: true
  prefer_agency: true
  workiq:
    feedback_search: find demo feedback
    teams_search: find demo teams
""".strip(),
        encoding="utf-8",
    )
    (program_dir / "workstreams.yaml").write_text(
        """
schema_version: "2.0"
workstreams:
  - id: demo
    name: Demo
    area_paths: ['One\\Demo']
""".strip(),
        encoding="utf-8",
    )

    program, _ = gather._load_program_context("demo", programs_root)

    assert program.m365 is not None
    assert program.m365.workiq_queries == {
        "feedback_search": "find demo feedback",
        "teams_search": "find demo teams",
    }


def test_load_program_context_reads_kusto_query_tail_fields(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text(
        """
schema_version: "2.0"
id: demo
name: Demo Program
ado:
  organization: your-org
  project: One
  area_paths: ['One\\Demo']
  work_item_types: ["Feature"]
  excluded_states: ["Removed"]
  date_window_days: 14
  api_timeout_seconds: 30
kusto:
  enabled: true
  queries:
    - id: velocity-p50
      cluster: https://adventure.kusto.windows.net
      database: xdataanalytics
      kql: Metrics | take 1
      section: Demo Telemetry
      render_as: metric_highlight
      confidence: high
      workstream_ids: [demo]
      refresh_on_gather: true
      label: Deploy P50 (hrs)
      result_column: P50
""".strip(),
        encoding="utf-8",
    )
    (program_dir / "workstreams.yaml").write_text(
        """
schema_version: "2.0"
workstreams:
  - id: demo
    name: Demo
    area_paths: ['One\\Demo']
""".strip(),
        encoding="utf-8",
    )

    program, _ = gather._load_program_context("demo", programs_root)

    assert program.kusto is not None
    assert len(program.kusto.queries) == 1
    assert program.kusto.queries[0].refresh_on_gather is True
    assert program.kusto.queries[0].label == "Deploy P50 (hrs)"
    assert program.kusto.queries[0].result_column == "P50"


def test_load_program_context_reads_workstreams_from_program_facts(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text(
        """
schema_version: "2.0"
id: demo
name: Demo Program
ado:
  organization: your-org
  project: One
  area_paths: ['One\\Demo']
  work_item_types: ["Feature"]
  excluded_states: ["Removed"]
  date_window_days: 14
  api_timeout_seconds: 30
""".strip(),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}
    workstream = Workstream(id="demo", name="Demo", area_paths=("One\\Demo",))

    def _load_current_workstreams(program_id: str, *, programs_root: Path):
        captured["program_id"] = program_id
        captured["programs_root"] = programs_root
        return (workstream,)

    monkeypatch.setattr(gather, "load_current_workstreams", _load_current_workstreams)

    program, loaded_workstreams = gather._load_program_context("demo", programs_root)

    assert program.id == "demo"
    assert loaded_workstreams == (workstream,)
    assert captured == {
        "program_id": "demo",
        "programs_root": programs_root,
    }



def test_gather_program_appends_workiq_signals_without_auto_review(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    db_root = tmp_path / "vertex-db"
    monkeypatch.setenv("VERTEX_DB_PATH", str(db_root))

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find feedback from Rushi on Acme newsletter drafts",
                "teams_search": "xInfraSWPM channel discussions about WI:5678 blocker status",
            },
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
            signal_sources=WorkstreamSignalSources(
                teams_meeting_series=(
                    TeamsMeetingSeries(display_name="Acme Weekly Ops Review", series_id=None),
                    TeamsMeetingSeries(display_name="Adventure Ramp Weekly Sync", series_id="meeting-series-1"),
                ),
                teams_chats=(
                    TeamsChat(display_name="Acme Eng Core Chat", thread_id=None),
                    TeamsChat(display_name="Acme Incident Triage", thread_id="configured-chat-1"),
                ),
            ),
        ),
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find feedback from Rushi on Acme newsletter drafts. Focus on Acme. Keywords: Acme Eng Core Chat, Acme Incident Triage, Acme Weekly Ops Review, Adventure Ramp Weekly Sync.": {
                "results": [
                    {
                        "messageId": "thread-1",
                        "threadId": "observed-email-thread-1",
                        "title": "Rushi feedback on WI:1234",
                        "sender": "rushi@example.com",
                        "snippet": "Please update WI:1234 before the next draft.",
                        "timestamp": "2026-05-10T09:00:00Z",
                        "link": "https://outlook.office.com/mail/thread-1",
                    }
                ]
            },
            "xInfraSWPM channel discussions about WI:5678 blocker status. Focus on Acme. Keywords: Acme Eng Core Chat, Acme Incident Triage, Acme Weekly Ops Review, Adventure Ramp Weekly Sync.": {
                "results": [
                    {
                        "messageId": "teams-1",
                        "threadId": "observed-teams-thread-1",
                        "title": "Channel thread WI:5678",
                        "sender": "owner@example.com",
                        "snippet": "WI:5678 needs a blocker update in ADO.",
                        "timestamp": "2026-05-10T10:00:00Z",
                        "link": "https://teams.microsoft.com/l/message/1",
                    }
                ]
            },
        }
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    artifacts = gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: bridge,
    )

    signals = read_signals("acme", programs_root=programs_root)
    reviews = read_review_log("acme", programs_root=programs_root)
    gather_state = load_gather_state("acme", programs_root=programs_root)

    assert artifacts.discovered_signals == 2
    assert artifacts.new_signals == 2
    assert artifacts.pending_review == 2
    assert artifacts.auto_reviews_written == 0
    # The signal-content questions (the newsletter path) are still issued unchanged.
    # "ramp sync" and "acme core" are adaptive keyword expansions (discover.md §8.5 / FR-DISC-06)
    # mined from configured sources — intended adaptive behavior.
    # Discovery now ALSO fires content-relational calendar/Teams questions for the
    # configured sources (ops-ready.md S1 discovery recall fix); those are asserted by
    # shape rather than exact prose to avoid pinning the full discovery prompt sequence.
    assert any(
        q.startswith("Find feedback from Rushi on Acme newsletter drafts. Focus on Acme.")
        and "ramp sync" in q
        for q in bridge.questions
    )
    assert any(
        q.startswith("xInfraSWPM channel discussions about WI:5678 blocker status. Focus on Acme.")
        and "ramp sync" in q
        for q in bridge.questions
    )
    assert any(question.startswith("Use my Microsoft 365 calendar") for question in bridge.questions)
    assert any(question.startswith("Use my Microsoft Teams messages") for question in bridge.questions)
    assert gather_state.m365_discovery["observed_thread_ids"] == 2
    assert gather_state.m365_discovery["untracked_observed_thread_ids"] == 2
    assert gather_state.m365_discovery["signals_without_thread_id"] == 0
    assert _read_ingestion_run_rows("acme", db_root=db_root) == [
        ("ado/comment", "success", 0),
        ("ado/dependency", "success", 0),
        ("ado/revision", "success", 0),
        ("vertex/freshness", "success", 0),
        ("workiq", "success", 2),
    ]
    assert gather_state.m365_discovery["signals_without_workstream"] == 0
    assert gather_state.m365_discovery["registry_bootstrapped"] is True


def test_workiq_thread_id_falls_back_to_teams_urls_when_direct_id_missing() -> None:
    assert gather._workiq_thread_id(
        {"link": "https://teams.microsoft.com/l/message/19:thread-id@thread.v2/1776716474489?context=%7B%22contextType%22:%22chat%22%7D"}
    ) == "19:thread-id@thread.v2"
    assert gather._workiq_thread_id(
        {"link": "https://teams.microsoft.com/l/meeting/details?eventId=AAMkExampleEventId%3d%3d"}
    ) == "AAMkExampleEventId=="


def test_gather_program_threads_configured_workiq_total_budget_seconds(monkeypatch, tmp_path: Path) -> None:
    """The whole WorkIQ phase (all query plans combined) must be bounded by the
    program's configured ``m365.retrieval.max_wall_clock_seconds`` -- without
    this, a live run with N query plans could take up to WORKIQ_TIMEOUT per
    plan with no overall cap, stalling gather for tens of minutes."""
    programs_root = tmp_path / "programs"

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={"feedback_search": "Find feedback on Acme newsletter drafts"},
            retrieval=WorkIQRetrievalConfig(max_wall_clock_seconds=120),
        ),
    )
    workstreams = (
        Workstream(id="acme", name="Acme", area_paths=("One\\Adventure\\Acme",), dri_email="maintainer@example.com"),
    )

    captured_kwargs: dict = {}

    def _fake_build_workiq_signals(**kwargs):
        captured_kwargs.update(kwargs)
        return ()

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(gather, "_build_workiq_signals", _fake_build_workiq_signals)

    gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: _FakeWorkIQBridge(responses={}),
    )

    assert captured_kwargs["total_budget_seconds"] == 120
    assert captured_kwargs["timeout_seconds"] == AgencyBridge.WORKIQ_TIMEOUT


def test_gather_program_workiq_total_budget_defaults_to_600_when_program_has_no_retrieval_config(
    monkeypatch, tmp_path: Path
) -> None:
    programs_root = tmp_path / "programs"

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={"feedback_search": "Find feedback on Acme newsletter drafts"},
        ),
    )
    workstreams = (
        Workstream(id="acme", name="Acme", area_paths=("One\\Adventure\\Acme",), dri_email="maintainer@example.com"),
    )

    captured_kwargs: dict = {}

    def _fake_build_workiq_signals(**kwargs):
        captured_kwargs.update(kwargs)
        return ()

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(gather, "_build_workiq_signals", _fake_build_workiq_signals)

    gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: _FakeWorkIQBridge(responses={}),
    )

    assert captured_kwargs["total_budget_seconds"] == 600


def test_gather_program_discovers_registry_threads_and_marks_observed_thread_as_tracked(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find current status",
            },
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(
                workiq_keywords=("SCHIE", "ramp review"),
            ),
        ),
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Adventure on Northwind. Keywords: SCHIE, ramp review.": {
                "results": [
                    {
                        "messageId": "mail-1",
                        "threadId": "observed-email-thread-1",
                        "title": "SCHIE weekly follow-up",
                        "snippet": "Northwind ramp review is blocked on SCHIE.",
                        "timestamp": "2026-05-10T09:00:00Z",
                    }
                ]
            },
            "Use my Microsoft Teams messages in any channel or chat to answer.": {
                "messages": [
                    {
                        "messageId": "search-team-1",
                        "threadId": "new-teams-thread-1",
                        "channel": "Acme Eng Core Chat",
                        "snippet": "SCHIE blocker remains open for Northwind.",
                        "createdDateTime": "2026-05-10T09:15:00Z",
                    }
                ]
            },
        },
        tool_payloads={
            "search_emails": {
                "emails": [
                    {
                        "messageId": "search-mail-1",
                        "threadId": "observed-email-thread-1",
                        "subject": "SCHIE weekly follow-up",
                        "snippet": "Northwind ramp review is blocked on SCHIE.",
                        "receivedDateTime": "2026-05-10T09:00:00Z",
                    }
                ]
            },
        },
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: bridge,
    )

    registry = load_m365_registry("acme", programs_root)
    gather_state = load_gather_state("acme", programs_root=programs_root)

    assert gather_state is not None
    assert gather_state.m365_discovery["observed_thread_ids"] == 1
    assert gather_state.m365_discovery["untracked_observed_thread_ids"] == 0
    assert any(artifact.thread_id == "observed-email-thread-1" for artifact in registry.artifacts)
    assert any(artifact.thread_id == "new-teams-thread-1" for artifact in registry.artifacts)
    assert any(artifact.inferred_workstream == "acme" for artifact in registry.artifacts if artifact.thread_id == "observed-email-thread-1")
    # Mail discovery still falls back to the structured search tool; Teams discovery now
    # goes through ask_work_iq, so assert both discovery paths ran without pinning the
    # exact internal sequence.
    invoked_tools = {call[1] for call in bridge.tool_calls}
    assert "search_emails" in invoked_tools
    assert any(
        question.startswith("Use my Microsoft Teams messages in any channel or chat to answer.")
        for question in bridge.questions
    )


def test_run_m365_discovery_pass_rediscovers_recently_rejected_registry_thread(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(
            M365RegistryArtifact(
                artifact_id="thread:auto:rejected001",
                artifact_type="email_thread",
                inferred_workstream="acme",
                confidence=1.0,
                confidence_source="pm_confirmed",
                pm_confirmed=True,
                promoted_to_workstreams_yaml=False,
                first_seen=date(2026, 5, 1),
                last_seen=date(2026, 5, 10),
                display_name="Rejected Ramp Thread",
                thread_id="rejected-thread-1",
                topics=("SCHIE",),
                routing_reasoning="Previously routed to Acme.",
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )
    feedback_path = programs_root / "acme" / "_feedback" / "m365_routing_feedback.jsonl"
    feedback_path.parent.mkdir(parents=True, exist_ok=True)
    feedback_path.write_text(
        '{"ts": "2026-05-10T07:30:00+00:00", "artifact_id": "thread:auto:rejected001", "action": "reject", "pm_alias": "operator", "workstream_id": null, "topics": [], "reason": "not current", "series_id": null, "thread_id": null}\n',
        encoding="utf-8",
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(workiq_keywords=("SCHIE",)),
        ),
    )

    class _FakeM365TopicRouter:
        def route_artifact(
            self,
            *,
            display_name: str | None,
            subject_or_title: str | None,
            participant_aliases: tuple[str, ...],
            sample_text: str | None,
            workstream_profiles: tuple[Workstream, ...],
            recent_confirmed_signals: dict[str, tuple[str, ...]] | None = None,
            recent_rejected_signals: dict[str, tuple[str, ...]] | None = None,
            recent_reassign_corrections=None,
        ) -> M365RoutingDecision:
            del display_name, subject_or_title, participant_aliases, sample_text, workstream_profiles, recent_confirmed_signals, recent_rejected_signals, recent_reassign_corrections
            return M365RoutingDecision(
                workstream_id="acme",
                confidence=0.72,
                topics=("SCHIE",),
                confidence_source="router",
                reasoning="Rediscovered after rejection.",
            )

    bridge = _FakeWorkIQBridge(
        responses={},
        tool_payloads={
            "search_emails": {
                "emails": [
                    {
                        "messageId": "search-mail-1",
                        "threadId": "rejected-thread-1",
                        "subject": "Rejected Ramp Thread",
                        "snippet": "SCHIE status moved again.",
                        "receivedDateTime": "2026-05-10T09:00:00Z",
                    }
                ]
            },
        }
    )

    discovered_artifacts, discovery_errors = gather._run_m365_discovery_pass(
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        workstreams=workstreams,
        bridge_client=bridge,
        topic_router=_FakeM365TopicRouter(),
        programs_root=programs_root,
    )

    assert discovery_errors == ()
    assert len(discovered_artifacts) == 1
    assert discovered_artifacts[0].thread_id == "rejected-thread-1"
    assert discovered_artifacts[0].routing_reasoning == "Rediscovered after rejection."


def test_run_m365_discovery_pass_rediscovers_pm_rejected_registry_thread(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(
            M365RegistryArtifact(
                artifact_id="thread:auto:rejectedpm",
                artifact_type="email_thread",
                inferred_workstream="acme",
                confidence=0.2,
                confidence_source="pm_rejected",
                pm_confirmed=False,
                promoted_to_workstreams_yaml=False,
                first_seen=date(2026, 5, 1),
                last_seen=date(2026, 5, 10),
                display_name="PM Rejected Ramp Thread",
                thread_id="rejected-thread-pm-1",
                topics=("SCHIE",),
                routing_reasoning="Rejected by PM.",
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(workiq_keywords=("SCHIE",)),
        ),
    )

    class _FakeM365TopicRouter:
        def route_artifact(
            self,
            *,
            display_name: str | None,
            subject_or_title: str | None,
            participant_aliases: tuple[str, ...],
            sample_text: str | None,
            workstream_profiles: tuple[Workstream, ...],
            recent_confirmed_signals: dict[str, tuple[str, ...]] | None = None,
            recent_rejected_signals: dict[str, tuple[str, ...]] | None = None,
            recent_reassign_corrections=None,
        ) -> M365RoutingDecision:
            del display_name, subject_or_title, participant_aliases, sample_text, workstream_profiles, recent_confirmed_signals, recent_rejected_signals, recent_reassign_corrections
            return M365RoutingDecision(
                workstream_id="acme",
                confidence=0.72,
                topics=("SCHIE",),
                confidence_source="router",
                reasoning="Rediscovered after PM rejection.",
            )

    bridge = _FakeWorkIQBridge(
        responses={},
        tool_payloads={
            "search_emails": {
                "emails": [
                    {
                        "messageId": "search-mail-pm-rejected",
                        "threadId": "rejected-thread-pm-1",
                        "subject": "PM Rejected Ramp Thread",
                        "snippet": "SCHIE status moved again.",
                        "receivedDateTime": "2026-05-10T09:00:00Z",
                    }
                ]
            },
        }
    )

    discovered_artifacts, discovery_errors = gather._run_m365_discovery_pass(
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        workstreams=workstreams,
        bridge_client=bridge,
        topic_router=_FakeM365TopicRouter(),
        programs_root=programs_root,
    )

    assert discovery_errors == ()
    assert len(discovered_artifacts) == 1
    assert discovered_artifacts[0].thread_id == "rejected-thread-pm-1"
    assert discovered_artifacts[0].routing_reasoning == "Rediscovered after PM rejection."


def test_run_m365_discovery_pass_rebinds_recreated_teams_chat_drift(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(
            M365RegistryArtifact(
                artifact_id="chan:acme-ramp-chat",
                artifact_type="teams_channel",
                inferred_workstream="acme",
                confidence=1.0,
                confidence_source="pm_confirmed",
                pm_confirmed=True,
                promoted_to_workstreams_yaml=False,
                first_seen=date(2026, 5, 1),
                last_seen=date(2026, 5, 10),
                display_name="Ramp Chat",
                thread_id="old-thread-1",
                topics=("SCHIE", "ramp"),
                routing_reasoning="Current operating chat.",
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(workiq_keywords=("SCHIE",)),
        ),
    )

    class _FakeM365TopicRouter:
        def route_artifact(
            self,
            *,
            display_name: str | None,
            subject_or_title: str | None,
            participant_aliases: tuple[str, ...],
            sample_text: str | None,
            workstream_profiles: tuple[Workstream, ...],
            recent_confirmed_signals: dict[str, tuple[str, ...]] | None = None,
            recent_rejected_signals: dict[str, tuple[str, ...]] | None = None,
            recent_reassign_corrections=None,
        ) -> M365RoutingDecision:
            del display_name, subject_or_title, participant_aliases, sample_text, workstream_profiles, recent_confirmed_signals, recent_rejected_signals, recent_reassign_corrections
            return M365RoutingDecision(
                workstream_id="acme",
                confidence=0.91,
                topics=("SCHIE", "ramp"),
                confidence_source="router",
                reasoning="Same ramp chat recreated with a fresh Teams thread id.",
            )

    bridge = _FakeWorkIQBridge(
        responses={
            "Use my Microsoft Teams messages in any channel or chat to answer.": {
                "messages": [
                    {
                        "messageId": "teams-msg-1",
                        "threadId": "new-thread-1",
                        "channel": "Ramp Chat",
                        "subject": "Ramp Chat",
                        "bodyPreview": "SCHIE ramp coordination continues here.",
                        "sentDateTime": "2026-05-10T09:00:00Z",
                    }
                ]
            },
        },
        tool_payloads={
            "search_emails": {"emails": []},
        }
    )

    discovered_artifacts, discovery_errors = gather._run_m365_discovery_pass(
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        workstreams=workstreams,
        bridge_client=bridge,
        topic_router=_FakeM365TopicRouter(),
        programs_root=programs_root,
    )

    registry = load_m365_registry("acme", programs_root)

    assert discovery_errors == ()
    assert len(discovered_artifacts) == 1
    assert discovered_artifacts[0].thread_id == "new-thread-1"
    assert len(registry.artifacts) == 1
    assert registry.artifacts[0].artifact_id == "chan:acme-ramp-chat"
    assert registry.artifacts[0].thread_id == "new-thread-1"
    assert "thread:auto:newthrea" in registry.artifacts[0].legacy_artifact_ids


def test_run_m365_discovery_pass_discovers_meeting_series_from_calendar(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    workstreams = (
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(workiq_keywords=("SCHIE",)),
        ),
    )

    class _FakeM365TopicRouter:
        def route_artifact(
            self,
            *,
            display_name: str | None,
            subject_or_title: str | None,
            participant_aliases: tuple[str, ...],
            sample_text: str | None,
            workstream_profiles: tuple[Workstream, ...],
            recent_confirmed_signals: dict[str, tuple[str, ...]] | None = None,
            recent_rejected_signals: dict[str, tuple[str, ...]] | None = None,
            recent_reassign_corrections=None,
        ) -> M365RoutingDecision:
            del display_name, subject_or_title, participant_aliases, sample_text, workstream_profiles, recent_confirmed_signals, recent_rejected_signals, recent_reassign_corrections
            return M365RoutingDecision(
                workstream_id="acme",
                confidence=0.87,
                topics=("SCHIE", "ops"),
                confidence_source="router",
                reasoning="Recurring northwind ops meeting belongs to Acme.",
            )

    bridge = _FakeWorkIQBridge(
        responses={
            "Use my Microsoft 365 calendar and meetings to answer.": {
                "events": [
                    {
                        "id": "event-1",
                        "meetingId": "meeting-occurrence-1",
                        "seriesMasterId": "series-123",
                        "subject": "Acme Weekly Ops Review",
                        "organizer": {"emailAddress": {"address": "operator@example.com"}},
                        "attendees": [{"emailAddress": {"address": "owner@example.com"}}],
                        "startDateTime": "2026-05-10T09:00:00Z",
                    }
                ]
            },
        },
        tool_payloads={
            "search_emails": {"emails": []},
        },
    )

    discovered_artifacts, discovery_errors = gather._run_m365_discovery_pass(
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        workstreams=workstreams,
        bridge_client=bridge,
        topic_router=_FakeM365TopicRouter(),
        programs_root=programs_root,
    )

    registry = load_m365_registry("acme", programs_root)

    assert discovery_errors == ()
    assert len(discovered_artifacts) == 1
    assert discovered_artifacts[0].artifact_type == "meeting_series"
    assert discovered_artifacts[0].series_id == "series-123"
    assert discovered_artifacts[0].display_name == "Acme Weekly Ops Review"
    assert len(registry.artifacts) == 1
    assert registry.artifacts[0].artifact_type == "meeting_series"
    assert registry.artifacts[0].series_id == "series-123"


def test_gather_program_routes_discovered_thread_from_recent_confirmed_m365_corpus(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find current status",
            },
        ),
    )
    workstreams = (
        Workstream(
            id="contoso",
            name="Device delivery",
            area_paths=("One\\Devices\\Contoso",),
            signal_sources=WorkstreamSignalSources(
                workiq_keywords=("gfu",),
            ),
        ),
        Workstream(
            id="acme",
            name="Store rollout",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(
                workiq_keywords=("schie",),
            ),
        ),
    )
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(
            M365RegistryArtifact(
                artifact_id="thread:auto:confirmed1",
                artifact_type="email_thread",
                display_name="Pilot readiness thread",
                thread_id="confirmed-thread-1",
                inferred_workstream="acme",
                confidence=1.0,
                confidence_source="pm_confirmed",
                pm_confirmed=True,
                promoted_to_workstreams_yaml=False,
                first_seen=date(2026, 5, 8),
                last_seen=date(2026, 5, 9),
                signal_yield_last_3=(1, 1, 1),
                routing_reasoning="Pilot readiness follow-up for rollout blockers.",
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc),
    )
    for signal_id, signal_text in (
        ("sig-pilot-ready-1", "Pilot readiness blockers remain open for rollout planning."),
        ("sig-pilot-ready-2", "Pilot readiness recap tracks the same rollout blockers."),
    ):
        signal_timestamp = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
        append_signal(
            Signal(
                id=signal_id,
                timestamp=signal_timestamp,
                source="workiq/email",
                program_id="acme",
                workstream_id="acme",
                entity_refs=(),
                text=signal_text,
                raw_ref=f"message:{signal_id}",
                confidence=Confidence.MEDIUM,
                metadata={"source_type": "email", "message_id": signal_id},
                thread_id=f"thread:{signal_id}",
                review_policy=ReviewPolicy.PENDING,
            ),
            programs_root=programs_root,
            partition_at=signal_timestamp,
        )
        append_review_decision(
            "acme",
            SignalReviewDecision(
                signal_id=signal_id,
                decision="approved",
                reviewed_at=signal_timestamp + timedelta(hours=1),
                reviewed_by="operator",
            ),
            programs_root=programs_root,
        )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Device delivery. Keywords: gfu.": {"results": []},
            "Find current status. Focus on Store rollout. Keywords: schie.": {"results": []},
        },
        tool_payloads={
            "search_emails": {
                "emails": [
                    {
                        "messageId": "search-mail-1",
                        "threadId": "observed-email-thread-2",
                        "subject": "Pilot readiness follow-up",
                        "snippet": "Pilot readiness blockers still need rollout sign-off.",
                        "receivedDateTime": "2026-05-10T09:00:00Z",
                    }
                ]
            },
        },
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: bridge,
    )

    registry = load_m365_registry("acme", programs_root)
    observed = next(artifact for artifact in registry.artifacts if artifact.thread_id == "observed-email-thread-2")

    assert observed.inferred_workstream == "acme"
    assert observed.confidence > 0.0
    assert "pilot readiness" in (observed.routing_reasoning or "").lower()


def test_gather_program_uses_injected_m365_topic_router_for_discovery(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    class _FakeM365TopicRouter:
        def route_artifact(
            self,
            *,
            display_name: str | None,
            subject_or_title: str | None,
            participant_aliases: tuple[str, ...],
            sample_text: str | None,
            workstream_profiles: tuple[Workstream, ...],
            recent_confirmed_signals: dict[str, tuple[str, ...]] | None = None,
            recent_rejected_signals: dict[str, tuple[str, ...]] | None = None,
            recent_reassign_corrections=None,
        ) -> M365RoutingDecision:
            del display_name, subject_or_title, participant_aliases, sample_text, workstream_profiles, recent_confirmed_signals, recent_rejected_signals, recent_reassign_corrections
            return M365RoutingDecision(
                workstream_id="contoso",
                confidence=0.88,
                topics=("fake-router",),
                confidence_source="router",
                reasoning="Injected router decision.",
            )

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find current status",
            },
        ),
    )
    workstreams = (
        Workstream(
            id="contoso",
            name="Device delivery",
            area_paths=("One\\Devices\\Contoso",),
            signal_sources=WorkstreamSignalSources(
                workiq_keywords=("gfu",),
            ),
        ),
        Workstream(
            id="acme",
            name="Store rollout",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(
                workiq_keywords=("schie",),
            ),
        ),
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Device delivery. Keywords: gfu.": {"results": []},
            "Find current status. Focus on Store rollout. Keywords: schie.": {"results": []},
        },
        tool_payloads={
            "search_emails": {
                "emails": [
                    {
                        "messageId": "search-mail-1",
                        "threadId": "observed-email-thread-3",
                        "subject": "Unmapped discovery thread",
                        "snippet": "This text should be routed by the injected router.",
                        "receivedDateTime": "2026-05-10T09:00:00Z",
                    }
                ]
            },
        },
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: bridge,
        m365_topic_router=_FakeM365TopicRouter(),
    )

    registry = load_m365_registry("acme", programs_root)
    observed = next(artifact for artifact in registry.artifacts if artifact.thread_id == "observed-email-thread-3")

    assert observed.inferred_workstream == "contoso"
    assert observed.confidence == 0.88
    assert observed.confidence_source == "router"
    assert observed.routing_reasoning == "Injected router decision."


def test_signals_from_workiq_payload_scrubs_external_pii_from_signal_text() -> None:
    workstreams = (
        Workstream(id="acme", name="Store rollout", signal_sources=WorkstreamSignalSources(workiq_keywords=("schie",))),
    )

    signals = gather._signals_from_workiq_payload(
        payload={
            "emails": [
                {
                    "messageId": "mail-1",
                    "threadId": "thread-1",
                    "subject": "Reach me at partner@example.com",
                    "snippet": "Call +1 (425) 555-0100 about SCHIE blockers.",
                    "receivedDateTime": "2026-05-10T09:00:00Z",
                }
            ]
        },
        query_name="feedback_search:acme",
        question="Find current status",
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        items_by_id={},
        workstreams=workstreams,
        default_workstream_id="acme",
    )

    assert len(signals) == 1
    assert "partner@example.com" not in signals[0].text
    assert "+1 (425) 555-0100" not in signals[0].text
    assert "[PII-FILTERED-EMAIL]" in signals[0].text
    assert "[PII-FILTERED-PHONE]" in signals[0].text


def test_signals_from_workiq_payload_preserves_microsoft_aliases_in_signal_text() -> None:
    workstreams = (
        Workstream(id="acme", name="Store rollout", signal_sources=WorkstreamSignalSources(workiq_keywords=("schie",))),
    )

    signals = gather._signals_from_workiq_payload(
        payload={
            "emails": [
                {
                    "messageId": "mail-2",
                    "threadId": "thread-2",
                    "subject": "Follow up with operator@example.com",
                    "snippet": "SCHIE blockers remain active.",
                    "receivedDateTime": "2026-05-10T09:00:00Z",
                }
            ]
        },
        query_name="feedback_search:acme",
        question="Find current status",
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        items_by_id={},
        workstreams=workstreams,
        default_workstream_id="acme",
    )

    assert len(signals) == 1
    assert "[PII-FILTERED-EMAIL]" in signals[0].text
    assert "operator@example.com" not in signals[0].text


def test_signals_from_workiq_payload_marks_historical_transcripts_as_backfill() -> None:
    workstreams = (
        Workstream(id="acme", name="Store rollout", signal_sources=WorkstreamSignalSources(workiq_keywords=("schie",))),
    )

    signals = gather._signals_from_workiq_payload(
        payload={
            "meetings": [
                {
                    "messageId": "tx-1",
                    "threadId": "thread-1",
                    "subject": "Acme Weekly Ops Review",
                    "snippet": "SCHIE blockers remain active.",
                    "receivedDateTime": "2026-05-08T09:00:00Z",
                }
            ]
        },
        query_name="transcript_search:acme",
        question="Find current status",
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        items_by_id={},
        workstreams=workstreams,
        default_workstream_id="acme",
    )

    assert len(signals) == 1
    assert signals[0].source == "workiq/transcript"
    assert signals[0].metadata is not None
    assert signals[0].metadata["backfill"] is True


def test_extract_work_item_refs_supports_common_ado_aliases() -> None:
    assert gather._extract_work_item_refs(
        "Bug 12345 remains blocked; PBI #23456 in review; User Story 34567 accepted; Task 45678 queued."
    ) == ("WI:12345", "WI:23456", "WI:34567", "WI:45678")


def test_signals_from_workiq_payload_splits_multi_ref_record_into_atomic_signals() -> None:
    workstreams = (
        Workstream(id="acme", name="Store rollout", area_paths=("One\\Adventure\\Acme",)),
        Workstream(id="contoso", name="Device delivery", area_paths=("One\\Adventure\\Contoso",)),
    )
    items_by_id = {
        12345: WorkItem(
            id=12345,
            type="Bug",
            title="Pilot blocker",
            state="Active",
            assigned_to=None,
            assigned_to_email=None,
            area_path="One\\Adventure\\Acme",
            iteration_path="One\\Sprint 30",
            target_date=None,
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        ),
        67890: WorkItem(
            id=67890,
            type="Task",
            title="Mitigation follow-up",
            state="Active",
            assigned_to=None,
            assigned_to_email=None,
            area_path="One\\Adventure\\Contoso",
            iteration_path="One\\Sprint 30",
            target_date=None,
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        ),
    }

    signals = gather._signals_from_workiq_payload(
        payload={
            "emails": [
                {
                    "messageId": "mail-atomic-1",
                    "threadId": "thread-atomic-1",
                    "subject": "Weekly ops review",
                    "snippet": "Bug 12345 remains blocked on SCHIE.\nTask 67890 mitigation owner confirmed.",
                    "receivedDateTime": "2026-05-10T09:00:00Z",
                }
            ]
        },
        query_name="feedback_search:acme",
        question="Find current status",
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        items_by_id=items_by_id,
        workstreams=workstreams,
        default_workstream_id="acme",
    )

    assert len(signals) == 2
    assert tuple(signal.entity_refs for signal in signals) == (
        ("WI:12345", "WS:acme"),
        ("WI:67890", "WS:contoso"),
    )
    assert tuple(signal.workstream_id for signal in signals) == ("acme", "contoso")
    assert all(signal.metadata is not None for signal in signals)
    assert signals[0].metadata["message_id"] == "mail-atomic-1:seg:0"
    assert signals[1].metadata["message_id"] == "mail-atomic-1:seg:1"
    assert signals[0].metadata["parent_message_id"] == "mail-atomic-1"
    assert signals[1].metadata["parent_message_id"] == "mail-atomic-1"
    assert signals[0].raw_ref == "workiq:email:mail-atomic-1:seg:0"
    assert signals[1].raw_ref == "workiq:email:mail-atomic-1:seg:1"


def test_signals_from_workiq_payload_routes_fragmented_record_once_and_uses_single_ref_fallback() -> None:
    captured: dict[str, object] = {"calls": 0}

    class _CapturingRouter:
        def route_artifact(
            self,
            *,
            display_name: str | None,
            subject_or_title: str | None,
            participant_aliases: tuple[str, ...],
            sample_text: str | None,
            workstream_profiles: tuple[Workstream, ...],
            recent_confirmed_signals: dict[str, tuple[str, ...]] | None = None,
            recent_rejected_signals: dict[str, tuple[str, ...]] | None = None,
            recent_reassign_corrections=None,
        ) -> M365RoutingDecision:
            del display_name, subject_or_title, participant_aliases, sample_text, workstream_profiles, recent_confirmed_signals, recent_rejected_signals, recent_reassign_corrections
            captured["calls"] = int(captured["calls"]) + 1
            return M365RoutingDecision(
                workstream_id="contoso",
                confidence=0.74,
                topics=("pilot",),
                confidence_source="router",
                reasoning="Fragmented transcript aligns with Contoso.",
            )

    workstreams = (
        Workstream(id="contoso", name="Device delivery", area_paths=("One\\Adventure\\Contoso",)),
    )

    anchored_signals = gather._signals_from_workiq_payload(
        payload={
            "meetings": [
                {
                    "messageId": "tx-fallback-1",
                    "threadId": "thread-fallback-1",
                    "subject": "WI 12345 weekly ops review",
                    "snippet": "Deployment remains blocked;\nNeed partner approval before deployment",
                    "receivedDateTime": "2026-05-10T09:00:00Z",
                }
            ]
        },
        query_name="transcript_search:acme",
        question="Find current status",
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        items_by_id={
            12345: WorkItem(
                id=12345,
                type="Bug",
                title="Pilot blocker",
                state="Active",
                assigned_to=None,
                assigned_to_email=None,
                area_path="One\\Adventure\\Contoso",
                iteration_path="One\\Sprint 30",
                target_date=None,
                risk_level=RiskLevel.MEDIUM,
                tags=[],
                custom_fields={},
                revisions=[],
                comments=[],
                fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
            )
        },
        workstreams=workstreams,
        default_workstream_id=None,
    )

    assert len(anchored_signals) == 2
    assert all(signal.entity_refs == ("WI:12345", "WS:contoso") for signal in anchored_signals)
    assert all(signal.workstream_id == "contoso" for signal in anchored_signals)

    routed_signals = gather._signals_from_workiq_payload(
        payload={
            "emails": [
                {
                    "messageId": "mail-routed-1",
                    "threadId": "thread-routed-1",
                    "subject": "Pilot readiness follow-up",
                    "snippet": "Deployment remains blocked;\nNeed partner approval before deployment",
                    "receivedDateTime": "2026-05-10T09:00:00Z",
                }
            ]
        },
        query_name="feedback_search:acme",
        question="Find current status",
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        items_by_id={},
        workstreams=workstreams,
        default_workstream_id=None,
        topic_router=_CapturingRouter(),
    )

    assert captured["calls"] == 1
    assert len(routed_signals) == 2
    assert all(signal.workstream_id == "contoso" for signal in routed_signals)
    assert all(signal.entity_refs == ("WS:contoso",) for signal in routed_signals)


def test_signals_from_workiq_payload_preserve_configured_thread_work_item_refs() -> None:
    workstreams = (
        Workstream(
            id="contoso",
            name="Device delivery",
            area_paths=("One\\Adventure\\Contoso",),
            signal_sources=WorkstreamSignalSources(
                teams_chats=(
                    TeamsChat(
                        display_name="Contoso Chat",
                        thread_id="thread-config-1",
                        work_item_ids=(12345,),
                    ),
                ),
            ),
        ),
    )
    items_by_id = {
        12345: WorkItem(
            id=12345,
            type="Bug",
            title="Pilot blocker",
            state="Active",
            assigned_to=None,
            assigned_to_email=None,
            area_path="One\\Adventure\\Contoso",
            iteration_path="One\\Sprint 30",
            target_date=None,
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        ),
    }

    signals = gather._signals_from_workiq_payload(
        payload={
            "messages": [
                {
                    "messageId": "msg-config-1",
                    "threadId": "thread-config-1",
                    "subject": "Pilot readiness follow-up",
                    "snippet": "Deployment remains blocked pending partner approval.",
                    "receivedDateTime": "2026-05-10T09:00:00Z",
                }
            ]
        },
        query_name="feedback_search:acme",
        question="Find current status",
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        items_by_id=items_by_id,
        workstreams=workstreams,
        default_workstream_id=None,
    )

    assert len(signals) == 1
    assert signals[0].workstream_id == "contoso"
    assert signals[0].entity_refs == ("WI:12345", "WS:contoso")


def test_signals_from_workiq_payload_merge_configured_and_textual_thread_work_item_refs() -> None:
    workstreams = (
        Workstream(
            id="contoso",
            name="Device delivery",
            area_paths=("One\\Adventure\\Contoso",),
            signal_sources=WorkstreamSignalSources(
                email_threads=(
                    EmailThreadSource(
                        display_name="Contoso Thread",
                        thread_id="mail-thread-1",
                        work_item_ids=(12345, 67890),
                    ),
                ),
            ),
        ),
    )
    items_by_id = {
        12345: WorkItem(
            id=12345,
            type="Bug",
            title="Pilot blocker",
            state="Active",
            assigned_to=None,
            assigned_to_email=None,
            area_path="One\\Adventure\\Contoso",
            iteration_path="One\\Sprint 30",
            target_date=None,
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        ),
        67890: WorkItem(
            id=67890,
            type="Task",
            title="Mitigation owner",
            state="Active",
            assigned_to=None,
            assigned_to_email=None,
            area_path="One\\Adventure\\Contoso",
            iteration_path="One\\Sprint 30",
            target_date=None,
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        ),
    }

    signals = gather._signals_from_workiq_payload(
        payload={
            "emails": [
                {
                    "messageId": "mail-config-1",
                    "threadId": "mail-thread-1",
                    "subject": "WI 12345 pilot readiness follow-up",
                    "snippet": "Deployment remains blocked pending partner approval.",
                    "receivedDateTime": "2026-05-10T09:00:00Z",
                }
            ]
        },
        query_name="email_thread:contoso:mail-thread-1",
        question="Find current status",
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        items_by_id=items_by_id,
        workstreams=workstreams,
        default_workstream_id="contoso",
        allowed_thread_ids=("mail-thread-1",),
    )

    assert len(signals) == 1
    assert signals[0].workstream_id == "contoso"
    assert signals[0].entity_refs == ("WI:12345", "WI:67890", "WS:contoso")


def test_signals_from_workiq_payload_preserve_configured_meeting_work_item_refs() -> None:
    workstreams = (
        Workstream(
            id="contoso",
            name="Device delivery",
            area_paths=("One\\Adventure\\Contoso",),
            signal_sources=WorkstreamSignalSources(
                teams_meeting_series=(
                    TeamsMeetingSeries(
                        display_name="Contoso Ops Review",
                        series_id="meeting-config-1",
                        work_item_ids=(12345,),
                    ),
                ),
            ),
        ),
    )
    items_by_id = {
        12345: WorkItem(
            id=12345,
            type="Bug",
            title="Pilot blocker",
            state="Active",
            assigned_to=None,
            assigned_to_email=None,
            area_path="One\\Adventure\\Contoso",
            iteration_path="One\\Sprint 30",
            target_date=None,
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        ),
    }

    signals = gather._signals_from_workiq_payload(
        payload={
            "meetings": [
                {
                    "meetingId": "meeting-config-1",
                    "title": "Pilot readiness review",
                    "summary": "Deployment remains blocked pending partner approval.",
                    "timestamp": "2026-05-10T09:00:00Z",
                }
            ]
        },
        query_name="feedback_search:contoso",
        question="Find current status",
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        items_by_id=items_by_id,
        workstreams=workstreams,
        default_workstream_id=None,
    )

    assert len(signals) == 1
    assert signals[0].workstream_id == "contoso"
    assert signals[0].entity_refs == ("WI:12345", "WS:contoso")


def test_signals_from_workiq_payload_normalize_configured_meeting_ids_for_work_item_refs() -> None:
    workstreams = (
        Workstream(
            id="contoso",
            name="Device delivery",
            area_paths=("One\\Adventure\\Contoso",),
            signal_sources=WorkstreamSignalSources(
                teams_meeting_series=(
                    TeamsMeetingSeries(
                        display_name="Contoso Ops Review",
                        series_id="https://teams.microsoft.com/l/meeting/details?eventId=AAMkExampleEventId%3d%3d",
                        work_item_ids=(12345,),
                    ),
                ),
            ),
        ),
    )
    items_by_id = {
        12345: WorkItem(
            id=12345,
            type="Bug",
            title="Pilot blocker",
            state="Active",
            assigned_to=None,
            assigned_to_email=None,
            area_path="One\\Adventure\\Contoso",
            iteration_path="One\\Sprint 30",
            target_date=None,
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        ),
    }

    signals = gather._signals_from_workiq_payload(
        payload={
            "meetings": [
                {
                    "link": "https://teams.microsoft.com/l/meeting/details?eventId=AAMkExampleEventId%3d%3d",
                    "title": "Pilot readiness review",
                    "summary": "Deployment remains blocked pending partner approval.",
                    "timestamp": "2026-05-10T09:00:00Z",
                }
            ]
        },
        query_name="feedback_search:contoso",
        question="Find current status",
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        items_by_id=items_by_id,
        workstreams=workstreams,
        default_workstream_id=None,
    )

    assert len(signals) == 1
    assert signals[0].workstream_id == "contoso"
    assert signals[0].entity_refs == ("WI:12345", "WS:contoso")


def test_tracked_workstream_ids_by_m365_id_include_normalized_meeting_series_ids() -> None:
    tracked_workstream_ids = gather._tracked_workstream_ids_by_m365_id(
        (
            M365RegistryArtifact(
                artifact_id="meeting:auto:1",
                artifact_type="meeting_series",
                inferred_workstream="contoso",
                confidence=0.95,
                confidence_source="pm_confirmed",
                pm_confirmed=True,
                promoted_to_workstreams_yaml=True,
                first_seen=date(2026, 5, 1),
                last_seen=date(2026, 5, 10),
                series_id="https://teams.microsoft.com/l/meeting/details?eventId=AAMkExampleEventId%3d%3d",
            ),
        )
    )

    assert tracked_workstream_ids == {"AAMkExampleEventId==": "contoso"}


def test_tracked_registry_thread_ids_exclude_recently_rejected_artifacts() -> None:
    tracked_ids = gather.tracked_registry_thread_ids(
        (
            M365RegistryArtifact(
                artifact_id="thread:auto:rejected001",
                artifact_type="email_thread",
                inferred_workstream="acme",
                confidence=1.0,
                confidence_source="pm_confirmed",
                pm_confirmed=True,
                promoted_to_workstreams_yaml=False,
                first_seen=date(2026, 5, 1),
                last_seen=date(2026, 5, 10),
                display_name="Rejected Thread",
                thread_id="rejected-thread-1",
                topics=("SCHIE",),
                routing_reasoning="Previously routed to Acme.",
            ),
            M365RegistryArtifact(
                artifact_id="meeting:auto:kept001",
                artifact_type="meeting_series",
                inferred_workstream="acme",
                confidence=1.0,
                confidence_source="pm_confirmed",
                pm_confirmed=True,
                promoted_to_workstreams_yaml=False,
                first_seen=date(2026, 5, 1),
                last_seen=date(2026, 5, 10),
                series_id="https://teams.microsoft.com/l/meeting/details?eventId=AAMkExampleEventId%3d%3d",
            ),
        ),
        feedback_events=(
            M365RoutingFeedbackEvent(
                ts=datetime(2026, 5, 10, 7, 0, tzinfo=timezone.utc),
                artifact_id="thread:auto:rejected001",
                action="reject",
                pm_alias="operator",
            ),
        ),
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    assert tracked_ids == {"AAMkExampleEventId=="}


def test_tracked_registry_thread_ids_exclude_pm_rejected_artifacts() -> None:
    tracked_ids = gather.tracked_registry_thread_ids(
        (
            M365RegistryArtifact(
                artifact_id="thread:auto:pmreject",
                artifact_type="email_thread",
                inferred_workstream="acme",
                confidence=0.2,
                confidence_source="pm_rejected",
                pm_confirmed=False,
                promoted_to_workstreams_yaml=False,
                first_seen=date(2026, 5, 1),
                last_seen=date(2026, 5, 10),
                display_name="Rejected Thread",
                thread_id="rejected-thread-1",
                topics=("SCHIE",),
                routing_reasoning="Rejected by PM.",
            ),
            M365RegistryArtifact(
                artifact_id="meeting:auto:kept001",
                artifact_type="meeting_series",
                inferred_workstream="acme",
                confidence=1.0,
                confidence_source="pm_confirmed",
                pm_confirmed=True,
                promoted_to_workstreams_yaml=False,
                first_seen=date(2026, 5, 1),
                last_seen=date(2026, 5, 10),
                series_id="https://teams.microsoft.com/l/meeting/details?eventId=AAMkExampleEventId%3d%3d",
            ),
        )
    )

    assert tracked_ids == {"AAMkExampleEventId=="}


def test_tracked_workstream_ids_by_m365_id_exclude_recently_rejected_artifacts() -> None:
    tracked_workstream_ids = gather._tracked_workstream_ids_by_m365_id(
        (
            M365RegistryArtifact(
                artifact_id="meeting:auto:1",
                artifact_type="meeting_series",
                inferred_workstream="contoso",
                confidence=0.95,
                confidence_source="pm_confirmed",
                pm_confirmed=True,
                promoted_to_workstreams_yaml=True,
                first_seen=date(2026, 5, 1),
                last_seen=date(2026, 5, 10),
                series_id="https://teams.microsoft.com/l/meeting/details?eventId=AAMkExampleEventId%3d%3d",
            ),
        ),
        feedback_events=(
            M365RoutingFeedbackEvent(
                ts=datetime(2026, 5, 10, 7, 0, tzinfo=timezone.utc),
                artifact_id="meeting:auto:1",
                action="reject",
                pm_alias="operator",
            ),
        ),
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    assert tracked_workstream_ids == {}


def test_signals_from_workiq_payload_route_meeting_records_from_tracked_registry_ids() -> None:
    workstreams = (
        Workstream(
            id="contoso",
            name="Device delivery",
            area_paths=("One\\Adventure\\Contoso",),
            signal_sources=WorkstreamSignalSources(),
        ),
    )

    tracked_workstream_ids = gather._tracked_workstream_ids_by_m365_id(
        (
            M365RegistryArtifact(
                artifact_id="meeting:auto:1",
                artifact_type="meeting_series",
                inferred_workstream="contoso",
                confidence=0.95,
                confidence_source="pm_confirmed",
                pm_confirmed=True,
                promoted_to_workstreams_yaml=True,
                first_seen=date(2026, 5, 1),
                last_seen=date(2026, 5, 10),
                series_id="https://teams.microsoft.com/l/meeting/details?eventId=AAMkExampleEventId%3d%3d",
            ),
        )
    )

    signals = gather._signals_from_workiq_payload(
        payload={
            "meetings": [
                {
                    "link": "https://teams.microsoft.com/l/meeting/details?eventId=AAMkExampleEventId%3d%3d",
                    "title": "Pilot readiness review",
                    "summary": "Deployment remains blocked pending partner approval.",
                    "timestamp": "2026-05-10T09:00:00Z",
                }
            ]
        },
        query_name="feedback_search:contoso",
        question="Find current status",
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        items_by_id={},
        workstreams=workstreams,
        default_workstream_id=None,
        tracked_workstream_ids_by_m365_id=tracked_workstream_ids,
    )

    assert len(signals) == 1
    assert signals[0].workstream_id == "contoso"
    assert signals[0].entity_refs == ("WS:contoso",)


def test_signals_from_workiq_payload_do_not_route_meeting_records_from_recently_rejected_registry_ids() -> None:
    workstreams = (
        Workstream(
            id="contoso",
            name="Device delivery",
            area_paths=("One\\Adventure\\Contoso",),
            signal_sources=WorkstreamSignalSources(),
        ),
    )

    tracked_workstream_ids = gather._tracked_workstream_ids_by_m365_id(
        (
            M365RegistryArtifact(
                artifact_id="meeting:auto:1",
                artifact_type="meeting_series",
                inferred_workstream="contoso",
                confidence=0.95,
                confidence_source="pm_confirmed",
                pm_confirmed=True,
                promoted_to_workstreams_yaml=True,
                first_seen=date(2026, 5, 1),
                last_seen=date(2026, 5, 10),
                series_id="https://teams.microsoft.com/l/meeting/details?eventId=AAMkExampleEventId%3d%3d",
            ),
        ),
        feedback_events=(
            M365RoutingFeedbackEvent(
                ts=datetime(2026, 5, 10, 7, 0, tzinfo=timezone.utc),
                artifact_id="meeting:auto:1",
                action="reject",
                pm_alias="operator",
            ),
        ),
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    signals = gather._signals_from_workiq_payload(
        payload={
            "meetings": [
                {
                    "link": "https://teams.microsoft.com/l/meeting/details?eventId=AAMkExampleEventId%3d%3d",
                    "title": "Pilot readiness review",
                    "summary": "Deployment remains blocked pending partner approval.",
                    "timestamp": "2026-05-10T09:00:00Z",
                }
            ]
        },
        query_name="feedback_search:contoso",
        question="Find current status",
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        items_by_id={},
        workstreams=workstreams,
        default_workstream_id=None,
        tracked_workstream_ids_by_m365_id=tracked_workstream_ids,
    )

    assert len(signals) == 1
    assert signals[0].workstream_id is None
    assert signals[0].entity_refs == ()


def test_gather_program_uses_ai_m365_topic_router_when_enabled(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    class _FakeAiRouter:
        def route_artifact(
            self,
            *,
            display_name: str | None,
            subject_or_title: str | None,
            participant_aliases: tuple[str, ...],
            sample_text: str | None,
            workstream_profiles: tuple[Workstream, ...],
            recent_confirmed_signals: dict[str, tuple[str, ...]] | None = None,
            recent_rejected_signals: dict[str, tuple[str, ...]] | None = None,
            recent_reassign_corrections=None,
        ) -> M365RoutingDecision:
            del display_name, subject_or_title, participant_aliases, sample_text, workstream_profiles, recent_confirmed_signals, recent_rejected_signals, recent_reassign_corrections
            return M365RoutingDecision(
                workstream_id="contoso",
                confidence=0.91,
                topics=("ai-router",),
                confidence_source="router",
                reasoning="AI router decision.",
            )

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        ai=AIConfig(
            enabled=True,
            budget_usd_per_run=0.25,
            exec_summary_deployment="exec-deployment",
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find current status",
            },
        ),
    )
    workstreams = (
        Workstream(id="contoso", name="Device delivery", signal_sources=WorkstreamSignalSources(workiq_keywords=("gfu",))),
        Workstream(id="acme", name="Store rollout", signal_sources=WorkstreamSignalSources(workiq_keywords=("schie",))),
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Device delivery. Keywords: gfu.": {"results": []},
            "Find current status. Focus on Store rollout. Keywords: schie.": {"results": []},
        },
        tool_payloads={
            "search_emails": {
                "emails": [
                    {
                        "messageId": "search-mail-1",
                        "threadId": "observed-email-thread-4",
                        "subject": "AI routed discovery thread",
                        "snippet": "This discovery should use the AI router when enabled.",
                        "receivedDateTime": "2026-05-10T09:00:00Z",
                    }
                ]
            },
        },
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(gather.M365TopicRouter, "from_program", classmethod(lambda cls, program: _FakeAiRouter()))

    gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: bridge,
    )

    registry = load_m365_registry("acme", programs_root)
    observed = next(artifact for artifact in registry.artifacts if artifact.thread_id == "observed-email-thread-4")

    assert observed.inferred_workstream == "contoso"
    assert observed.confidence == 0.91
    assert observed.routing_reasoning == "AI router decision."


def test_gather_program_caps_ai_routing_confidence_when_keyword_router_disagrees(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    class _FakeAiClient:
        def structured(self, system: str, user: str, *, parser, max_tokens: int = 800, prompt_version: str | None = None):
            del system, user, max_tokens, prompt_version
            return parser(
                {
                    "workstream_id": "contoso",
                    "confidence": 0.91,
                    "topics": ["firmware"],
                    "reasoning": "Firmware sign-off language aligns with Contoso.",
                }
            )

    class _FallbackRouter:
        def route_artifact(
            self,
            *,
            display_name: str | None,
            subject_or_title: str | None,
            participant_aliases: tuple[str, ...],
            sample_text: str | None,
            workstream_profiles: tuple[Workstream, ...],
            recent_confirmed_signals: dict[str, tuple[str, ...]] | None = None,
            recent_rejected_signals: dict[str, tuple[str, ...]] | None = None,
            recent_reassign_corrections=None,
        ) -> M365RoutingDecision:
            del display_name, subject_or_title, participant_aliases, sample_text, workstream_profiles, recent_confirmed_signals, recent_rejected_signals, recent_reassign_corrections
            return M365RoutingDecision(
                workstream_id="acme",
                confidence=0.44,
                topics=("fallback",),
                confidence_source="keyword",
                reasoning="Fallback router decision.",
            )

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        ai=AIConfig(
            enabled=True,
            budget_usd_per_run=0.25,
            exec_summary_deployment="exec-deployment",
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find current status",
            },
        ),
    )
    workstreams = (
        Workstream(id="contoso", name="Device delivery", signal_sources=WorkstreamSignalSources(workiq_keywords=("gfu",))),
        Workstream(id="acme", name="Store rollout", signal_sources=WorkstreamSignalSources(workiq_keywords=("schie",))),
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Device delivery. Keywords: gfu.": {"results": []},
            "Find current status. Focus on Store rollout. Keywords: schie.": {"results": []},
        },
        tool_payloads={
            "search_emails": {
                "emails": [
                    {
                        "messageId": "search-mail-1",
                        "threadId": "observed-email-thread-5",
                        "subject": "Firmware follow-up",
                        "snippet": "Firmware sign-off is still pending.",
                        "receivedDateTime": "2026-05-10T09:00:00Z",
                    }
                ]
            },
        },
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(
        gather.M365TopicRouter,
        "from_program",
        classmethod(lambda cls, program: gather.M365TopicRouter(client=_FakeAiClient(), fallback_router=_FallbackRouter())),
    )

    gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: bridge,
    )

    registry = load_m365_registry("acme", programs_root)
    observed = next(artifact for artifact in registry.artifacts if artifact.thread_id == "observed-email-thread-5")

    assert observed.inferred_workstream == "contoso"
    assert observed.confidence == 0.79
    assert observed.confidence_source == "router"
    assert "confidence was capped at 0.79 for review" in (observed.routing_reasoning or "")


def test_gather_program_passes_participant_aliases_to_discovery_router(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    captured: dict[str, tuple[str, ...]] = {}

    class _CapturingRouter:
        def route_artifact(
            self,
            *,
            display_name: str | None,
            subject_or_title: str | None,
            participant_aliases: tuple[str, ...],
            sample_text: str | None,
            workstream_profiles: tuple[Workstream, ...],
            recent_confirmed_signals: dict[str, tuple[str, ...]] | None = None,
            recent_rejected_signals: dict[str, tuple[str, ...]] | None = None,
            recent_reassign_corrections=None,
        ) -> M365RoutingDecision:
            del display_name, subject_or_title, sample_text, workstream_profiles, recent_confirmed_signals, recent_rejected_signals, recent_reassign_corrections
            captured["participant_aliases"] = participant_aliases
            return M365RoutingDecision(
                workstream_id="acme",
                confidence=0.72,
                topics=("participants",),
                confidence_source="router",
                reasoning="Participant aliases captured.",
            )

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find current status",
            },
        ),
    )
    workstreams = (
        Workstream(id="acme", name="Store rollout", signal_sources=WorkstreamSignalSources(workiq_keywords=("schie",))),
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Store rollout. Keywords: schie.": {"results": []},
        },
        tool_payloads={
            "search_emails": {
                "emails": [
                    {
                        "messageId": "search-mail-1",
                        "threadId": "observed-email-thread-6",
                        "subject": "Participant-aware discovery thread",
                        "snippet": "This discovery should carry participant aliases.",
                        "sender": {"emailAddress": {"address": "operator@example.com"}},
                        "toRecipients": [
                            {"emailAddress": {"address": "priya@example.com"}},
                            {"emailAddress": {"address": "operator@example.com"}},
                        ],
                        "receivedDateTime": "2026-05-10T09:00:00Z",
                    }
                ]
            },
        },
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: bridge,
        m365_topic_router=_CapturingRouter(),
    )

    assert captured["participant_aliases"] == ("operator", "priya")


def test_gather_program_augments_discovery_profiles_with_dependency_owner_aliases(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text(
        """
schema_version: "2.0"
id: acme
name: Adventure + DD on PF
people:
  - email: lidavidson@example.com
    display_name: James Davidson
    role: dependency_owner
    workstreams: [Store rollout]
""".strip(),
        encoding="utf-8",
    )

    captured: dict[str, tuple[Workstream, ...]] = {}

    class _CapturingRouter:
        def route_artifact(
            self,
            *,
            display_name: str | None,
            subject_or_title: str | None,
            participant_aliases: tuple[str, ...],
            sample_text: str | None,
            workstream_profiles: tuple[Workstream, ...],
            recent_confirmed_signals: dict[str, tuple[str, ...]] | None = None,
            recent_rejected_signals: dict[str, tuple[str, ...]] | None = None,
            recent_reassign_corrections=None,
        ) -> M365RoutingDecision:
            del display_name, subject_or_title, participant_aliases, sample_text, recent_confirmed_signals, recent_rejected_signals, recent_reassign_corrections
            captured["profiles"] = workstream_profiles
            return M365RoutingDecision(
                workstream_id="acme",
                confidence=0.72,
                topics=("owners",),
                confidence_source="router",
                reasoning="Dependency owner aliases captured.",
            )

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find current status",
            },
        ),
    )
    workstreams = (
        Workstream(id="acme", name="Store rollout", signal_sources=WorkstreamSignalSources(workiq_keywords=("schie",))),
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Store rollout. Keywords: schie.": {"results": []},
        },
        tool_payloads={
            "search_emails": {
                "emails": [
                    {
                        "messageId": "search-mail-owner-1",
                        "threadId": "observed-email-thread-owner-1",
                        "subject": "Owner-aware discovery thread",
                        "snippet": "This discovery should carry dependency owner aliases.",
                        "sender": {"emailAddress": {"address": "operator@example.com"}},
                        "receivedDateTime": "2026-05-10T09:00:00Z",
                    }
                ]
            },
        },
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: bridge,
        m365_topic_router=_CapturingRouter(),
    )

    assert "profiles" in captured
    assert captured["profiles"][0].aliases == ("lidavidson", "James Davidson")


def test_gather_program_passes_structured_reassign_corrections_to_discovery_router(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    captured: dict[str, object] = {}

    class _CapturingRouter:
        def route_artifact(
            self,
            *,
            display_name: str | None,
            subject_or_title: str | None,
            participant_aliases: tuple[str, ...],
            sample_text: str | None,
            workstream_profiles: tuple[Workstream, ...],
            recent_confirmed_signals: dict[str, tuple[str, ...]] | None = None,
            recent_rejected_signals: dict[str, tuple[str, ...]] | None = None,
            recent_reassign_corrections=None,
        ) -> M365RoutingDecision:
            del display_name, subject_or_title, participant_aliases, sample_text, workstream_profiles, recent_confirmed_signals, recent_rejected_signals
            captured["recent_reassign_corrections"] = recent_reassign_corrections
            return M365RoutingDecision(
                workstream_id="contoso",
                confidence=0.73,
                topics=("reassign",),
                confidence_source="router",
                reasoning="Structured reassign corrections captured.",
            )

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find current status",
            },
        ),
    )
    workstreams = (
        Workstream(id="acme", name="Store rollout", signal_sources=WorkstreamSignalSources(workiq_keywords=("schie",))),
        Workstream(id="contoso", name="Device delivery", signal_sources=WorkstreamSignalSources(workiq_keywords=("gfu",))),
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Store rollout. Keywords: schie.": {"results": []},
            "Find current status. Focus on Device delivery. Keywords: gfu.": {"results": []},
        },
        tool_payloads={
            "search_emails": {
                "emails": [
                    {
                        "messageId": "search-mail-1",
                        "threadId": "observed-email-thread-reassign",
                        "subject": "DD pilot readiness",
                        "snippet": "This text should carry structured reassign context.",
                        "receivedDateTime": "2026-05-10T09:00:00Z",
                    }
                ]
            },
        },
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    feedback_dir = programs_root / "acme" / "_feedback"
    feedback_dir.mkdir(parents=True, exist_ok=True)
    (feedback_dir / "m365_routing_feedback.jsonl").write_text(
        '{"ts":"2026-05-09T09:00:00Z","artifact_id":"thread:auto:abc12345","action":"reassign","pm_alias":"operator","workstream_id":"contoso","prior_workstream_id":"acme","reason":"Belongs with DD pilot execution."}\n',
        encoding="utf-8",
    )
    registry_path = programs_root / "acme" / "m365_registry.yaml"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        """
schema_version: "1.0"
program_id: acme
artifacts:
  - artifact_id: thread:auto:abc12345
    artifact_type: email_thread
    inferred_workstream: contoso
    confidence: 0.91
    confidence_source: pm_confirmed
    pm_confirmed: true
    promoted_to_workstreams_yaml: false
    first_seen: "2026-05-08"
    last_seen: "2026-05-08"
    display_name: DD pilot readiness thread
    routing_reasoning: Belongs with DD pilot execution.
""".strip(),
        encoding="utf-8",
    )

    gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: bridge,
        m365_topic_router=_CapturingRouter(),
    )

    corrections = captured["recent_reassign_corrections"]
    assert corrections is not None
    assert len(corrections["contoso"]) == 1
    correction = corrections["contoso"][0]
    assert correction.prior_workstream_id == "acme"
    assert correction.corrected_workstream_id == "contoso"
    assert correction.artifact_display_name == "DD pilot readiness thread"


def test_gather_program_uses_participant_aliases_in_deterministic_discovery_routing(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find current status",
            },
        ),
    )
    workstreams = (
        Workstream(id="acme", name="Store rollout", pm_owner="operator", signal_sources=WorkstreamSignalSources(workiq_keywords=("schie",))),
        Workstream(id="contoso", name="Device delivery", pm_owner="priya", signal_sources=WorkstreamSignalSources(workiq_keywords=("gfu",))),
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Store rollout. Keywords: schie.": {"results": []},
            "Find current status. Focus on Device delivery. Keywords: gfu.": {"results": []},
        },
        tool_payloads={
            "search_emails": {
                "emails": [
                    {
                        "messageId": "search-mail-1",
                        "threadId": "observed-email-thread-7",
                        "subject": "General sync follow-up",
                        "snippet": "No configured keywords are present in this discovery payload.",
                        "sender": {"emailAddress": {"address": "priya@example.com"}},
                        "receivedDateTime": "2026-05-10T09:00:00Z",
                    }
                ]
            },
        },
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: bridge,
    )

    registry = load_m365_registry("acme", programs_root)
    observed = next(artifact for artifact in registry.artifacts if artifact.thread_id == "observed-email-thread-7")

    assert observed.inferred_workstream == "contoso"
    assert observed.confidence == 0.15
    assert "participant aliases ('priya',)" in (observed.routing_reasoning or "")


def test_gather_program_uses_area_path_anchors_in_deterministic_discovery_routing(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find current status",
            },
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Store rollout",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(workiq_keywords=("ramp", "planning")),
        ),
        Workstream(
            id="contoso",
            name="Device delivery",
            area_paths=("One\\Adventure\\Contoso\\Networking",),
        ),
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Store rollout. Keywords: ramp, planning.": {"results": []},
            "Find current status. Focus on Device delivery. Keywords: (none).": {"results": []},
        },
        tool_payloads={
            "search_emails": {
                "emails": [
                    {
                        "messageId": "search-mail-1",
                        "threadId": "observed-email-thread-8",
                        "subject": "Ramp review for Contoso networking",
                        "snippet": "One\\Adventure\\Contoso\\Networking blockers remain on the critical path.",
                        "receivedDateTime": "2026-05-10T09:00:00Z",
                    }
                ]
            },
        },
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: bridge,
    )

    registry = load_m365_registry("acme", programs_root)
    observed = next(artifact for artifact in registry.artifacts if artifact.thread_id == "observed-email-thread-8")

    assert observed.inferred_workstream == "contoso"
    assert observed.confidence > 0.57
    assert "area-path anchors" in (observed.routing_reasoning or "")


def test_gather_program_uses_rejected_feedback_to_penalize_future_discovery_routing(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find current status",
            },
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Store rollout",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(workiq_keywords=("ramp",)),
        ),
        Workstream(
            id="contoso",
            name="Device delivery",
            area_paths=("One\\Adventure\\Contoso\\Networking",),
        ),
    )
    historical_artifact = M365RegistryArtifact(
        artifact_id=build_auto_thread_artifact_id("historical-thread-1"),
        artifact_type="email_thread",
        inferred_workstream="acme",
        confidence=0.61,
        confidence_source="keyword",
        pm_confirmed=False,
        promoted_to_workstreams_yaml=False,
        first_seen=date(2026, 5, 1),
        last_seen=date(2026, 5, 1),
        display_name="Ramp finance planning follow-up",
        thread_id="historical-thread-1",
        routing_reasoning="Routed to Acme from ramp keyword overlap.",
    )
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(historical_artifact,),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
    )
    apply_m365_routing_feedback(
        "acme",
        event=M365RoutingFeedbackEvent(
            ts=datetime(2026, 5, 2, 9, 0, tzinfo=timezone.utc),
            artifact_id=historical_artifact.artifact_id,
            action="reject",
            pm_alias="operator",
            workstream_id="acme",
            reason="Finance planning was rejected as off-topic for store rollout.",
            thread_id=historical_artifact.thread_id,
        ),
        programs_root=programs_root,
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Store rollout. Keywords: ramp.": {"results": []},
            "Find current status. Focus on Device delivery. Keywords: (none).": {"results": []},
        },
        tool_payloads={
            "search_emails": {
                "emails": [
                    {
                        "messageId": "search-mail-1",
                        "threadId": "observed-email-thread-9",
                        "subject": "Ramp finance planning for Contoso networking",
                        "snippet": "One\\Adventure\\Contoso\\Networking blockers remain active while finance planning is discussed.",
                        "receivedDateTime": "2026-05-10T09:00:00Z",
                    }
                ]
            },
        },
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: bridge,
    )

    registry = load_m365_registry("acme", programs_root)
    observed = next(artifact for artifact in registry.artifacts if artifact.thread_id == "observed-email-thread-9")

    assert observed.inferred_workstream == "contoso"
    assert observed.confidence > 0.57


def test_gather_program_ignores_stale_rejected_feedback_in_discovery_routing(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find current status",
            },
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Store rollout",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(workiq_keywords=("ramp", "planning")),
        ),
        Workstream(
            id="contoso",
            name="Device delivery",
            area_paths=("One\\Adventure\\Contoso\\Networking",),
        ),
    )
    historical_artifact = M365RegistryArtifact(
        artifact_id=build_auto_thread_artifact_id("historical-thread-2"),
        artifact_type="email_thread",
        inferred_workstream="acme",
        confidence=0.61,
        confidence_source="keyword",
        pm_confirmed=False,
        promoted_to_workstreams_yaml=False,
        first_seen=date(2026, 3, 1),
        last_seen=date(2026, 3, 1),
        display_name="Ramp finance planning follow-up",
        thread_id="historical-thread-2",
        routing_reasoning="Routed to Acme from ramp keyword overlap.",
    )
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(historical_artifact,),
        programs_root=programs_root,
        as_of=datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc),
    )
    apply_m365_routing_feedback(
        "acme",
        event=M365RoutingFeedbackEvent(
            ts=datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc),
            artifact_id=historical_artifact.artifact_id,
            action="reject",
            pm_alias="operator",
            workstream_id="acme",
            reason="Finance planning was rejected as off-topic for store rollout.",
            thread_id=historical_artifact.thread_id,
        ),
        programs_root=programs_root,
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Store rollout. Keywords: ramp, planning.": {"results": []},
            "Find current status. Focus on Device delivery. Keywords: (none).": {"results": []},
        },
        tool_payloads={
            "search_emails": {
                "emails": [
                    {
                        "messageId": "search-mail-1",
                        "threadId": "observed-email-thread-10",
                        "subject": "Ramp finance planning for Contoso networking",
                        "snippet": "One\\Adventure\\Contoso\\Networking blockers remain active while finance planning is discussed.",
                        "receivedDateTime": "2026-05-10T09:00:00Z",
                    }
                ]
            },
        },
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: bridge,
    )

    registry = load_m365_registry("acme", programs_root)
    observed = next(artifact for artifact in registry.artifacts if artifact.thread_id == "observed-email-thread-10")

    assert observed.inferred_workstream == "acme"
    assert observed.confidence > 0.6


def test_gather_program_uses_reassign_feedback_as_directional_negative_evidence(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find current status",
            },
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Store rollout",
            signal_sources=WorkstreamSignalSources(workiq_keywords=("pilot",)),
        ),
        Workstream(
            id="contoso",
            name="Device delivery",
            area_paths=("One\\Adventure\\Contoso\\Networking",),
        ),
    )
    historical_artifact = M365RegistryArtifact(
        artifact_id=build_auto_thread_artifact_id("historical-thread-3"),
        artifact_type="email_thread",
        inferred_workstream="acme",
        confidence=0.61,
        confidence_source="keyword",
        pm_confirmed=False,
        promoted_to_workstreams_yaml=False,
        first_seen=date(2026, 5, 1),
        last_seen=date(2026, 5, 1),
        display_name="DD pilot readiness thread",
        thread_id="historical-thread-3",
        routing_reasoning="Routed to Acme from pilot keyword overlap.",
    )
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(historical_artifact,),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
    )
    apply_m365_routing_feedback(
        "acme",
        event=M365RoutingFeedbackEvent(
            ts=datetime(2026, 5, 2, 9, 0, tzinfo=timezone.utc),
            artifact_id=historical_artifact.artifact_id,
            action="reassign",
            pm_alias="operator",
            workstream_id="contoso",
            reason="Belongs with DD pilot execution.",
            thread_id=historical_artifact.thread_id,
        ),
        programs_root=programs_root,
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Store rollout. Keywords: pilot.": {"results": []},
            "Find current status. Focus on Device delivery. Keywords: (none).": {"results": []},
        },
        tool_payloads={
            "search_emails": {
                "emails": [
                    {
                        "messageId": "search-mail-1",
                        "threadId": "observed-email-thread-11",
                        "subject": "DD pilot readiness thread",
                        "snippet": "DD pilot execution remains blocked for One\\Adventure\\Contoso\\Networking.",
                        "receivedDateTime": "2026-05-10T09:00:00Z",
                    }
                ]
            },
        },
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: bridge,
    )

    registry = load_m365_registry("acme", programs_root)
    observed = next(artifact for artifact in registry.artifacts if artifact.thread_id == "observed-email-thread-11")

    assert observed.inferred_workstream == "contoso"
    assert observed.confidence > 0.57


def test_gather_program_refreshes_registry_signal_yield_and_decays_stale_unconfirmed_threads(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find current status",
            },
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(
                workiq_keywords=("SCHIE",),
            ),
        ),
    )
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(
            M365RegistryArtifact(
                artifact_id="thread:auto:observed1",
                artifact_type="email_thread",
                display_name="Observed thread",
                thread_id="observed-email-thread-1",
                inferred_workstream="acme",
                confidence=0.70,
                confidence_source="keyword",
                pm_confirmed=False,
                promoted_to_workstreams_yaml=False,
                first_seen=date(2026, 5, 8),
                last_seen=date(2026, 5, 9),
                signal_yield_last_3=(1, 1, 1),
                topics=("SCHIE",),
            ),
            M365RegistryArtifact(
                artifact_id="thread:auto:stale001",
                artifact_type="email_thread",
                display_name="Stale thread",
                thread_id="stale-thread-1",
                inferred_workstream="acme",
                confidence=0.70,
                confidence_source="keyword",
                pm_confirmed=False,
                promoted_to_workstreams_yaml=False,
                first_seen=date(2026, 5, 8),
                last_seen=date(2026, 5, 9),
                signal_yield_last_3=(1, 1, 1),
                topics=("SCHIE",),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc),
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Adventure on Northwind. Keywords: SCHIE.": {
                "results": [
                    {
                        "messageId": "mail-1",
                        "threadId": "observed-email-thread-1",
                        "title": "SCHIE weekly follow-up",
                        "snippet": "Northwind ramp review is blocked on SCHIE.",
                        "timestamp": "2026-05-10T09:00:00Z",
                    }
                ]
            },
        },
        tool_payloads={
            "search_emails": {"emails": []},
        },
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: bridge,
    )

    registry = load_m365_registry("acme", programs_root)
    observed = next(artifact for artifact in registry.artifacts if artifact.thread_id == "observed-email-thread-1")
    stale = next(artifact for artifact in registry.artifacts if artifact.thread_id == "stale-thread-1")

    assert observed.signal_yield_last_3 == (1, 1, 1)
    assert observed.confidence == 0.70
    assert observed.last_seen == date(2026, 5, 10)
    assert stale.signal_yield_last_3 == (1, 1, 0)
    assert stale.confidence == 0.65


def test_gather_program_reroutes_tracked_low_confidence_registry_threads_when_fresh_hits_arrive(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    captured: dict[str, object] = {}

    class _FakeRouter:
        def route_artifact(
            self,
            *,
            display_name: str | None,
            subject_or_title: str | None,
            participant_aliases: tuple[str, ...],
            sample_text: str | None,
            workstream_profiles: tuple[Workstream, ...],
            recent_confirmed_signals: dict[str, tuple[str, ...]] | None = None,
            recent_rejected_signals: dict[str, tuple[str, ...]] | None = None,
            recent_reassign_corrections=None,
        ) -> M365RoutingDecision:
            del display_name, subject_or_title, sample_text, workstream_profiles, recent_confirmed_signals, recent_rejected_signals, recent_reassign_corrections
            captured["participant_aliases"] = participant_aliases
            return M365RoutingDecision(
                workstream_id="contoso",
                confidence=0.83,
                topics=("pilot",),
                confidence_source="router",
                reasoning="Fresh thread hit aligns with Contoso pilot execution.",
            )

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find current status",
            },
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Store rollout",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(workiq_keywords=("pilot",)),
        ),
        Workstream(
            id="contoso",
            name="Device delivery",
            area_paths=("One\\Adventure\\Contoso",),
            signal_sources=WorkstreamSignalSources(workiq_keywords=()),
        ),
    )
    stale_artifact = M365RegistryArtifact(
        artifact_id=build_auto_thread_artifact_id("tracked-thread-1"),
        artifact_type="email_thread",
        inferred_workstream="acme",
        confidence=0.55,
        confidence_source="keyword",
        pm_confirmed=False,
        promoted_to_workstreams_yaml=False,
        first_seen=date(2026, 5, 1),
        last_seen=date(2026, 5, 9),
        display_name="DD pilot readiness thread",
        thread_id="tracked-thread-1",
        routing_reasoning="Earlier pilot keyword overlap favored Acme.",
    )
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(stale_artifact,),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc),
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Store rollout. Keywords: pilot.": {
                "results": [
                    {
                        "messageId": "tracked-mail-1",
                        "threadId": "tracked-thread-1",
                        "subject": "DD pilot readiness thread",
                        "snippet": "DD pilot execution remains blocked for One\\Adventure\\Contoso\\Networking.",
                        "sender": {"emailAddress": {"address": "operator@example.com"}},
                        "toRecipients": [{"emailAddress": {"address": "priya@example.com"}}],
                        "receivedDateTime": "2026-05-10T09:00:00Z",
                    }
                ]
            },
            "Find current status. Focus on Device delivery. Keywords: .": {"results": []},
        },
        tool_payloads={
            "search_emails": {"emails": []},
        },
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: bridge,
        m365_topic_router=_FakeRouter(),
    )

    registry = load_m365_registry("acme", programs_root=programs_root)
    rerouted = next(artifact for artifact in registry.artifacts if artifact.thread_id == "tracked-thread-1")
    signals = read_signals("acme", programs_root=programs_root)
    tracked_signal = next(signal for signal in signals if signal.thread_id == "tracked-thread-1")

    assert rerouted.inferred_workstream == "contoso"
    assert rerouted.confidence == 0.83
    assert rerouted.confidence_source == "router"
    assert rerouted.routing_reasoning == "Fresh thread hit aligns with Contoso pilot execution."
    assert captured["participant_aliases"] == ("operator", "priya")
    assert tracked_signal.workstream_id == "contoso"
    assert tracked_signal.entity_refs == ("WS:contoso",)
    assert tracked_signal.metadata is not None
    assert tracked_signal.metadata["participant_aliases"] == "operator,priya"
    assert tracked_signal.metadata["routed_workstream_id"] == "contoso"


def test_reroute_low_confidence_registry_artifacts_skips_recently_rejected_threads(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    class _FakeRouter:
        def route_artifact(
            self,
            *,
            display_name: str | None,
            subject_or_title: str | None,
            participant_aliases: tuple[str, ...],
            sample_text: str | None,
            workstream_profiles: tuple[Workstream, ...],
            recent_confirmed_signals: dict[str, tuple[str, ...]] | None = None,
            recent_rejected_signals: dict[str, tuple[str, ...]] | None = None,
            recent_reassign_corrections=None,
        ) -> M365RoutingDecision:
            del display_name, subject_or_title, participant_aliases, sample_text, workstream_profiles, recent_confirmed_signals, recent_rejected_signals, recent_reassign_corrections
            return M365RoutingDecision(
                workstream_id="contoso",
                confidence=0.83,
                topics=("pilot",),
                confidence_source="router",
                reasoning="Fresh thread hit aligns with Contoso pilot execution.",
            )

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find current status",
            },
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Store rollout",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(workiq_keywords=("pilot",)),
        ),
        Workstream(
            id="contoso",
            name="Device delivery",
            area_paths=("One\\Adventure\\Contoso",),
            signal_sources=WorkstreamSignalSources(workiq_keywords=()),
        ),
    )
    stale_artifact = M365RegistryArtifact(
        artifact_id=build_auto_thread_artifact_id("tracked-thread-rejected"),
        artifact_type="email_thread",
        inferred_workstream="acme",
        confidence=0.55,
        confidence_source="keyword",
        pm_confirmed=False,
        promoted_to_workstreams_yaml=False,
        first_seen=date(2026, 5, 1),
        last_seen=date(2026, 5, 9),
        display_name="DD pilot readiness thread",
        thread_id="tracked-thread-rejected",
        routing_reasoning="Earlier pilot keyword overlap favored Acme.",
    )
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(stale_artifact,),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc),
    )
    feedback_path = programs_root / "acme" / "_feedback" / "m365_routing_feedback.jsonl"
    feedback_path.parent.mkdir(parents=True, exist_ok=True)
    feedback_path.write_text(
        f'{{"ts": "2026-05-10T07:30:00+00:00", "artifact_id": "{stale_artifact.artifact_id}", "action": "reject", "pm_alias": "operator", "workstream_id": null, "topics": [], "reason": "off track", "series_id": null, "thread_id": null}}\n',
        encoding="utf-8",
    )
    rerouted_signals = gather._reroute_low_confidence_registry_artifacts(
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        workstreams=workstreams,
        topic_router=_FakeRouter(),
        observed_signals=(
            Signal(
                id="tracked-mail-rejected",
                timestamp=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
                source="workiq/email",
                program_id="acme",
                workstream_id="acme",
                entity_refs=("WS:acme",),
                text="DD pilot execution remains blocked for One\\Adventure\\Contoso\\Networking.",
                raw_ref="workiq:email:tracked-mail-rejected",
                confidence=Confidence.MEDIUM,
                metadata={"participant_aliases": "operator,priya", "message_id": "tracked-mail-rejected"},
                thread_id="tracked-thread-rejected",
            ),
        ),
        programs_root=programs_root,
    )

    registry = load_m365_registry("acme", programs_root=programs_root)
    rerouted = next(artifact for artifact in registry.artifacts if artifact.thread_id == "tracked-thread-rejected")

    assert rerouted_signals[0].workstream_id == "acme"
    assert rerouted_signals[0].entity_refs == ("WS:acme",)
    assert rerouted.inferred_workstream == "acme"
    assert rerouted.confidence == 0.55
    assert rerouted.confidence_source == "keyword"
    assert rerouted.routing_reasoning == "Earlier pilot keyword overlap favored Acme."


def test_gather_program_attributes_tracked_registry_threads_to_registry_workstream(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find current status",
            },
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Store rollout",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(workiq_keywords=("pilot",)),
        ),
        Workstream(
            id="contoso",
            name="Device delivery",
            area_paths=("One\\Adventure\\Contoso",),
            signal_sources=WorkstreamSignalSources(workiq_keywords=()),
        ),
    )
    tracked_artifact = M365RegistryArtifact(
        artifact_id=build_auto_thread_artifact_id("tracked-thread-2"),
        artifact_type="email_thread",
        inferred_workstream="contoso",
        confidence=0.92,
        confidence_source="router",
        pm_confirmed=False,
        promoted_to_workstreams_yaml=False,
        first_seen=date(2026, 5, 1),
        last_seen=date(2026, 5, 9),
        display_name="DD control plane thread",
        thread_id="tracked-thread-2",
        routing_reasoning="Previously classified to Contoso.",
    )
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(tracked_artifact,),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc),
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Store rollout. Keywords: pilot.": {
                "results": [
                    {
                        "messageId": "tracked-mail-2",
                        "threadId": "tracked-thread-2",
                        "subject": "DD control plane thread",
                        "snippet": "Control plane rollout remains blocked for Contoso pilot readiness.",
                        "receivedDateTime": "2026-05-10T09:00:00Z",
                    }
                ]
            },
            "Find current status. Focus on Device delivery. Keywords: .": {"results": []},
        },
        tool_payloads={
            "search_emails": {"emails": []},
        },
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: bridge,
    )

    signals = read_signals("acme", programs_root=programs_root)
    tracked_signal = next(signal for signal in signals if signal.thread_id == "tracked-thread-2")

    assert tracked_signal.workstream_id == "contoso"
    assert tracked_signal.metadata is not None
    assert tracked_signal.metadata["queried_workstream_id"] == "acme"
    assert tracked_signal.metadata["routed_workstream_id"] is None


def test_gather_program_routes_unanchored_workiq_hits_before_falling_back_to_query_scope(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    captured: dict[str, object] = {}

    class _CapturingRouter:
        def route_artifact(
            self,
            *,
            display_name: str | None,
            subject_or_title: str | None,
            participant_aliases: tuple[str, ...],
            sample_text: str | None,
            workstream_profiles: tuple[Workstream, ...],
            recent_confirmed_signals: dict[str, tuple[str, ...]] | None = None,
            recent_rejected_signals: dict[str, tuple[str, ...]] | None = None,
            recent_reassign_corrections=None,
        ) -> M365RoutingDecision:
            del display_name, subject_or_title, sample_text, workstream_profiles, recent_confirmed_signals, recent_rejected_signals, recent_reassign_corrections
            captured["participant_aliases"] = participant_aliases
            return M365RoutingDecision(
                workstream_id="contoso",
                confidence=0.74,
                topics=("pilot",),
                confidence_source="router",
                reasoning="Participant and text cues align with Contoso.",
            )

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find current status",
            },
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Store rollout",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(workiq_keywords=("pilot",)),
        ),
        Workstream(
            id="contoso",
            name="Device delivery",
            area_paths=("One\\Adventure\\Contoso",),
            signal_sources=WorkstreamSignalSources(workiq_keywords=()),
        ),
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Store rollout. Keywords: pilot.": {
                "results": [
                    {
                        "messageId": "unanchored-mail-1",
                        "threadId": "unanchored-thread-1",
                        "subject": "Pilot readiness follow-up",
                        "snippet": "Contoso validation remains blocked even though no work item ID was cited.",
                        "sender": {"emailAddress": {"address": "priya@example.com"}},
                        "toRecipients": [{"emailAddress": {"address": "operator@example.com"}}],
                        "receivedDateTime": "2026-05-10T09:00:00Z",
                    }
                ]
            },
            "Find current status. Focus on Device delivery. Keywords: .": {"results": []},
        },
        tool_payloads={
            "search_emails": {"emails": []},
        },
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: bridge,
        m365_topic_router=_CapturingRouter(),
    )

    signals = read_signals("acme", programs_root=programs_root)
    observed = next(signal for signal in signals if signal.thread_id == "unanchored-thread-1")

    assert captured["participant_aliases"] == ("priya", "operator")
    assert observed.workstream_id == "contoso"
    assert observed.entity_refs == ("WS:contoso",)
    assert observed.metadata is not None
    assert observed.metadata["queried_workstream_id"] == "acme"
    assert observed.metadata["routed_workstream_id"] == "contoso"

def test_gather_program_records_current_m365_promotion_candidates(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find current status",
            },
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(
                workiq_keywords=("SCHIE",),
            ),
        ),
    )
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(
            M365RegistryArtifact(
                artifact_id="chan:acme-promote-ready",
                artifact_type="teams_channel",
                display_name="Promotion Ready Chat",
                thread_id="promote-thread-1",
                inferred_workstream="acme",
                confidence=1.0,
                confidence_source="pm_confirmed",
                pm_confirmed=True,
                promoted_to_workstreams_yaml=False,
                first_seen=date(2026, 5, 8),
                last_seen=date(2026, 5, 9),
                signal_yield_last_3=(1, 2, 1),
                topics=("SCHIE",),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc),
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Adventure on Northwind. Keywords: SCHIE.": {"results": []},
        },
        tool_payloads={
            "search_emails": {"emails": []},
        },
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    artifacts = gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: bridge,
    )

    gather_state = load_gather_state("acme", programs_root=programs_root)

    assert artifacts.promotion_candidates == (
        gather.M365PromotionCandidate(
            artifact_id="chan:acme-promote-ready",
            display_name="Promotion Ready Chat",
            workstream_id="acme",
            confidence=1.0,
            signal_yield_last_3=(2, 1, 0),
        ),
    )
    assert gather_state is not None
    assert gather_state.m365_discovery["promotion_candidate_count"] == 1
    assert gather_state.m365_discovery["promotion_candidate_ids"] == ["chan:acme-promote-ready"]
    assert gather_state.m365_discovery["promotion_blocked_recent_rejection_count"] == 0
    assert gather_state.m365_discovery["promotion_blocked_missing_id_count"] == 0
    assert gather_state.m365_discovery["promotion_blocked_signal_yield_count"] == 0


def test_gather_program_excludes_recently_rejected_artifacts_from_promotion_candidates(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find current status",
            },
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(
                workiq_keywords=("SCHIE",),
            ),
        ),
    )
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(
            M365RegistryArtifact(
                artifact_id="chan:acme-promote-ready",
                artifact_type="teams_channel",
                display_name="Promotion Ready Chat",
                thread_id="promote-thread-1",
                inferred_workstream="acme",
                confidence=1.0,
                confidence_source="pm_confirmed",
                pm_confirmed=True,
                promoted_to_workstreams_yaml=False,
                first_seen=date(2026, 5, 8),
                last_seen=date(2026, 5, 9),
                signal_yield_last_3=(1, 2, 1),
                topics=("SCHIE",),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc),
    )
    feedback_path = programs_root / "acme" / "_feedback" / "m365_routing_feedback.jsonl"
    feedback_path.parent.mkdir(parents=True, exist_ok=True)
    feedback_path.write_text(
        '{"ts": "2026-05-09T18:00:00+00:00", "artifact_id": "chan:acme-promote-ready", "action": "reject", "pm_alias": "operator", "workstream_id": null, "topics": [], "reason": "off topic", "series_id": null, "thread_id": null}\n',
        encoding="utf-8",
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Adventure on Northwind. Keywords: SCHIE.": {"results": []},
        },
        tool_payloads={
            "search_emails": {"emails": []},
        },
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    artifacts = gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: bridge,
    )

    gather_state = load_gather_state("acme", programs_root=programs_root)

    assert artifacts.promotion_candidates == ()
    assert gather_state is not None
    assert gather_state.m365_discovery["promotion_candidate_count"] == 0
    assert gather_state.m365_discovery["promotion_candidate_ids"] == []
    assert gather_state.m365_discovery["promotion_blocked_recent_rejection_count"] == 1
    assert gather_state.m365_discovery["promotion_blocked_recent_rejection_ids"] == ["chan:acme-promote-ready"]


def test_gather_program_persists_structured_missing_id_promotion_blockers(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find current status",
            },
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(
                workiq_keywords=("SCHIE",),
            ),
        ),
    )
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(
            M365RegistryArtifact(
                artifact_id="meet:acme-acme-weekly-ops-review",
                artifact_type="meeting_series",
                display_name="Acme Weekly Ops Review",
                series_id=None,
                inferred_workstream="acme",
                confidence=1.0,
                confidence_source="pm_confirmed",
                pm_confirmed=True,
                promoted_to_workstreams_yaml=True,
                first_seen=date(2026, 5, 8),
                last_seen=date(2026, 5, 9),
                signal_yield_last_3=(0, 0, 0),
                topics=("SCHIE",),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc),
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Adventure on Northwind. Keywords: SCHIE.": {"results": []},
        },
        tool_payloads={
            "search_emails": {"emails": []},
        },
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: bridge,
    )

    gather_state = load_gather_state("acme", programs_root=programs_root)

    assert gather_state is not None
    assert gather_state.m365_discovery["first_discovery_completed_at"] == "2026-05-10T08:00:00+00:00"
    assert gather_state.m365_discovery["promotion_blocked_missing_id_count"] == 1
    assert gather_state.m365_discovery["promotion_blocked_missing_id_ids"] == ["meet:acme-acme-weekly-ops-review"]
    assert gather_state.m365_discovery["promotion_blocked_missing_id_artifacts"] == [
        {
            "artifact_id": "meet:acme-acme-weekly-ops-review",
            "artifact_type": "meeting_series",
            "inferred_workstream": "acme",
        }
    ]


def test_build_m365_discovery_state_uses_seeded_attempt_timestamp_for_first_completion(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(enabled=True, prefer_agency=True, workiq_queries={}),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            signal_sources=WorkstreamSignalSources(
                teams_chats=(TeamsChat(display_name="Acme Eng Core Chat", thread_id=None),),
            ),
        ),
    )
    store = SourceCandidateStore(programs_root / "acme" / "channel_registry.sqlite3", "acme")
    attempted_at = datetime(2026, 5, 10, 8, 15, tzinfo=timezone.utc)
    store.bootstrap_intents(workstreams=workstreams, registry_artifacts=(), as_of=attempted_at)
    intent = store.list_intents(workstream_id="acme", ref_kind=SourceRefKind.TEAMS_CHAT)[0]
    store.record_attempt(
        DiscoveryAttempt(
            attempt_id=build_discovery_attempt_id(
                program_id="acme",
                intent_id=intent.intent_id,
                source_provider="seeded_resolution",
                query_hash="query",
                attempted_at=attempted_at,
            ),
            program_id="acme",
            intent_id=intent.intent_id,
            workstream_id=intent.workstream_id,
            channel="teams",
            provider_instance_id="default",
            ref_kind=SourceRefKind.TEAMS_CHAT,
            source_provider="seeded_resolution",
            query_hash="query",
            config_hash="config",
            autonomous_run_id=None,
            outcome=DiscoveryAttemptOutcome.NO_CANDIDATES,
            reason=None,
            result_count=0,
            duration_ms=10,
            attempted_at=attempted_at,
        )
    )

    state = gather._build_m365_discovery_state(
        program_id="acme",
        programs_root=programs_root,
        program=program,
        workstreams=workstreams,
        workiq_signals=(),
        gather_flags={"workiq": True},
        as_of=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
        previous_entry=None,
    )

    assert state["query_plan_count"] == 0
    assert state["first_discovery_completed_at"] == "2026-05-10T08:15:00+00:00"
    assert state["seeded_resolution_attempt_count"] == 1
    assert state["seeded_resolution_attempted_intent_count"] == 1
    assert state["seeded_resolution_outcome_counts"] == {"no_candidates": 1}
    assert state["seeded_resolution_latest_attempts"] == [
        {
            "intent_id": intent.intent_id,
            "display_name": "Acme Eng Core Chat",
            "ref_kind": "teams_chat",
            "workstream_id": "acme",
            "outcome": "no_candidates",
            "reason": None,
            "result_count": 0,
            "attempted_at": "2026-05-10T08:15:00+00:00",
        }
    ]


def test_build_m365_discovery_state_includes_adaptive_learning_snapshot(tmp_path: Path) -> None:
    from src.core.channel_registry_store import ChannelRegistryStore
    from src.core.integration_types import (
        ChannelRegistration,
        DiscoveredRef,
        DiscoveryCompleteness,
        DiscoveryResult,
        RegistrationBinding,
        RegistrationStatus,
        ScopeStatus,
        ScopeStatusKind,
    )
    from src.core.milestone_engine import save_milestones

    programs_root = tmp_path / "programs"
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(enabled=True, prefer_agency=True, workiq_queries={}),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            area_paths=("One\\Adventure\\Acme",),
            pm_owner="operator",
            signal_sources=WorkstreamSignalSources(),
        ),
    )
    items = (
        WorkItem(
            id=101,
            type="Feature",
            title="Northwind Launch Readiness",
            state="Active",
            assigned_to=None,
            assigned_to_email=None,
            area_path="One\\Adventure\\Acme",
            iteration_path="One\\Adventure\\Acme\\Sprint 1",
            target_date=None,
            risk_level=RiskLevel.UNKNOWN,
            tags=[],
            custom_fields={},
            fetched_at=datetime(2026, 6, 3, tzinfo=timezone.utc),
        ),
    )
    save_milestones(
        "acme",
        (
            Milestone(
                id="ms1",
                program_id="acme",
                name="Northwind Launch GA",
                target_date=date(2026, 6, 20),
                owner_alias="operator",
                status=MilestoneStatus.ON_TRACK,
                exit_criteria=(),
                linked_workstream_ids=("acme",),
                linked_work_item_ids=(),
            ),
        ),
        programs_root=programs_root,
    )
    registry_artifacts = (
        M365RegistryArtifact(
            artifact_id="thread:named:acme-northwind-launch",
            artifact_type="email_thread",
            inferred_workstream="acme",
            confidence=1.0,
            confidence_source="pm_confirmed",
            pm_confirmed=True,
            promoted_to_workstreams_yaml=False,
            first_seen=date(2026, 6, 1),
            last_seen=date(2026, 6, 3),
            display_name="Northwind Launch Thread",
            thread_id="mail-thread-1",
            routing_reasoning="Northwind launch thread",
        ),
        M365RegistryArtifact(
            artifact_id="thread:named:acme-noisy-thread",
            artifact_type="email_thread",
            inferred_workstream="acme",
            confidence=0.2,
            confidence_source="pm_rejected",
            pm_confirmed=False,
            promoted_to_workstreams_yaml=False,
            first_seen=date(2026, 6, 1),
            last_seen=date(2026, 6, 3),
            display_name="Legacy deck thread",
            thread_id="mail-thread-noisy",
            routing_reasoning="Legacy deck chatter",
        ),
    )
    feedback_events = (
        M365RoutingFeedbackEvent(
            ts=datetime(2026, 6, 3, 8, 0, tzinfo=timezone.utc),
            artifact_id="thread:named:acme-noisy-thread",
            action="reject",
            pm_alias="operator",
            reason="Legacy deck chatter",
        ),
    )

    store = ChannelRegistryStore(programs_root / "acme" / "channel_registry.sqlite3", "acme")
    now = datetime(2026, 6, 3, 8, 30, tzinfo=timezone.utc)
    discovered = DiscoveredRef(
        registration=ChannelRegistration(
            channel="email",
            program_id="acme",
            provider_instance_id="default",
            ref_id="mail-thread-1",
            ref_kind="email_thread",
            status=RegistrationStatus.ACTIVE,
            first_discovered_at=now,
            last_seen_at=now,
            confidence=1.0,
            confidence_source="manual_config",
            signal_yield_last_3=(2, 1, 0),
            ref_title="Northwind Launch Thread",
            workstream_ids=("acme",),
        ),
        bindings=(
            RegistrationBinding(
                workstream_id="acme",
                scope_id="scope",
                source_type="manual_config",
                confidence=1.0,
                confidence_source="manual_config",
                signal_yield_last_3=(2, 1, 0),
            ),
        ),
    )
    store.apply_discovery_result(
        DiscoveryResult(
            channel="email",
            program_id="acme",
            discovered_refs=(discovered,),
            completeness=DiscoveryCompleteness.FULL,
            scope_statuses={
                "scope": ScopeStatus(
                    scope_id="scope",
                    status=ScopeStatusKind.SUCCESS,
                    completeness=DiscoveryCompleteness.FULL,
                    item_count=1,
                )
            },
            scope_state_updates={},
            errors=(),
            computed_at=now,
        )
    )

    state = gather._build_m365_discovery_state(
        program_id="acme",
        programs_root=programs_root,
        program=program,
        workstreams=workstreams,
        items=items,
        workiq_signals=(),
        gather_flags={"workiq": True},
        registry_artifacts=registry_artifacts,
        feedback_events=feedback_events,
        as_of=datetime(2026, 6, 3, 9, 0, tzinfo=timezone.utc),
        previous_entry=None,
    )

    adaptive = state["adaptive_learning"]["workstreams"]["acme"]
    assert any("northwind launch" in keyword.lower() for keyword in adaptive["effective_keywords"])
    assert adaptive["exploration_terms"]
    assert adaptive["active_source_count"] == 1
    assert adaptive["yield_total_last_3"] == 3
    assert adaptive["top_sources"][0]["ref_id"] == "mail-thread-1"


def test_gather_program_promotes_high_confidence_non_confirmed_candidates(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find current status",
            },
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(
                workiq_keywords=("SCHIE",),
            ),
        ),
    )
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(
            M365RegistryArtifact(
                artifact_id="thread:auto:steady001",
                artifact_type="email_thread",
                display_name="Steady High Confidence Thread",
                thread_id="steady-thread-1",
                inferred_workstream="acme",
                confidence=0.91,
                confidence_source="keyword_router",
                pm_confirmed=False,
                promoted_to_workstreams_yaml=False,
                first_seen=date(2026, 5, 8),
                last_seen=date(2026, 5, 9),
                signal_yield_last_3=(1, 2, 1),
                high_confidence_streak=2,
                topics=("SCHIE",),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc),
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Adventure on Northwind. Keywords: SCHIE.": {"results": []},
        },
        tool_payloads={
            "search_emails": {"emails": []},
        },
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    artifacts = gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: bridge,
    )

    gather_state = load_gather_state("acme", programs_root=programs_root)

    assert artifacts.promotion_candidates == (
        gather.M365PromotionCandidate(
            artifact_id="thread:auto:steady001",
            display_name="Steady High Confidence Thread",
            workstream_id="acme",
            confidence=0.86,
            signal_yield_last_3=(2, 1, 0),
        ),
    )
    assert gather_state is not None
    assert gather_state.m365_discovery["promotion_candidate_count"] == 1
    assert gather_state.m365_discovery["promotion_candidate_ids"] == ["thread:auto:steady001"]
    assert gather_state.m365_discovery["promotion_blocked_recent_rejection_count"] == 0
    assert gather_state.m365_discovery["promotion_blocked_missing_id_count"] == 0
    assert gather_state.m365_discovery["promotion_blocked_signal_yield_count"] == 0


def test_gather_program_executes_thread_targeted_email_plans_for_authored_email_threads(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find current status",
            },
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(
                workiq_keywords=("SCHIE",),
                email_threads=(EmailThreadSource(display_name="SCHIE Mail Thread", thread_id="thread-123"),),
            ),
        ),
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Adventure on Northwind. Keywords: SCHIE.": {"results": []},
        },
        tool_payloads={
            "search_emails": {
                "emails": [
                    {
                        "messageId": "mail-miss",
                        "threadId": "thread-other",
                        "subject": "SCHIE unrelated",
                        "snippet": "Different thread should be filtered out.",
                        "receivedDateTime": "2026-05-10T09:00:00Z",
                    },
                    {
                        "messageId": "mail-hit",
                        "threadId": "thread-123",
                        "subject": "SCHIE Mail Thread",
                        "snippet": "Northwind ramp review remains blocked on SCHIE.",
                        "receivedDateTime": "2026-05-10T09:05:00Z",
                    },
                ]
            },
        },
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    artifacts = gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: bridge,
    )

    signals = read_signals("acme", programs_root=programs_root)

    assert artifacts.new_signals == 1
    assert [signal.raw_ref for signal in signals] == ["workiq:email:mail-hit"]
    assert bridge.tool_calls[0] == (
        "workiq",
        "search_emails",
        {"query": '"SCHIE Mail Thread" OR thread-123 OR SCHIE', "limit": 50},
    )
    # The signal-content question still fires unchanged; discovery additionally issues
    # content-relational source questions (ops-ready.md S1 recall fix). Assert the signal
    # question is preserved and a discovery question ran, without pinning the full sequence.
    assert "Find current status. Focus on Adventure on Northwind. Keywords: SCHIE." in bridge.questions
    assert any(
        question.startswith(
            ("Use my Microsoft 365 calendar", "Use my Microsoft Teams messages", "Use my Microsoft 365 mailbox")
        )
        for question in bridge.questions
    )


def test_gather_program_executes_thread_targeted_teams_plans_for_authored_chats(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find current status",
            },
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(
                workiq_keywords=("SCHIE",),
                teams_chats=(TeamsChat(display_name="SCHIE Chat", thread_id="thread-chat-123"),),
            ),
        ),
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Adventure on Northwind. Keywords: SCHIE.": {"results": []},
            "Use my Microsoft Teams messages in any channel or chat to answer.": {
                "messages": [
                    {
                        "messageId": "chat-miss",
                        "threadId": "thread-other",
                        "subject": "SCHIE unrelated",
                        "snippet": "Different thread should be filtered out.",
                        "receivedDateTime": "2026-05-10T09:00:00Z",
                    },
                    {
                        "messageId": "chat-hit",
                        "threadId": "thread-chat-123",
                        "subject": "SCHIE Chat",
                        "snippet": "Northwind ramp review remains blocked on SCHIE.",
                        "receivedDateTime": "2026-05-10T09:05:00Z",
                    },
                ]
            },
        },
        tool_payloads={
            "search_emails": {"emails": []},
        },
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    artifacts = gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: bridge,
    )

    signals = read_signals("acme", programs_root=programs_root)

    assert artifacts.new_signals == 1
    assert [signal.raw_ref for signal in signals] == ["workiq:teams:chat-hit"]
    assert any(
        question.startswith("Use my Microsoft Teams messages in any channel or chat to answer.")
        and '"SCHIE Chat" OR thread-chat-123 OR SCHIE' in question
        for question in bridge.questions
    )
    # The signal-content question still fires unchanged; discovery additionally issues
    # content-relational source questions (ops-ready.md S1 recall fix). Assert the signal
    # question is preserved and a discovery question ran, without pinning the full sequence.
    assert "Find current status. Focus on Adventure on Northwind. Keywords: SCHIE." in bridge.questions
    assert any(
        question.startswith(
            ("Use my Microsoft 365 calendar", "Use my Microsoft Teams messages", "Use my Microsoft 365 mailbox")
        )
        for question in bridge.questions
    )


def test_gather_program_executes_targeted_calendar_plans_for_authored_meeting_series(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find current status",
            },
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(
                workiq_keywords=("SCHIE",),
                teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Ops Review", series_id="meeting-123"),),
            ),
        ),
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Adventure on Northwind. Keywords: SCHIE, acme.": {"results": []},
            "Use my Microsoft 365 calendar and meetings to answer.": {
                "events": [
                    {
                        "meetingId": "meeting-other",
                        "subject": "Other review",
                        "webUrl": "https://teams.microsoft.com/l/meeting/details?eventId=meeting-other",
                        "startDateTime": "2026-05-10T09:00:00Z",
                    },
                    {
                        "meetingId": "meeting-123",
                        "subject": "Acme Weekly Ops Review",
                        "webUrl": "https://teams.microsoft.com/l/meeting/details?eventId=meeting-123",
                        "startDateTime": "2026-05-10T09:05:00Z",
                    },
                ]
            },
        },
        tool_payloads={
            "get_transcript": {
                "meetingId": "meeting-123",
                "title": "Acme Weekly Ops Review transcript",
                "captured_at": "2026-05-10T09:06:00Z",
                "content": "Northwind ramp review remains blocked on SCHIE.",
                "webUrl": "https://teams.microsoft.com/l/meeting/details?eventId=meeting-123",
            },
            "search_emails": {"emails": []},
        },
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    artifacts = gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: bridge,
    )

    signals = read_signals("acme", programs_root=programs_root)

    assert artifacts.new_signals == 1
    assert [signal.raw_ref for signal in signals] == ["workiq:transcript:meeting-123"]
    assert any(
        question.startswith("Use my Microsoft 365 calendar and meetings to answer.")
        and '"Acme Weekly Ops Review" OR meeting-123 OR SCHIE' in question
        for question in bridge.questions
    )
    assert bridge.tool_calls[0] == ("workiq", "get_transcript", {"meeting_id": "meeting-123"})
    # The signal-content question still fires unchanged; discovery additionally issues
    # content-relational source questions (ops-ready.md S1 recall fix). Assert the signal
    # question is preserved and a discovery question ran, without pinning the full sequence.
    # "acme" is added to keywords because the bootstrap registry artifact for "Acme Weekly Ops Review"
    # has display_name that yields "acme" (4 chars) as a learned keyword (min 4 chars required).
    assert "Find current status. Focus on Adventure on Northwind. Keywords: SCHIE, acme." in bridge.questions
    assert any(
        question.startswith(
            ("Use my Microsoft 365 calendar", "Use my Microsoft Teams messages", "Use my Microsoft 365 mailbox")
        )
        for question in bridge.questions
    )


def test_gather_program_executes_targeted_calendar_plans_for_registry_meeting_series(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find current status",
            },
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(workiq_keywords=("SCHIE",)),
        ),
    )
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(
            M365RegistryArtifact(
                artifact_id="meet:acme-weekly-ops-review",
                artifact_type="meeting_series",
                inferred_workstream="acme",
                confidence=0.93,
                confidence_source="router",
                pm_confirmed=False,
                promoted_to_workstreams_yaml=False,
                first_seen=date(2026, 5, 1),
                last_seen=date(2026, 5, 10),
                high_confidence_streak=3,
                display_name="Acme Weekly Ops Review",
                series_id="meeting-123",
                routing_reasoning="Previously routed to Acme.",
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Adventure on Northwind. Keywords: Acme Weekly Ops Review, SCHIE.": {"results": []},
            "Use my Microsoft 365 calendar and meetings to answer.": {
                "events": [
                    {
                        "meetingId": "meeting-other",
                        "subject": "Other review",
                        "webUrl": "https://teams.microsoft.com/l/meeting/details?eventId=meeting-other",
                        "startDateTime": "2026-05-10T09:00:00Z",
                    },
                    {
                        "meetingId": "meeting-123",
                        "subject": "Acme Weekly Ops Review",
                        "webUrl": "https://teams.microsoft.com/l/meeting/details?eventId=meeting-123",
                        "startDateTime": "2026-05-10T09:05:00Z",
                    },
                ]
            },
        },
        tool_payloads={
            "get_transcript": {
                "meetingId": "meeting-123",
                "title": "Acme Weekly Ops Review transcript",
                "captured_at": "2026-05-10T09:06:00Z",
                "content": "Northwind ramp review remains blocked on SCHIE.",
                "webUrl": "https://teams.microsoft.com/l/meeting/details?eventId=meeting-123",
            },
            "search_emails": {"emails": []},
        },
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    artifacts = gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: bridge,
    )

    signals = read_signals("acme", programs_root=programs_root)

    assert artifacts.new_signals == 1
    assert [signal.raw_ref for signal in signals] == ["workiq:transcript:meeting-123"]
    assert any(
        question.startswith("Use my Microsoft 365 calendar and meetings to answer.")
        and '"Acme Weekly Ops Review" OR meeting-123 OR SCHIE' in question
        for question in bridge.questions
    )
    assert ("workiq", "get_transcript", {"meeting_id": "meeting-123"}) in bridge.tool_calls
    # The signal-content question still fires unchanged; discovery additionally issues
    # content-relational source questions (ops-ready.md S1 recall fix). Assert the signal
    # question is preserved and a discovery question ran, without pinning the full sequence.
    assert "Find current status. Focus on Adventure on Northwind. Keywords: SCHIE." in bridge.questions
    assert any(
        question.startswith(
            ("Use my Microsoft 365 calendar", "Use my Microsoft Teams messages", "Use my Microsoft 365 mailbox")
        )
        for question in bridge.questions
    )


def test_build_workiq_signals_resolves_workstream_from_linked_ado_items() -> None:
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find feedback from Rushi on Acme newsletter drafts",
            },
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    items = (
        WorkItem(
            id=1234,
            type="Feature",
            title="Ramp checkpoint",
            state="Active",
            assigned_to="Rushi",
            assigned_to_email="rushi@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="One\\FY26\\Q4",
            target_date=date(2026, 5, 17),
            risk_level=RiskLevel.MEDIUM,
            tags=["acme"],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        ),
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find feedback from Rushi on Acme newsletter drafts": {
                "results": [
                    {
                        "messageId": "thread-1",
                        "title": "Rushi feedback on WI:1234",
                        "sender": "rushi@example.com",
                        "snippet": "Please update WI:1234 before the next draft.",
                        "timestamp": "2026-05-10T09:00:00Z",
                    }
                ]
            },
        }
    )

    signals = gather._build_workiq_signals(
        program=program,
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        items=items,
        workstreams=workstreams,
        bridge=lambda: bridge,
    )

    assert len(signals) == 1
    assert signals[0].workstream_id == "acme"
    assert signals[0].entity_refs == ("WI:1234", "WS:acme")


def test_build_workiq_signals_uses_workstream_signal_source_keywords(monkeypatch) -> None:
    del monkeypatch
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme", "One\\Adventure\\Contoso"),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find review feedback from Acme stakeholders",
                "teams_search": "Search Teams conversations for Acme status",
            },
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(
                workiq_keywords=("Adventure Northwind", "SCHIE gaps"),
                workiq_exclude_keywords=("Direct Drive Northwind",),
            ),
        ),
        Workstream(
            id="dd_on_pf",
            name="Direct Drive on Northwind",
            area_paths=("One\\Adventure\\Contoso",),
            signal_sources=WorkstreamSignalSources(
                workiq_keywords=("Direct Drive Northwind", "DD performance"),
                workiq_exclude_keywords=("Adventure Northwind",),
            ),
        ),
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find review feedback from Acme stakeholders. Focus on Adventure on Northwind. Keywords: Adventure Northwind, SCHIE gaps. Exclude: Direct Drive Northwind.": {
                "results": [
                    {
                        "messageId": "mail-acme",
                        "title": "Adventure ramp update",
                        "snippet": "Latest Adventure Northwind checkpoint is green.",
                        "timestamp": "2026-05-10T09:00:00Z",
                    }
                ]
            },
            "Search Teams conversations for Acme status. Focus on Adventure on Northwind. Keywords: Adventure Northwind, SCHIE gaps. Exclude: Direct Drive Northwind.": None,
            "Find review feedback from Acme stakeholders. Focus on Direct Drive on Northwind. Keywords: Direct Drive Northwind, DD performance. Exclude: Adventure Northwind.": {
                "results": [
                    {
                        "messageId": "mail-dd",
                        "title": "DD pilot note",
                        "snippet": "Direct Drive Northwind remains on track for the next perf gate.",
                        "timestamp": "2026-05-10T10:00:00Z",
                    }
                ]
            },
            "Search Teams conversations for Acme status. Focus on Direct Drive on Northwind. Keywords: Direct Drive Northwind, DD performance. Exclude: Adventure Northwind.": None,
        }
    )

    signals = gather._build_workiq_signals(
        program=program,
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        items=(),
        workstreams=workstreams,
        bridge=lambda: bridge,
    )

    assert bridge.questions == [
        "Find review feedback from Acme stakeholders. Focus on Adventure on Northwind. Keywords: Adventure Northwind, SCHIE gaps. Exclude: Direct Drive Northwind.",
        "Search Teams conversations for Acme status. Focus on Adventure on Northwind. Keywords: Adventure Northwind, SCHIE gaps. Exclude: Direct Drive Northwind.",
        "Find review feedback from Acme stakeholders. Focus on Direct Drive on Northwind. Keywords: Direct Drive Northwind, DD performance. Exclude: Adventure Northwind.",
        "Search Teams conversations for Acme status. Focus on Direct Drive on Northwind. Keywords: Direct Drive Northwind, DD performance. Exclude: Adventure Northwind.",
    ]
    assert {signal.workstream_id for signal in signals} == {"acme", "dd_on_pf"}
    assert {signal.source for signal in signals} == {"workiq/email"}


def test_build_workiq_signals_uses_confirmed_registry_artifacts_when_keywords_missing(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={"feedback_search": "Find current status"},
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(),
        ),
    )
    ensure_m365_registry_bootstrap(
        "acme",
        workstreams=(
            Workstream(
                id="acme",
                name="Adventure on Northwind",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(
                        TeamsMeetingSeries(display_name="Acme Weekly Ops Review", series_id="meeting-1"),
                    ),
                    workiq_keywords=("SCHIE gaps", "Ramp review"),
                ),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Adventure on Northwind. Keywords: Acme Weekly Ops Review, SCHIE gaps, Ramp review.": {
                "results": [
                    {
                        "messageId": "mail-registry",
                        "title": "Registry-backed status",
                        "snippet": "Latest SCHIE gaps review is green.",
                        "timestamp": "2026-05-10T09:00:00Z",
                    }
                ]
            },
        }
    )

    signals = gather._build_workiq_signals(
        program=program,
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        items=(),
        workstreams=workstreams,
        bridge=lambda: bridge,
        programs_root=programs_root,
    )

    # Signal-content question preserved; discovery also issues content-relational source
    # questions (ops-ready.md S1 recall fix) — asserted by shape, not full sequence.
    assert (
        "Find current status. Focus on Adventure on Northwind. Keywords: Acme Weekly Ops Review, SCHIE gaps, Ramp review."
        in bridge.questions
    )
    assert any(question.startswith("Use my Microsoft 365 calendar") for question in bridge.questions)
    assert len(signals) == 1
    assert signals[0].workstream_id == "acme"


def test_build_workiq_signals_runs_targeted_registry_email_thread_plans(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={"feedback_search": "Find current status"},
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(),
        ),
    )
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(
            M365RegistryArtifact(
                artifact_id="thread:named:acme-ramp-thread",
                artifact_type="email_thread",
                inferred_workstream="acme",
                confidence=0.92,
                confidence_source="router",
                pm_confirmed=False,
                promoted_to_workstreams_yaml=False,
                first_seen=date(2026, 5, 1),
                last_seen=date(2026, 5, 10),
                high_confidence_streak=3,
                display_name="Ramp Thread",
                thread_id="thread-1",
                topics=("SCHIE gaps",),
                routing_reasoning="Previously routed to Acme.",
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Adventure on Northwind. Keywords: Ramp Thread, SCHIE gaps.": {"results": []},
        },
        tool_payloads={
            "search_emails": {
                "emails": [
                    {
                        "messageId": "mail-registry-targeted",
                        "threadId": "thread-1",
                        "subject": "Ramp Thread",
                        "snippet": "SCHIE gaps remain blocked on WI:1234.",
                        "receivedDateTime": "2026-05-10T09:00:00Z",
                    }
                ]
            }
        },
    )

    signals = gather._build_workiq_signals(
        program=program,
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        items=(),
        workstreams=workstreams,
        bridge=lambda: bridge,
        programs_root=programs_root,
    )

    assert any(
        call[0] == "workiq"
        and call[1] == "search_emails"
        and call[2].get("limit") == 50
        and str(call[2].get("query", "")).startswith('"Ramp Thread" OR thread-1')
        for call in bridge.tool_calls
    )
    # Signal-content question preserved; discovery also issues a content-relational
    # mailbox question for the targeted thread (the tool_call is asserted above).
    assert (
        "Find current status. Focus on Adventure on Northwind. Keywords: Ramp Thread, SCHIE gaps."
        in bridge.questions
    )
    assert any(question.startswith("Use my Microsoft 365 mailbox") for question in bridge.questions)
    assert len(signals) == 1
    assert signals[0].thread_id == "thread-1"
    assert signals[0].workstream_id == "acme"
    assert signals[0].entity_refs == ("WI:1234", "WS:acme")


def test_build_workiq_signals_auto_resolves_unique_high_confidence_seeded_candidates(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={"feedback_search": "Find current status"},
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            signal_sources=WorkstreamSignalSources(
                teams_chats=(TeamsChat(display_name="Acme Eng Core Chat", thread_id=None),),
            ),
        ),
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Adventure on Northwind. Keywords: Acme Eng Core Chat.": {"results": []},
            "Use my Microsoft Teams messages in any channel or chat to answer.": {
                "messages": [
                    {
                        "threadId": "19:thread-id@thread.v2",
                        "title": "Acme Eng Core Chat",
                        "channel": "Acme Eng Core Chat",
                        "snippet": "Status remains blocked on WI:1234.",
                        "messageId": "msg-1",
                        "timestamp": "2026-05-10T09:00:00Z",
                    }
                ]
            },
        },
        tool_payloads={
            "search_emails": {"emails": []},
        },
    )

    signals = gather._build_workiq_signals(
        program=program,
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        items=(),
        workstreams=workstreams,
        bridge=lambda: bridge,
        programs_root=programs_root,
    )

    store = SourceCandidateStore(programs_root / "acme" / "runtime" / "channel_registry.sqlite3", "acme", ensure_schema=False)
    intent = store.list_intents(workstream_id="acme", ref_kind=SourceRefKind.TEAMS_CHAT)[0]
    candidates = store.list_candidates_for_intent(intent.intent_id)
    attempts = store.get_attempts(intent.intent_id, exclude_expired=False)
    registrations = gather.ChannelRegistryStore(programs_root / "acme" / "runtime" / "channel_registry.sqlite3", "acme", ensure_schema=False)

    assert signals == ()
    assert len(candidates) == 1
    assert candidates[0].ref_id == "19:thread-id@thread.v2"
    assert candidates[0].status == SourceCandidateStatus.ACCEPTED
    assert len(attempts) == 1
    assert attempts[0].outcome == DiscoveryAttemptOutcome.CANDIDATES_FOUND
    assert any(
        question.startswith("Use my Microsoft Teams messages in any channel or chat to answer.")
        and "Acme Eng Core Chat" in question
        for question in bridge.questions
    )
    assert len(registrations.active_registrations("teams")) == 1
    decision_log_path = programs_root / "acme" / "source_intent_decisions.jsonl"
    assert decision_log_path.exists()
    decision_events = [json.loads(line) for line in decision_log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert decision_events[-1]["action"] == "candidate_auto_accept_resolved_intent"
    assert decision_events[-1]["pm_alias"] == "vertex.gather"
    assert decision_events[-1]["candidate_id"] == candidates[0].candidate_id
    assert decision_events[-1]["ref_id"] == "19:thread-id@thread.v2"


def test_gather_program_auto_resolves_seeded_meeting_series_from_same_cycle_calendar_discovery(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            signal_sources=WorkstreamSignalSources(
                teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Ops Review", series_id=None),),
            ),
        ),
    )

    class _DynamicCalendarBridge:
        def __init__(self) -> None:
            self.questions: list[str] = []
            self.tool_calls: list[tuple[str, str, dict[str, object]]] = []
            self._last_mcp_error: str | None = None

        def probe(self) -> AgencyCapabilities:
            return AgencyCapabilities(available=True, has_workiq=True, has_workiq_cli=True, tier="msft")

        def ask_workiq(self, question: str, **_: object) -> dict[str, object] | None:
            self.questions.append(question)
            if question.startswith("Use my Microsoft 365 calendar and meetings to answer.") and any(
                token in question
                for token in ("Adventure on Northwind", '"Acme Weekly Ops Review" OR', "Acme Weekly Ops Review", "acme ops")
            ):
                return {
                    "events": [
                        {
                            "id": "event-1",
                            "meetingId": "meeting-occurrence-1",
                            "seriesMasterId": "series-123",
                            "subject": "Acme Weekly Ops Review",
                            "organizer": {"emailAddress": {"address": "owner@example.com"}},
                            "attendees": [{"emailAddress": {"address": "pm@example.com"}}],
                            "startDateTime": "2026-05-10T09:00:00Z",
                        }
                    ]
                }
            return None

        def invoke_mcp_tool(
            self,
            server: str,
            tool: str,
            args: dict[str, object],
            timeout_seconds: int | None = None,
        ) -> dict[str, object] | None:
            del server, timeout_seconds
            self.tool_calls.append(("workiq", tool, args))
            self._last_mcp_error = None
            if tool == "search_emails":
                return {"emails": []}
            return None

        def last_mcp_error(self) -> str | None:
            return self._last_mcp_error

    bridge = _DynamicCalendarBridge()

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    artifacts = gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: bridge,
    )

    store = SourceCandidateStore(programs_root / "acme" / "runtime" / "channel_registry.sqlite3", "acme", ensure_schema=False)
    intent = store.list_intents(workstream_id="acme", ref_kind=SourceRefKind.MEETING_SERIES)[0]
    candidates = store.list_candidates_for_intent(intent.intent_id)
    registrations = gather.ChannelRegistryStore(programs_root / "acme" / "runtime" / "channel_registry.sqlite3", "acme", ensure_schema=False)
    registry = load_m365_registry("acme", programs_root)

    assert artifacts.new_signals == 0
    assert len(candidates) == 1
    assert candidates[0].ref_id == "series-123"
    assert candidates[0].status == SourceCandidateStatus.ACCEPTED
    assert len(registrations.active_registrations("teams")) == 1
    assert registrations.active_registrations("teams")[0].ref_id == "series-123"
    assert any(artifact.artifact_type == "meeting_series" and artifact.series_id == "series-123" for artifact in registry.artifacts)
    calendar_questions = [
        question
        for question in bridge.questions
        if question.startswith("Use my Microsoft 365 calendar and meetings to answer.")
    ]
    assert any("Acme Weekly Ops Review" in question for question in calendar_questions)
    assert len(calendar_questions) >= 2
    assert any("Adventure on Northwind" in question for question in calendar_questions)


def test_build_workiq_signals_suppresses_recently_rejected_seeded_candidates(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={"feedback_search": "Find current status"},
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            signal_sources=WorkstreamSignalSources(
                teams_chats=(TeamsChat(display_name="Acme Eng Core Chat", thread_id=None),),
            ),
        ),
    )
    store = SourceCandidateStore(programs_root / "acme" / "runtime" / "channel_registry.sqlite3", "acme")
    rejected_at = datetime(2026, 5, 9, 8, 0, tzinfo=timezone.utc)
    store.upsert_candidate(
        SourceCandidate(
            candidate_id=build_source_candidate_id(
                program_id="acme",
                channel="teams",
                provider_instance_id="default",
                ref_kind=SourceRefKind.TEAMS_CHAT,
                ref_id="19:thread-id@thread.v2",
            ),
            program_id="acme",
            channel="teams",
            provider_instance_id="default",
            ref_id="19:thread-id@thread.v2",
            ref_kind=SourceRefKind.TEAMS_CHAT,
            display_name="Acme Eng Core Chat",
            confidence=0.93,
            source_provider="seeded_resolution",
            status=SourceCandidateStatus.REJECTED,
            evidence_json=candidate_evidence_json({"matched_terms": ["Acme Eng Core Chat"]}),
            first_discovered_at=rejected_at,
            last_seen_at=rejected_at,
            decided_at=rejected_at,
            decided_by="pm@test",
            decision_reason="wrong chat",
            old_status=SourceCandidateStatus.PENDING.value,
            decision_version=1,
        ),
        pii_prescrubbed=True,
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Adventure on Northwind. Keywords: Acme Eng Core Chat.": {"results": []},
            "Use my Microsoft Teams messages in any channel or chat to answer.": {
                "messages": [
                    {
                        "threadId": "19:thread-id@thread.v2",
                        "title": "Acme Eng Core Chat",
                        "channel": "Acme Eng Core Chat",
                        "snippet": "Status remains blocked on WI:1234.",
                        "messageId": "msg-1",
                        "timestamp": "2026-05-10T09:00:00Z",
                    }
                ]
            },
        },
        tool_payloads={
            "search_emails": {"emails": []},
        },
    )

    signals = gather._build_workiq_signals(
        program=program,
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        items=(),
        workstreams=workstreams,
        bridge=lambda: bridge,
        programs_root=programs_root,
    )

    intent = store.list_intents(workstream_id="acme", ref_kind=SourceRefKind.TEAMS_CHAT)[0]
    attempts = store.get_attempts(intent.intent_id, exclude_expired=False)
    assert signals == ()
    assert store.list_candidates_for_intent(intent.intent_id) == ()
    suppressed_candidate = store.get_candidate_by_ref(ref_id="19:thread-id@thread.v2", ref_kind=SourceRefKind.TEAMS_CHAT)
    assert suppressed_candidate is not None
    assert suppressed_candidate.status == SourceCandidateStatus.REJECTED
    assert len(attempts) == 1
    assert attempts[0].outcome == DiscoveryAttemptOutcome.REJECTED_CANDIDATE_SUPPRESSED
    assert attempts[0].result_count == 1
    assert attempts[0].reason is not None
    assert "60-day rejection window" in attempts[0].reason


def test_build_workiq_signals_persists_fresh_seeded_candidate_when_one_match_was_recently_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={"feedback_search": "Find current status"},
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            signal_sources=WorkstreamSignalSources(
                teams_chats=(TeamsChat(display_name="Acme Eng Core Chat", thread_id=None),),
            ),
        ),
    )
    store = SourceCandidateStore(programs_root / "acme" / "runtime" / "channel_registry.sqlite3", "acme")
    rejected_at = datetime(2026, 5, 9, 8, 0, tzinfo=timezone.utc)
    store.upsert_candidate(
        SourceCandidate(
            candidate_id=build_source_candidate_id(
                program_id="acme",
                channel="teams",
                provider_instance_id="default",
                ref_kind=SourceRefKind.TEAMS_CHAT,
                ref_id="19:thread-id@thread.v2",
            ),
            program_id="acme",
            channel="teams",
            provider_instance_id="default",
            ref_id="19:thread-id@thread.v2",
            ref_kind=SourceRefKind.TEAMS_CHAT,
            display_name="Acme Eng Core Chat",
            confidence=0.93,
            source_provider="seeded_resolution",
            status=SourceCandidateStatus.REJECTED,
            evidence_json=candidate_evidence_json({"matched_terms": ["Acme Eng Core Chat"]}),
            first_discovered_at=rejected_at,
            last_seen_at=rejected_at,
            decided_at=rejected_at,
            decided_by="pm@test",
            decision_reason="wrong chat",
            old_status=SourceCandidateStatus.PENDING.value,
            decision_version=1,
        ),
        pii_prescrubbed=True,
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Adventure on Northwind. Keywords: Acme Eng Core Chat.": {"results": []},
            "Use my Microsoft Teams messages in any channel or chat to answer.": {
                "messages": [
                    {
                        "threadId": "19:thread-id@thread.v2",
                        "title": "Acme Eng Core Chat",
                        "channel": "Acme Eng Core Chat",
                        "snippet": "Suppressed candidate.",
                        "messageId": "msg-1",
                        "timestamp": "2026-05-10T09:00:00Z",
                    },
                    {
                        "threadId": "19:fresh-thread@thread.v2",
                        "title": "Acme Eng Core Chat",
                        "channel": "Acme Eng Core Chat",
                        "snippet": "Fresh candidate.",
                        "messageId": "msg-2",
                        "timestamp": "2026-05-10T10:00:00Z",
                    },
                ]
            },
        },
        tool_payloads={
            "search_emails": {"emails": []},
        },
    )

    signals = gather._build_workiq_signals(
        program=program,
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        items=(),
        workstreams=workstreams,
        bridge=lambda: bridge,
        programs_root=programs_root,
    )

    intent = store.list_intents(workstream_id="acme", ref_kind=SourceRefKind.TEAMS_CHAT)[0]
    candidates = store.list_candidates_for_intent(intent.intent_id)
    attempts = store.get_attempts(intent.intent_id, exclude_expired=False)
    assert signals == ()
    assert len(candidates) == 1
    assert candidates[0].ref_id == "19:fresh-thread@thread.v2"
    assert candidates[0].status == SourceCandidateStatus.ACCEPTED
    assert len(attempts) == 1
    assert attempts[0].outcome == DiscoveryAttemptOutcome.CANDIDATES_FOUND
    assert attempts[0].result_count == 2


def test_attempt_seeded_source_auto_resolution_returns_stale_plan_when_intent_version_changes(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    as_of = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstream = Workstream(
        id="acme",
        name="Adventure on Northwind",
        signal_sources=WorkstreamSignalSources(
            teams_chats=(TeamsChat(display_name="Acme Eng Core Chat", thread_id=None),),
        ),
    )
    store = SourceCandidateStore(programs_root / "acme" / "channel_registry.sqlite3", "acme")
    store.bootstrap_intents(workstreams=(workstream,), registry_artifacts=(), as_of=as_of)
    intent = store.list_intents(workstream_id="acme", ref_kind=SourceRefKind.TEAMS_CHAT)[0]
    candidate = SourceCandidate(
        candidate_id=build_source_candidate_id(
            program_id="acme",
            channel="teams",
            provider_instance_id="default",
            ref_kind=SourceRefKind.TEAMS_CHAT,
            ref_id="19:thread-id@thread.v2",
        ),
        program_id="acme",
        channel="teams",
        provider_instance_id="default",
        ref_id="19:thread-id@thread.v2",
        ref_kind=SourceRefKind.TEAMS_CHAT,
        display_name="Acme Eng Core Chat",
        confidence=1.0,
        source_provider="seeded_resolution",
        status=SourceCandidateStatus.PENDING,
        evidence_json=candidate_evidence_json({"matched_terms": ["Acme Eng Core Chat"]}),
        first_discovered_at=as_of,
        last_seen_at=as_of,
    )
    store.upsert_candidate(candidate, pii_prescrubbed=True)
    store.link_candidate_to_intent(candidate.candidate_id, intent.intent_id, 1.0)
    store.update_intent_status(intent.intent_id, status=SourceIntentStatus.SEARCHING, updated_by="pm@test")

    stale_plan = gather._attempt_seeded_source_auto_resolution(
        program=program,
        programs_root=programs_root,
        candidate_store=store,
        intent=intent,
        candidate=SimpleNamespace(discovered_id="19:thread-id@thread.v2"),
        as_of=as_of,
    )

    assert stale_plan is True
    persisted = store.get_candidate(candidate.candidate_id)
    assert persisted is not None
    assert persisted.status == SourceCandidateStatus.PENDING


def test_build_workiq_signals_auto_resolves_unique_high_confidence_seeded_email_thread_candidates(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={"feedback_search": "Find current status"},
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            signal_sources=WorkstreamSignalSources(
                email_subject_filters=("SCHIE Mail Thread",),
            ),
        ),
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Adventure on Northwind. Keywords: SCHIE Mail Thread.": {"results": []},
        },
        tool_payloads={
            "search_emails": {
                "emails": [
                    {
                        "messageId": "mail-1",
                        "threadId": "mail-thread-123",
                        "subject": "SCHIE Mail Thread",
                        "bodyPreview": "Status remains blocked on WI:1234.",
                        "receivedDateTime": "2026-05-10T09:00:00Z",
                    }
                ]
            },
        },
    )

    signals = gather._build_workiq_signals(
        program=program,
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        items=(),
        workstreams=workstreams,
        bridge=lambda: bridge,
        programs_root=programs_root,
    )

    store = SourceCandidateStore(programs_root / "acme" / "runtime" / "channel_registry.sqlite3", "acme", ensure_schema=False)
    intent = store.list_intents(workstream_id="acme", ref_kind=SourceRefKind.EMAIL_THREAD)[0]
    candidates = store.list_candidates_for_intent(intent.intent_id)
    attempts = store.get_attempts(intent.intent_id, exclude_expired=False)

    assert len(signals) == 1
    assert signals[0].thread_id == "mail-thread-123"
    assert len(candidates) == 1
    assert candidates[0].ref_id == "mail-thread-123"
    assert candidates[0].status == SourceCandidateStatus.ACCEPTED
    assert len(attempts) == 1
    assert attempts[0].outcome == DiscoveryAttemptOutcome.CANDIDATES_FOUND
    assert any(call[1] == "search_emails" for call in bridge.tool_calls)


def test_build_workiq_signals_suppresses_recently_rejected_seeded_email_thread_candidates(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={"feedback_search": "Find current status"},
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            signal_sources=WorkstreamSignalSources(
                email_subject_filters=("SCHIE Mail Thread",),
            ),
        ),
    )
    store = SourceCandidateStore(programs_root / "acme" / "runtime" / "channel_registry.sqlite3", "acme")
    rejected_at = datetime(2026, 5, 9, 8, 0, tzinfo=timezone.utc)
    store.upsert_candidate(
        SourceCandidate(
            candidate_id=build_source_candidate_id(
                program_id="acme",
                channel="email",
                provider_instance_id="default",
                ref_kind=SourceRefKind.EMAIL_THREAD,
                ref_id="mail-thread-123",
            ),
            program_id="acme",
            channel="email",
            provider_instance_id="default",
            ref_id="mail-thread-123",
            ref_kind=SourceRefKind.EMAIL_THREAD,
            display_name="SCHIE Mail Thread",
            confidence=0.93,
            source_provider="seeded_resolution",
            status=SourceCandidateStatus.REJECTED,
            evidence_json=candidate_evidence_json({"matched_terms": ["SCHIE Mail Thread"]}),
            first_discovered_at=rejected_at,
            last_seen_at=rejected_at,
            decided_at=rejected_at,
            decided_by="pm@test",
            decision_reason="wrong email thread",
            old_status=SourceCandidateStatus.PENDING.value,
            decision_version=1,
        ),
        pii_prescrubbed=True,
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Adventure on Northwind. Keywords: SCHIE Mail Thread.": {"results": []},
        },
        tool_payloads={
            "search_emails": {
                "emails": [
                    {
                        "messageId": "mail-1",
                        "threadId": "mail-thread-123",
                        "subject": "SCHIE Mail Thread",
                        "bodyPreview": "Status remains blocked on WI:1234.",
                        "receivedDateTime": "2026-05-10T09:00:00Z",
                    }
                ]
            },
        },
    )

    signals = gather._build_workiq_signals(
        program=program,
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        items=(),
        workstreams=workstreams,
        bridge=lambda: bridge,
        programs_root=programs_root,
    )

    intent = store.list_intents(workstream_id="acme", ref_kind=SourceRefKind.EMAIL_THREAD)[0]
    attempts = store.get_attempts(intent.intent_id, exclude_expired=False)
    assert len(signals) == 1
    assert signals[0].thread_id == "mail-thread-123"
    assert store.list_candidates_for_intent(intent.intent_id) == ()
    suppressed_candidate = store.get_candidate_by_ref(ref_id="mail-thread-123", ref_kind=SourceRefKind.EMAIL_THREAD)
    assert suppressed_candidate is not None
    assert suppressed_candidate.status == SourceCandidateStatus.REJECTED
    # The email-thread intent is processed by seeded resolution both before and after the
    # broad discovery pass (same-cycle auto-resolution, ops-ready.md S1), so suppression
    # can be recorded once per pass. Every recorded attempt must be a suppression — the
    # rejected candidate is never recreated as a pending candidate (asserted above).
    assert len(attempts) >= 1
    assert all(
        attempt.outcome == DiscoveryAttemptOutcome.REJECTED_CANDIDATE_SUPPRESSED for attempt in attempts
    )
    assert attempts[0].result_count == 1
    assert attempts[0].reason is not None
    assert "60-day rejection window" in attempts[0].reason


def test_build_workiq_signals_keeps_same_message_for_different_workstreams() -> None:
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme", "One\\Adventure\\Contoso"),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={"feedback_search": "Find current status"},
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            signal_sources=WorkstreamSignalSources(workiq_keywords=("shared status",)),
        ),
        Workstream(
            id="dd_on_pf",
            name="Direct Drive on Northwind",
            signal_sources=WorkstreamSignalSources(workiq_keywords=("shared status",)),
        ),
    )
    shared_payload = {
        "results": [
            {
                "messageId": "shared-thread",
                "title": "Cross-program status",
                "snippet": "Shared status update without WI links.",
                "timestamp": "2026-05-10T09:00:00Z",
            }
        ]
    }
    bridge = _FakeWorkIQBridge(
        responses={
            "Find current status. Focus on Adventure on Northwind. Keywords: shared status.": shared_payload,
            "Find current status. Focus on Direct Drive on Northwind. Keywords: shared status.": shared_payload,
        }
    )

    signals = gather._build_workiq_signals(
        program=program,
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        items=(),
        workstreams=workstreams,
        bridge=lambda: bridge,
    )

    assert len(signals) == 2
    assert {signal.workstream_id for signal in signals} == {"acme", "dd_on_pf"}
    assert len({signal.id for signal in signals}) == 2


def test_build_workiq_signals_honors_timeout_and_total_budget(monkeypatch) -> None:
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find current status",
                "teams_search": "Search Teams conversations for current status",
            },
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            signal_sources=WorkstreamSignalSources(workiq_keywords=("shared status",)),
        ),
    )

    class _BudgetAwareBridge:
        def __init__(self) -> None:
            self.questions: list[str] = []
            self.timeouts: list[int | None] = []

        def probe(self) -> AgencyCapabilities:
            return AgencyCapabilities(available=True, has_workiq=True, tier="msft")

        def ask_workiq(self, question: str, *, timeout_seconds: int | None = None) -> dict[str, object] | None:
            self.questions.append(question)
            self.timeouts.append(timeout_seconds)
            return {
                "results": [
                    {
                        "messageId": f"msg-{len(self.questions)}",
                        "title": "Current status",
                        "snippet": "Shared status update without WI links.",
                        "timestamp": "2026-05-10T09:00:00Z",
                    }
                ]
            }

    bridge = _BudgetAwareBridge()
    monotonic_values = iter((0.0, 0.0, 6.0))
    monkeypatch.setattr(gather, "monotonic", lambda: next(monotonic_values))

    signals = gather._build_workiq_signals(
        program=program,
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        items=(),
        workstreams=workstreams,
        bridge=lambda: bridge,
        timeout_seconds=30,
        total_budget_seconds=5,
    )

    assert len(signals) == 1
    assert bridge.timeouts == [5]
    assert bridge.questions == ["Find current status. Focus on Adventure on Northwind. Keywords: shared status."]


def test_load_analytics_signals_summarizes_snapshot_metrics(monkeypatch) -> None:
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="deployment_readiness",
            name="Deployment Readiness",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    as_of = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    created_clients: list[object] = []

    class _FakeAnalyticsADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs
            self.calls: list[tuple[str, object, object]] = []
            created_clients.append(self)

        def query_work_item_snapshot(self, filter_expression: str, select_fields: tuple[str, ...], top=None) -> list[dict[str, object]]:
            self.calls.append(("query_work_item_snapshot", filter_expression, select_fields))
            assert top is None
            assert select_fields == gather._ANALYTICS_SNAPSHOT_FIELDS
            return [
                {
                    "DateSK": 20260509,
                    "WorkItemId": 101,
                    "WorkItemType": "Feature",
                    "Title": "Checkpoint A",
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "CompletedDateSK": None,
                    "CycleTimeDays": None,
                    "LeadTimeDays": None,
                },
                {
                    "DateSK": 20260509,
                    "WorkItemId": 202,
                    "WorkItemType": "Feature",
                    "Title": "Checkpoint B",
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "CompletedDateSK": 20260508,
                    "CycleTimeDays": 4.0,
                    "LeadTimeDays": 7.0,
                },
                {
                    "DateSK": 20260510,
                    "WorkItemId": 101,
                    "WorkItemType": "Feature",
                    "Title": "Checkpoint A",
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "CompletedDateSK": 20260510,
                    "CycleTimeDays": 6.0,
                    "LeadTimeDays": 9.0,
                },
                {
                    "DateSK": 20260510,
                    "WorkItemId": 202,
                    "WorkItemType": "Feature",
                    "Title": "Checkpoint B",
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "CompletedDateSK": 20260508,
                    "CycleTimeDays": 4.0,
                    "LeadTimeDays": 7.0,
                },
            ]

        def execute_wiql(self, wiql: str, top: int | None = None) -> list[int]:
            del wiql
            return []

    monkeypatch.setattr(gather, "ADOClient", _FakeAnalyticsADOClient)
    monkeypatch.setattr(gather, "_load_ado_wiql_queries", lambda *args, **kwargs: ())

    signals, ado_calls = gather._load_analytics_signals(program, workstreams, as_of)

    assert ado_calls == 1
    assert len(signals) == 1
    assert signals[0].source == "ado/analytics"
    assert signals[0].workstream_id == "deployment_readiness"
    assert "2 items in scope" in signals[0].text
    assert "2 completed in window" in signals[0].text
    assert "avg cycle 5.0d" in signals[0].text
    assert "avg lead 8.0d" in signals[0].text
    assert "scope stable vs 2026-05-09" in signals[0].text
    assert "open down 1 vs 2026-05-09" in signals[0].text
    assert "flow: Closed=2" in signals[0].text
    assert signals[0].entity_refs == ("WI:101", "WI:202", "WS:deployment_readiness")
    assert signals[0].metadata is not None
    assert signals[0].metadata["window_start_snapshot_date_sk"] == 20260509
    assert signals[0].metadata["latest_snapshot_date_sk"] == 20260510
    assert signals[0].metadata["window_start_item_count"] == 2
    assert signals[0].metadata["window_start_open_item_count"] == 1
    assert signals[0].metadata["latest_open_item_count"] == 0
    assert signals[0].metadata["scope_delta_count"] == 0
    assert signals[0].metadata["open_delta_count"] == -1
    assert signals[0].metadata["completed_item_count"] == 2
    assert signals[0].metadata["window_start_state_counts"] == {"Active": 1, "Closed": 1}
    assert signals[0].metadata["state_counts"] == {"Closed": 2}
    assert created_clients[0].calls == [
        (
            "query_work_item_snapshot",
            "( startswith(Area/AreaPath, 'One\\Adventure\\Acme') ) and ( WorkItemType eq 'Feature' ) and DateSK ge 20260426 and DateSK le 20260510 and IsLastRevisionOfDay eq true and not ( State eq 'Removed' )",
            gather._ANALYTICS_SNAPSHOT_FIELDS,
        )
    ]


def test_load_analytics_signals_records_open_history_for_burndown(monkeypatch) -> None:
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="deployment_readiness",
            name="Deployment Readiness",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    as_of = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)

    class _FakeAnalyticsADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def query_work_item_snapshot(self, filter_expression: str, select_fields: tuple[str, ...], top=None) -> list[dict[str, object]]:
            del filter_expression, select_fields, top
            return [
                {
                    "DateSK": 20260508,
                    "WorkItemId": 101,
                    "WorkItemType": "Feature",
                    "Title": "Checkpoint A",
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "CompletedDateSK": None,
                    "CycleTimeDays": None,
                    "LeadTimeDays": None,
                },
                {
                    "DateSK": 20260508,
                    "WorkItemId": 202,
                    "WorkItemType": "Feature",
                    "Title": "Checkpoint B",
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "CompletedDateSK": None,
                    "CycleTimeDays": None,
                    "LeadTimeDays": None,
                },
                {
                    "DateSK": 20260508,
                    "WorkItemId": 303,
                    "WorkItemType": "Feature",
                    "Title": "Checkpoint C",
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "CompletedDateSK": 20260508,
                    "CycleTimeDays": 2.0,
                    "LeadTimeDays": 3.0,
                },
                {
                    "DateSK": 20260509,
                    "WorkItemId": 101,
                    "WorkItemType": "Feature",
                    "Title": "Checkpoint A",
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "CompletedDateSK": None,
                    "CycleTimeDays": None,
                    "LeadTimeDays": None,
                },
                {
                    "DateSK": 20260509,
                    "WorkItemId": 202,
                    "WorkItemType": "Feature",
                    "Title": "Checkpoint B",
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "CompletedDateSK": 20260509,
                    "CycleTimeDays": 4.0,
                    "LeadTimeDays": 6.0,
                },
                {
                    "DateSK": 20260509,
                    "WorkItemId": 303,
                    "WorkItemType": "Feature",
                    "Title": "Checkpoint C",
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "CompletedDateSK": 20260508,
                    "CycleTimeDays": 2.0,
                    "LeadTimeDays": 3.0,
                },
                {
                    "DateSK": 20260510,
                    "WorkItemId": 101,
                    "WorkItemType": "Feature",
                    "Title": "Checkpoint A",
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "CompletedDateSK": 20260510,
                    "CycleTimeDays": 5.0,
                    "LeadTimeDays": 8.0,
                },
                {
                    "DateSK": 20260510,
                    "WorkItemId": 202,
                    "WorkItemType": "Feature",
                    "Title": "Checkpoint B",
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "CompletedDateSK": 20260509,
                    "CycleTimeDays": 4.0,
                    "LeadTimeDays": 6.0,
                },
                {
                    "DateSK": 20260510,
                    "WorkItemId": 303,
                    "WorkItemType": "Feature",
                    "Title": "Checkpoint C",
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "CompletedDateSK": 20260508,
                    "CycleTimeDays": 2.0,
                    "LeadTimeDays": 3.0,
                },
            ]

        def execute_wiql(self, wiql: str, top: int | None = None) -> list[int]:
            del wiql
            return []

    monkeypatch.setattr(gather, "ADOClient", _FakeAnalyticsADOClient)
    monkeypatch.setattr(gather, "_load_ado_wiql_queries", lambda *args, **kwargs: ())

    signals, ado_calls = gather._load_analytics_signals(program, workstreams, as_of)

    assert ado_calls == 1
    assert len(signals) == 1
    assert signals[0].source == "ado/analytics"
    assert "burndown 2->1->0 open" in signals[0].text
    assert signals[0].metadata is not None
    assert signals[0].metadata["open_history"] == {
        "2026-05-08": 2,
        "2026-05-09": 1,
        "2026-05-10": 0,
    }


def test_load_analytics_signals_raises_query_error_on_snapshot_failure(monkeypatch) -> None:
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="deployment_readiness",
            name="Deployment Readiness",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    as_of = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    created_clients: list[object] = []

    class _FailingAnalyticsADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs
            self.calls: list[str] = []
            created_clients.append(self)

        def query_work_item_snapshot(self, filter_expression: str, select_fields: tuple[str, ...], top=None) -> list[dict[str, object]]:
            self.calls.append("query_work_item_snapshot")
            del top, filter_expression, select_fields
            raise QueryError("AreaPath unsupported")

        def execute_wiql(self, wiql: str, top: int | None = None) -> list[int]:
            del wiql, top
            return []

    monkeypatch.setattr(gather, "ADOClient", _FailingAnalyticsADOClient)
    monkeypatch.setattr(gather, "_load_ado_wiql_queries", lambda *args, **kwargs: ())

    with pytest.raises(QueryError, match="AreaPath unsupported"):
        gather._load_analytics_signals(program, workstreams, as_of)

    assert created_clients[0].calls == ["query_work_item_snapshot"]


def test_load_analytics_signals_appends_wiql_golden_query_summaries(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    knowledge_dir = program_dir / "knowledge"
    knowledge_dir.mkdir(parents=True)
    (knowledge_dir / "people_directory.yaml").write_text('schema_version: "1.0"\npeople: []\n', encoding="utf-8")
    (knowledge_dir / "teams.yaml").write_text('schema_version: "1.0"\nteams: []\n', encoding="utf-8")
    (knowledge_dir / "products.yaml").write_text('schema_version: "1.0"\nproducts: []\n', encoding="utf-8")
    (knowledge_dir / "golden_queries.yaml").write_text(
        """
schema_version: "1.0"
queries:
  - id: schie-open
    engine: wiql
    wiql: SELECT [System.Id] FROM WorkItems WHERE [System.Tags] CONTAINS 'SCHIE'
    section: SCHIE Open
    render_as: table
    confidence: high
    program_ids: [acme]
    workstream_ids: [acme]
""".strip(),
        encoding="utf-8",
    )

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (Workstream(id="acme", name="Acme", area_paths=("One\\Adventure\\Acme",)),)

    class _FakeADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def query_work_item_snapshot(self, filter_expression: str, select_fields: tuple[str, ...], top=None) -> list[dict[str, object]]:
            del filter_expression, select_fields, top
            return []

        def execute_wiql(self, wiql: str, top: int | None = None) -> list[int]:
            assert "SCHIE" in wiql
            return [1001, 1002]

    monkeypatch.setattr(gather, "ADOClient", _FakeADOClient)

    signals, ado_calls = gather._load_analytics_signals(
        program,
        workstreams,
        datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    wiql_signals = [signal for signal in signals if signal.source == "ado/wiql"]
    assert ado_calls == 2
    assert len(wiql_signals) == 1
    assert wiql_signals[0].workstream_id == "acme"
    assert wiql_signals[0].entity_refs == ("WI:1001", "WI:1002", "WS:acme")
    assert wiql_signals[0].metadata["query_id"] == "schie-open"
    assert wiql_signals[0].metadata["work_item_count"] == 2
    assert "SCHIE Open: 2 item(s) matched WIQL query schie-open" in wiql_signals[0].text


def test_load_wiql_golden_query_signals_resolves_current_iteration_path(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    knowledge_dir = program_dir / "knowledge"
    knowledge_dir.mkdir(parents=True)
    (knowledge_dir / "people_directory.yaml").write_text('schema_version: "1.0"\npeople: []\n', encoding="utf-8")
    (knowledge_dir / "teams.yaml").write_text('schema_version: "1.0"\nteams: []\n', encoding="utf-8")
    (knowledge_dir / "products.yaml").write_text('schema_version: "1.0"\nproducts: []\n', encoding="utf-8")
    (knowledge_dir / "golden_queries.yaml").write_text(
        """
schema_version: "1.0"
queries:
  - id: stg-current-iteration
    engine: wiql
    wiql: SELECT [System.Id] FROM WorkItems WHERE [System.IterationPath] = '{current_iteration_path}'
    section: Acme STG Current Iteration
    render_as: table
    confidence: high
    program_ids: [acme]
    workstream_ids: [acme]
""".strip(),
        encoding="utf-8",
    )

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (Workstream(id="acme", name="Acme", area_paths=("One\\Adventure\\Acme",)),)
    executed_wiql: list[str] = []

    class _FakeADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def list_team_iterations(self, timeframe: str | None = None, team: str | None = None) -> list[dict[str, object]]:
            assert timeframe == "current"
            assert team is None
            return [{"id": "iteration-24", "path": "One\\Sprint 24"}]

        def execute_wiql(self, wiql: str, top: int | None = None) -> list[int]:
            executed_wiql.append(wiql)
            return [1001]

    monkeypatch.setattr(gather, "ADOClient", _FakeADOClient)

    signals, ado_calls = gather._load_wiql_golden_query_signals(
        program,
        workstreams,
        datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    assert ado_calls == 2
    assert executed_wiql == ["SELECT [System.Id] FROM WorkItems WHERE [System.IterationPath] = 'One\\Sprint 24'"]
    assert len(signals) == 1
    assert signals[0].source == "ado/wiql"
    assert signals[0].entity_refs == ("WI:1001", "WS:acme")
    assert signals[0].metadata["query_id"] == "stg-current-iteration"


def test_gather_program_records_frozen_wiql_query_state_history(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    knowledge_dir = program_dir / "knowledge"
    knowledge_dir.mkdir(parents=True)
    (knowledge_dir / "people_directory.yaml").write_text('schema_version: "1.0"\npeople: []\n', encoding="utf-8")
    (knowledge_dir / "teams.yaml").write_text('schema_version: "1.0"\nteams: []\n', encoding="utf-8")
    (knowledge_dir / "products.yaml").write_text('schema_version: "1.0"\nproducts: []\n', encoding="utf-8")
    (knowledge_dir / "golden_queries.yaml").write_text(
        """
schema_version: "1.0"
queries:
  - id: schie-open
    engine: wiql
    wiql: SELECT [System.Id] FROM WorkItems WHERE [System.Tags] CONTAINS 'SCHIE'
    section: SCHIE Open
    render_as: table
    confidence: high
    program_ids: [acme]
    workstream_ids: [acme]
""".strip(),
        encoding="utf-8",
    )

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    current_time = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, (Workstream(id="acme", name="Acme", area_paths=("One\\Adventure\\Acme",)),)))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    class _FakeADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def query_work_item_snapshot(self, filter_expression: str, select_fields: tuple[str, ...], top=None) -> list[dict[str, object]]:
            del filter_expression, select_fields, top
            return []

        def execute_wiql(self, wiql: str, top: int | None = None) -> list[int]:
            assert "SCHIE" in wiql
            return [1001, 1002]

    monkeypatch.setattr(gather, "ADOClient", _FakeADOClient)

    for day in range(4):
        gather.gather_program(
            "acme",
            as_of=current_time + timedelta(days=day),
            programs_root=programs_root,
            loader=lambda program, workstreams, as_of, **_: ((), 0),
            freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
            include_analytics=True,
        )

    gather_state = load_gather_state("acme", programs_root=programs_root)

    assert gather_state is not None
    query_state = gather_state.query_states["schie-open"]
    assert query_state["last_cycle_succeeded"] is True
    assert query_state["row_count"] == 2
    assert query_state["value_last_4"] == [2.0, 2.0, 2.0, 2.0]
    assert query_state["value_frozen_warning"] is True


def test_gather_program_records_frozen_ado_analytics_query_state_history(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="deployment_readiness",
            name="Deployment Readiness",
            area_paths=("One\\Adventure\\Acme",),
        ),
    )
    as_of = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)

    class _FakeAnalyticsADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def query_work_item_snapshot(self, filter_expression: str, select_fields: tuple[str, ...], top=None) -> list[dict[str, object]]:
            del filter_expression, select_fields, top
            return [
                {
                    "DateSK": 20260509,
                    "WorkItemId": 101,
                    "WorkItemType": "Feature",
                    "Title": "Checkpoint A",
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "CompletedDateSK": None,
                    "CycleTimeDays": None,
                    "LeadTimeDays": None,
                },
                {
                    "DateSK": 20260509,
                    "WorkItemId": 202,
                    "WorkItemType": "Feature",
                    "Title": "Checkpoint B",
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "CompletedDateSK": 20260508,
                    "CycleTimeDays": 4.0,
                    "LeadTimeDays": 7.0,
                },
                {
                    "DateSK": 20260510,
                    "WorkItemId": 101,
                    "WorkItemType": "Feature",
                    "Title": "Checkpoint A",
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "CompletedDateSK": 20260510,
                    "CycleTimeDays": 6.0,
                    "LeadTimeDays": 9.0,
                },
                {
                    "DateSK": 20260510,
                    "WorkItemId": 202,
                    "WorkItemType": "Feature",
                    "Title": "Checkpoint B",
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "CompletedDateSK": 20260508,
                    "CycleTimeDays": 4.0,
                    "LeadTimeDays": 7.0,
                },
            ]

        def execute_wiql(self, wiql: str, top: int | None = None) -> list[int]:
            del wiql
            return []

    monkeypatch.setattr(gather, "ADOClient", _FakeAnalyticsADOClient)
    monkeypatch.setattr(gather, "_load_ado_wiql_queries", lambda *args, **kwargs: ())
    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    for _ in range(4):
        gather.gather_program(
            "acme",
            as_of=as_of,
            programs_root=programs_root,
            loader=lambda program, workstreams, as_of, **_: ((), 0),
            freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
            include_analytics=True,
        )

    gather_state = load_gather_state("acme", programs_root=programs_root)

    assert gather_state is not None
    query_state = gather_state.query_states["ado-analytics:deployment_readiness"]
    assert query_state["last_cycle_succeeded"] is True
    assert query_state["row_count"] == 2
    assert query_state["completed_item_count"] == 2
    assert query_state["value_metric"] == "average_cycle_time_days"
    assert query_state["data_freshness_ok"] is True
    assert query_state["value_last_4"] == [5.0, 5.0, 5.0, 5.0]
    assert query_state["value_frozen_warning"] is True


def test_gather_program_records_frozen_sprint_query_state_history(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="deployment_readiness",
            name="Deployment Readiness",
            area_paths=("One\\Adventure\\Acme",),
        ),
    )
    item = WorkItem(
        id=101,
        type="Feature",
        title="Checkpoint A",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="One\\Sprint 24",
        target_date=date(2026, 5, 16),
        risk_level=RiskLevel.MEDIUM,
        tags=[],
        custom_fields={},
        revisions=[],
        comments=[],
        fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_resolve_uil_channel_binding_for_gather", lambda *_a, **_k: object())
    monkeypatch.setattr(gather, "_load_ado_items_via_uil", lambda _p, _ws, _ao, **__: ((item,), (item,), 0))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(gather, "_load_dependency_program_items", lambda program, workstreams, as_of, **_: ((), 0))

    class _FakeSprintADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def list_team_iterations(self, timeframe: str | None = None, team: str | None = None) -> list[dict[str, object]]:
            del team
            assert timeframe == "current"
            return [
                {
                    "id": "iteration-24",
                    "name": "Sprint 24",
                    "path": "One\\Sprint 24",
                    "attributes": {
                        "startDate": "2026-05-08T00:00:00Z",
                        "finishDate": "2026-05-16T00:00:00Z",
                        "timeFrame": "current",
                    },
                }
            ]

        def list_iteration_capacities(self, iteration_id: str, team: str | None = None) -> list[dict[str, object]]:
            del team
            assert iteration_id == "iteration-24"
            return []

        def query_work_item_snapshot(self, filter_expression: str, select_fields: tuple[str, ...], top=None) -> list[dict[str, object]]:
            del filter_expression, select_fields, top
            return [
                {
                    "DateSK": 20260509,
                    "WorkItemId": 101,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260510,
                    "WorkItemId": 101,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
            ]

    monkeypatch.setattr(gather, "ADOClient", _FakeSprintADOClient)

    for day in range(4):
        gather.gather_program(
            "acme",
            as_of=datetime(2026, 5, 10 + day, 8, 0, tzinfo=timezone.utc),
            programs_root=programs_root,
            include_sprints=True,
        )

    gather_state = load_gather_state("acme", programs_root=programs_root)

    assert gather_state is not None
    query_state = gather_state.query_states["ado-sprint:deployment_readiness:iteration-24"]
    assert query_state["last_cycle_succeeded"] is True
    assert query_state["row_count"] == 1
    assert query_state["open_item_count"] == 1
    assert query_state["value_metric"] == "open_item_count"
    assert query_state["iteration_name"] == "Sprint 24"
    assert query_state["data_freshness_ok"] is False
    assert query_state["value_last_4"] == [1.0, 1.0, 1.0, 1.0]
    assert query_state["value_frozen_warning"] is True


def test_gather_program_records_frozen_pull_request_query_state_history(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="deployment_readiness",
            name="Deployment Readiness",
            area_paths=("One\\Adventure\\Acme",),
            ado_repository_ids=("repo-42",),
        ),
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_resolve_uil_channel_binding_for_gather", lambda *_a, **_k: object())
    monkeypatch.setattr(gather, "_load_ado_items_via_uil", lambda _p, _ws, _ao, **__: ((), (), 0))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(gather, "_load_dependency_program_items", lambda program, workstreams, as_of, **_: ((), 0))

    class _FakePullRequestADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def list_pull_requests(self, repository_id: str, *, status: str = "active", top: int = 100) -> list[dict[str, object]]:
            assert repository_id == "repo-42"
            assert status == "active"
            assert top == 100
            return [
                {
                    "pullRequestId": 301,
                    "title": "Stabilize rollout for WI:12345",
                    "status": "active",
                    "creationDate": "2026-05-02T08:00:00Z",
                    "isDraft": False,
                    "repository": {"id": repository_id, "name": "XStoreApp"},
                },
                {
                    "pullRequestId": 302,
                    "title": "Tune validation gates",
                    "status": "active",
                    "creationDate": "2026-05-08T08:00:00Z",
                    "isDraft": True,
                    "repository": {"id": repository_id, "name": "XStoreApp"},
                },
            ]

        def list_pipeline_runs(self, pipeline_id: str, top: int = 10) -> list[dict[str, object]]:
            del pipeline_id, top
            return []

    monkeypatch.setattr(ado_pipeline_stage, "ADOClient", _FakePullRequestADOClient)

    for day in range(4):
        gather.gather_program(
            "acme",
            as_of=datetime(2026, 5, 10 + day, 8, 0, tzinfo=timezone.utc),
            programs_root=programs_root,
            include_pipelines=True,
        )

    gather_state = load_gather_state("acme", programs_root=programs_root)

    assert gather_state is not None
    query_state = gather_state.query_states["ado-pr:deployment_readiness"]
    assert query_state["last_cycle_succeeded"] is True
    assert query_state["row_count"] == 2
    assert query_state["open_pr_count"] == 2
    assert query_state["value_metric"] == "open_pr_count"
    assert query_state["p90_age_days"] == 11.0
    assert query_state["data_freshness_ok"] is True
    assert query_state["value_last_4"] == [2.0, 2.0, 2.0, 2.0]
    assert query_state["value_frozen_warning"] is True


def test_gather_program_records_zero_failure_pipeline_query_state_history(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="deployment_readiness",
            name="Deployment Readiness",
            area_paths=("One\\Adventure\\Acme",),
            ado_pipeline_ids=("42",),
        ),
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_resolve_uil_channel_binding_for_gather", lambda *_a, **_k: object())
    monkeypatch.setattr(gather, "_load_ado_items_via_uil", lambda _p, _ws, _ao, **__: ((), (), 0))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(gather, "_load_dependency_program_items", lambda program, workstreams, as_of, **_: ((), 0))

    class _FakeHealthyPipelineADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def list_pipeline_runs(self, pipeline_id: str, top: int = 10) -> list[dict[str, object]]:
            assert pipeline_id == "42"
            assert top == 10
            return [
                {
                    "id": 105,
                    "name": "Build Validation",
                    "state": "completed",
                    "result": "succeeded",
                    "createdDate": "2026-05-11T10:00:00Z",
                    "finishedDate": "2026-05-11T10:15:00Z",
                },
                {
                    "id": 103,
                    "name": "Build Validation",
                    "state": "completed",
                    "result": "succeeded",
                    "createdDate": "2026-05-09T09:00:00Z",
                    "finishedDate": "2026-05-09T09:10:00Z",
                },
            ]

        def list_pull_requests(self, repository_id: str, *, status: str = "active", top: int = 100) -> list[dict[str, object]]:
            del repository_id, status, top
            return []

    monkeypatch.setattr(ado_pipeline_stage, "ADOClient", _FakeHealthyPipelineADOClient)

    for day in range(4):
        gather.gather_program(
            "acme",
            as_of=datetime(2026, 5, 12 + day, 8, 0, tzinfo=timezone.utc),
            programs_root=programs_root,
            include_pipelines=True,
        )

    gather_state = load_gather_state("acme", programs_root=programs_root)

    assert gather_state is not None
    query_state = gather_state.query_states["ado-pipeline:deployment_readiness"]
    assert query_state["last_cycle_succeeded"] is True
    assert query_state["row_count"] == 2
    assert query_state["failed_run_count"] == 0
    assert query_state["zero_rows_ok"] is True
    assert query_state["value_metric"] == "failed_run_count"
    assert query_state["data_freshness_ok"] is True
    assert query_state["value_last_4"] == [0.0, 0.0, 0.0, 0.0]
    assert query_state["value_frozen_warning"] is False


def test_load_sprint_signals_summarizes_current_iteration_metrics(monkeypatch) -> None:
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="deployment_readiness",
            name="Deployment Readiness",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    items = (
        WorkItem(
            id=101,
            type="Feature",
            title="Checkpoint A",
            state="Active",
            assigned_to="Priya",
            assigned_to_email="priya@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="One\\Sprint 24",
            target_date=date(2026, 5, 16),
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        ),
        WorkItem(
            id=202,
            type="Feature",
            title="Checkpoint B",
            state="Closed",
            assigned_to="Priya",
            assigned_to_email="priya@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="One\\Sprint 24",
            target_date=date(2026, 5, 16),
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        ),
    )
    as_of = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    created_clients: list[object] = []

    class _FakeSprintADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs
            self.calls: list[tuple[str, object]] = []
            created_clients.append(self)

        def list_team_iterations(self, timeframe: str | None = None) -> list[dict[str, object]]:
            self.calls.append(("list_team_iterations", timeframe))
            return [
                {
                    "id": "iteration-24",
                    "name": "Sprint 24",
                    "path": "One\\Sprint 24",
                    "attributes": {
                        "startDate": "2026-05-04T00:00:00Z",
                        "finishDate": "2026-05-15T00:00:00Z",
                        "timeFrame": "current",
                    },
                }
            ]

        def list_iteration_capacities(self, iteration_id: str) -> list[dict[str, object]]:
            self.calls.append(("list_iteration_capacities", iteration_id))
            return [
                {
                    "teamMember": {"displayName": "Priya"},
                    "activities": [
                        {"name": "Development", "capacityPerDay": 4},
                        {"name": "Design", "capacityPerDay": 2},
                    ],
                    "daysOff": [],
                },
                {
                    "teamMember": {"displayName": "Alex"},
                    "activities": [{"name": "Development", "capacityPerDay": 3}],
                    "daysOff": [{"start": "2026-05-12T00:00:00Z", "end": "2026-05-12T00:00:00Z"}],
                },
            ]

        def query_work_item_snapshot(self, filter_expression: str, select_fields: tuple[str, ...], top=None) -> list[dict[str, object]]:
            self.calls.append(("query_work_item_snapshot", filter_expression, select_fields))
            return []

    monkeypatch.setattr(gather, "ADOClient", _FakeSprintADOClient)

    signals, ado_calls = gather._load_sprint_signals(program, workstreams, items, as_of)

    assert ado_calls == 3
    assert len(signals) == 1
    assert signals[0].source == "ado/sprint"
    assert signals[0].workstream_id == "deployment_readiness"
    assert "WS:deployment_readiness" in signals[0].entity_refs
    assert "2 committed" in signals[0].text
    assert "1 completed" in signals[0].text
    assert "1 open" in signals[0].text
    assert "50% complete" in signals[0].text
    assert "pace on track vs 50% elapsed" in signals[0].text
    assert "tracking to finish at 0.2/day (0.2/day needed)" in signals[0].text
    assert "team capacity 9.0h/day across 2 members" in signals[0].text
    assert signals[0].metadata is not None
    assert signals[0].metadata["iteration_name"] == "Sprint 24"
    assert signals[0].metadata["committed_item_count"] == 2
    assert signals[0].metadata["completed_item_count"] == 1
    assert signals[0].metadata["elapsed_business_days"] == 5
    assert signals[0].metadata["total_business_days"] == 10
    assert signals[0].metadata["remaining_business_days"] == 5
    assert signals[0].metadata["expected_completion_pct"] == 50
    assert signals[0].metadata["pace_delta_pct"] == 0
    assert signals[0].metadata["pace_status"] == "on_track"
    assert signals[0].metadata["observed_completion_per_business_day"] == 0.2
    assert signals[0].metadata["required_completion_per_business_day"] == 0.2
    assert signals[0].metadata["projected_completion_pct"] == 100
    assert signals[0].metadata["projection_status"] == "finish"
    assert signals[0].metadata["team_member_count"] == 2
    assert signals[0].metadata["members_with_capacity"] == 2
    assert signals[0].metadata["total_capacity_per_day"] == 9.0
    assert signals[0].metadata["days_off_entry_count"] == 1
    assert created_clients[0].calls == [
        ("list_team_iterations", "current"),
        ("list_iteration_capacities", "iteration-24"),
        (
            "query_work_item_snapshot",
            "( startswith(Area/AreaPath, 'One\\Adventure\\Acme') ) and ( WorkItemType eq 'Feature' ) and DateSK ge 20260426 and DateSK le 20260510 and IsLastRevisionOfDay eq true and not ( State eq 'Removed' )",
            gather._SPRINT_SNAPSHOT_FIELDS,
        ),
    ]


def test_load_sprint_signals_records_current_sprint_burndown_history(monkeypatch) -> None:
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="deployment_readiness",
            name="Deployment Readiness",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    items = (
        WorkItem(
            id=101,
            type="Feature",
            title="Checkpoint A",
            state="Active",
            assigned_to="Priya",
            assigned_to_email="priya@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="One\\Sprint 24",
            target_date=date(2026, 5, 16),
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        ),
        WorkItem(
            id=202,
            type="Feature",
            title="Checkpoint B",
            state="Closed",
            assigned_to="Priya",
            assigned_to_email="priya@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="One\\Sprint 24",
            target_date=date(2026, 5, 16),
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        ),
    )
    as_of = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)

    class _FakeSprintADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def list_team_iterations(self, timeframe: str | None = None) -> list[dict[str, object]]:
            assert timeframe == "current"
            return [
                {
                    "id": "iteration-24",
                    "name": "Sprint 24",
                    "path": "One\\Sprint 24",
                    "attributes": {
                        "startDate": "2026-05-04T00:00:00Z",
                        "finishDate": "2026-05-15T00:00:00Z",
                        "timeFrame": "current",
                    },
                }
            ]

        def list_iteration_capacities(self, iteration_id: str) -> list[dict[str, object]]:
            assert iteration_id == "iteration-24"
            return []

        def query_work_item_snapshot(self, filter_expression: str, select_fields: tuple[str, ...], top=None) -> list[dict[str, object]]:
            del filter_expression, top
            assert "IterationPath" in select_fields
            return [
                {
                    "DateSK": 20260508,
                    "WorkItemId": 101,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260508,
                    "WorkItemId": 202,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260509,
                    "WorkItemId": 101,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260509,
                    "WorkItemId": 202,
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260510,
                    "WorkItemId": 101,
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260510,
                    "WorkItemId": 202,
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
            ]

    monkeypatch.setattr(gather, "ADOClient", _FakeSprintADOClient)

    signals, ado_calls = gather._load_sprint_signals(program, workstreams, items, as_of)

    assert ado_calls == 3
    assert len(signals) == 1
    assert "burndown 2->1->0 open" in signals[0].text
    assert "completion 0->1->2 done" in signals[0].text
    assert signals[0].metadata is not None
    assert signals[0].metadata["open_history"] == {
        "2026-05-08": 2,
        "2026-05-09": 1,
        "2026-05-10": 0,
    }
    assert signals[0].metadata["completed_history"] == {
        "2026-05-08": 0,
        "2026-05-09": 1,
        "2026-05-10": 2,
    }


def test_load_sprint_signals_falls_back_to_item_scoped_snapshot_queries(monkeypatch) -> None:
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="deployment_readiness",
            name="Deployment Readiness",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    items = (
        WorkItem(
            id=101,
            type="Feature",
            title="Checkpoint A",
            state="Active",
            assigned_to="Priya",
            assigned_to_email="priya@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="One\\Sprint 24",
            target_date=date(2026, 5, 16),
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        ),
    )
    as_of = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    created_clients: list[object] = []

    class _FallbackSprintADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs
            self.calls: list[tuple[str, object, object]] = []
            created_clients.append(self)

        def list_team_iterations(self, timeframe: str | None = None) -> list[dict[str, object]]:
            self.calls.append(("list_team_iterations", timeframe, None))
            return [
                {
                    "id": "iteration-24",
                    "name": "Sprint 24",
                    "path": "One\\Sprint 24",
                    "attributes": {
                        "startDate": "2026-05-04T00:00:00Z",
                        "finishDate": "2026-05-15T00:00:00Z",
                        "timeFrame": "current",
                    },
                }
            ]

        def list_iteration_capacities(self, iteration_id: str) -> list[dict[str, object]]:
            self.calls.append(("list_iteration_capacities", iteration_id, None))
            return []

        def query_work_item_snapshot(self, filter_expression: str, select_fields: tuple[str, ...], top=None) -> list[dict[str, object]]:
            self.calls.append(("query_work_item_snapshot", filter_expression, select_fields))
            del top
            raise QueryError("AreaPath unsupported")

        def query_odata_all(self, entity_set: str, params: dict[str, str]) -> list[dict[str, object]]:
            self.calls.append(("query_odata_all", entity_set, params.get("$expand")))
            assert entity_set == "WorkItemSnapshot"
            assert params["$expand"] == "Iteration"
            assert "AreaPath" not in params["$select"]
            assert "IterationPath" not in params["$select"]
            assert "WorkItemId eq 101" in params["$filter"]
            return []

    monkeypatch.setattr(gather, "ADOClient", _FallbackSprintADOClient)

    signals, ado_calls = gather._load_sprint_signals(program, workstreams, items, as_of)

    assert ado_calls == 4
    assert len(signals) == 1
    assert signals[0].source == "ado/sprint"
    assert signals[0].workstream_id == "deployment_readiness"
    assert "WS:deployment_readiness" in signals[0].entity_refs
    assert created_clients[0].calls == [
        ("list_team_iterations", "current", None),
        ("list_iteration_capacities", "iteration-24", None),
        (
            "query_work_item_snapshot",
            "( startswith(Area/AreaPath, 'One\\Adventure\\Acme') ) and ( WorkItemType eq 'Feature' ) and DateSK ge 20260426 and DateSK le 20260510 and IsLastRevisionOfDay eq true and not ( State eq 'Removed' )",
            gather._SPRINT_SNAPSHOT_FIELDS,
        ),
    ]
    assert created_clients[1].calls == [("query_odata_all", "WorkItemSnapshot", "Iteration")]


def test_load_sprint_signals_records_recent_snapshot_throughput(monkeypatch) -> None:
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="deployment_readiness",
            name="Deployment Readiness",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    items = (
        WorkItem(
            id=101,
            type="Feature",
            title="Checkpoint A",
            state="Closed",
            assigned_to="Priya",
            assigned_to_email="priya@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="One\\Sprint 24",
            target_date=date(2026, 5, 16),
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc),
        ),
        WorkItem(
            id=202,
            type="Feature",
            title="Checkpoint B",
            state="Closed",
            assigned_to="Priya",
            assigned_to_email="priya@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="One\\Sprint 24",
            target_date=date(2026, 5, 16),
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc),
        ),
    )
    as_of = datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc)

    class _FakeSprintADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def list_team_iterations(self, timeframe: str | None = None) -> list[dict[str, object]]:
            assert timeframe == "current"
            return [
                {
                    "id": "iteration-24",
                    "name": "Sprint 24",
                    "path": "One\\Sprint 24",
                    "attributes": {
                        "startDate": "2026-05-11T00:00:00Z",
                        "finishDate": "2026-05-15T00:00:00Z",
                        "timeFrame": "current",
                    },
                }
            ]

        def list_iteration_capacities(self, iteration_id: str) -> list[dict[str, object]]:
            assert iteration_id == "iteration-24"
            return []

        def query_work_item_snapshot(self, filter_expression: str, select_fields: tuple[str, ...], top=None) -> list[dict[str, object]]:
            del filter_expression, top
            assert "IterationPath" in select_fields
            return [
                {
                    "DateSK": 20260511,
                    "WorkItemId": 101,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260511,
                    "WorkItemId": 202,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260512,
                    "WorkItemId": 101,
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260512,
                    "WorkItemId": 202,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260513,
                    "WorkItemId": 101,
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260513,
                    "WorkItemId": 202,
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
            ]

    monkeypatch.setattr(gather, "ADOClient", _FakeSprintADOClient)

    signals, ado_calls = gather._load_sprint_signals(program, workstreams, items, as_of)

    assert ado_calls == 3
    assert len(signals) == 1
    assert "recent 1.0/day over 3 snapshots" in signals[0].text
    assert signals[0].metadata is not None
    assert signals[0].metadata["recent_completion_per_business_day"] == 1.0
    assert signals[0].metadata["recent_completion_snapshot_count"] == 3


def test_load_sprint_signals_records_previous_iteration_open_comparison(monkeypatch) -> None:
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="deployment_readiness",
            name="Deployment Readiness",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    items = (
        WorkItem(
            id=301,
            type="Feature",
            title="Checkpoint A",
            state="Active",
            assigned_to="Priya",
            assigned_to_email="priya@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="One\\Sprint 24",
            target_date=date(2026, 5, 16),
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc),
        ),
        WorkItem(
            id=302,
            type="Feature",
            title="Checkpoint B",
            state="Closed",
            assigned_to="Priya",
            assigned_to_email="priya@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="One\\Sprint 24",
            target_date=date(2026, 5, 16),
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc),
        ),
    )
    as_of = datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc)

    class _FakeSprintADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def list_team_iterations(self, timeframe: str | None = None) -> list[dict[str, object]]:
            assert timeframe == "current"
            return [
                {
                    "id": "iteration-24",
                    "name": "Sprint 24",
                    "path": "One\\Sprint 24",
                    "attributes": {
                        "startDate": "2026-05-11T00:00:00Z",
                        "finishDate": "2026-05-15T00:00:00Z",
                        "timeFrame": "current",
                    },
                }
            ]

        def list_iteration_capacities(self, iteration_id: str) -> list[dict[str, object]]:
            assert iteration_id == "iteration-24"
            return []

        def query_work_item_snapshot(self, filter_expression: str, select_fields: tuple[str, ...], top=None) -> list[dict[str, object]]:
            del filter_expression, top
            assert "IterationPath" in select_fields
            return [
                {
                    "DateSK": 20260509,
                    "WorkItemId": 201,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 23",
                },
                {
                    "DateSK": 20260509,
                    "WorkItemId": 202,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 23",
                },
                {
                    "DateSK": 20260510,
                    "WorkItemId": 201,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 23",
                },
                {
                    "DateSK": 20260510,
                    "WorkItemId": 202,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 23",
                },
                {
                    "DateSK": 20260511,
                    "WorkItemId": 301,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260511,
                    "WorkItemId": 302,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260512,
                    "WorkItemId": 301,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260512,
                    "WorkItemId": 302,
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260513,
                    "WorkItemId": 301,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260513,
                    "WorkItemId": 302,
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
            ]

    monkeypatch.setattr(gather, "ADOClient", _FakeSprintADOClient)

    signals, ado_calls = gather._load_sprint_signals(program, workstreams, items, as_of)

    assert ado_calls == 3
    assert len(signals) == 1
    assert "1 fewer open vs last sprint" in signals[0].text
    assert signals[0].metadata is not None
    assert signals[0].metadata["previous_iteration_open_item_count"] == 2


def test_load_sprint_signals_records_previous_iteration_throughput_comparison(monkeypatch) -> None:
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="deployment_readiness",
            name="Deployment Readiness",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    items = (
        WorkItem(
            id=401,
            type="Feature",
            title="Checkpoint A",
            state="Closed",
            assigned_to="Priya",
            assigned_to_email="priya@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="One\\Sprint 24",
            target_date=date(2026, 5, 16),
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc),
        ),
        WorkItem(
            id=402,
            type="Feature",
            title="Checkpoint B",
            state="Closed",
            assigned_to="Priya",
            assigned_to_email="priya@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="One\\Sprint 24",
            target_date=date(2026, 5, 16),
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc),
        ),
    )
    as_of = datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc)

    class _FakeSprintADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def list_team_iterations(self, timeframe: str | None = None) -> list[dict[str, object]]:
            assert timeframe == "current"
            return [
                {
                    "id": "iteration-24",
                    "name": "Sprint 24",
                    "path": "One\\Sprint 24",
                    "attributes": {
                        "startDate": "2026-05-11T00:00:00Z",
                        "finishDate": "2026-05-15T00:00:00Z",
                        "timeFrame": "current",
                    },
                }
            ]

        def list_iteration_capacities(self, iteration_id: str) -> list[dict[str, object]]:
            assert iteration_id == "iteration-24"
            return []

        def query_work_item_snapshot(self, filter_expression: str, select_fields: tuple[str, ...], top=None) -> list[dict[str, object]]:
            del filter_expression, top
            assert "IterationPath" in select_fields
            return [
                {
                        "DateSK": 20260506,
                    "WorkItemId": 301,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 23",
                },
                {
                        "DateSK": 20260506,
                    "WorkItemId": 302,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 23",
                },
                {
                        "DateSK": 20260507,
                    "WorkItemId": 301,
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 23",
                },
                {
                        "DateSK": 20260507,
                    "WorkItemId": 302,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 23",
                },
                {
                        "DateSK": 20260508,
                    "WorkItemId": 301,
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 23",
                },
                {
                        "DateSK": 20260508,
                    "WorkItemId": 302,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 23",
                },
                {
                    "DateSK": 20260511,
                    "WorkItemId": 401,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260511,
                    "WorkItemId": 402,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260512,
                    "WorkItemId": 401,
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260512,
                    "WorkItemId": 402,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260513,
                    "WorkItemId": 401,
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260513,
                    "WorkItemId": 402,
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
            ]

    monkeypatch.setattr(gather, "ADOClient", _FakeSprintADOClient)

    signals, ado_calls = gather._load_sprint_signals(program, workstreams, items, as_of)

    assert ado_calls == 3
    assert len(signals) == 1
    assert "0.5/day faster vs last sprint" in signals[0].text
    assert signals[0].metadata is not None
    assert signals[0].metadata["previous_iteration_completion_per_business_day"] == 0.5


def test_load_sprint_signals_records_previous_iteration_history(monkeypatch) -> None:
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="deployment_readiness",
            name="Deployment Readiness",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    items = (
        WorkItem(
            id=401,
            type="Feature",
            title="Checkpoint A",
            state="Closed",
            assigned_to="Priya",
            assigned_to_email="priya@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="One\\Sprint 24",
            target_date=date(2026, 5, 16),
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc),
        ),
        WorkItem(
            id=402,
            type="Feature",
            title="Checkpoint B",
            state="Closed",
            assigned_to="Priya",
            assigned_to_email="priya@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="One\\Sprint 24",
            target_date=date(2026, 5, 16),
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc),
        ),
    )
    as_of = datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc)

    class _FakeSprintADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def list_team_iterations(self, timeframe: str | None = None) -> list[dict[str, object]]:
            assert timeframe == "current"
            return [
                {
                    "id": "iteration-24",
                    "name": "Sprint 24",
                    "path": "One\\Sprint 24",
                    "attributes": {
                        "startDate": "2026-05-11T00:00:00Z",
                        "finishDate": "2026-05-15T00:00:00Z",
                        "timeFrame": "current",
                    },
                }
            ]

        def list_iteration_capacities(self, iteration_id: str) -> list[dict[str, object]]:
            assert iteration_id == "iteration-24"
            return []

        def query_work_item_snapshot(self, filter_expression: str, select_fields: tuple[str, ...], top=None) -> list[dict[str, object]]:
            del filter_expression, top
            assert "IterationPath" in select_fields
            return [
                {
                    "DateSK": 20260506,
                    "WorkItemId": 301,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 23",
                },
                {
                    "DateSK": 20260506,
                    "WorkItemId": 302,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 23",
                },
                {
                    "DateSK": 20260507,
                    "WorkItemId": 301,
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 23",
                },
                {
                    "DateSK": 20260507,
                    "WorkItemId": 302,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 23",
                },
                {
                    "DateSK": 20260508,
                    "WorkItemId": 301,
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 23",
                },
                {
                    "DateSK": 20260508,
                    "WorkItemId": 302,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 23",
                },
                {
                    "DateSK": 20260511,
                    "WorkItemId": 401,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260511,
                    "WorkItemId": 402,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260512,
                    "WorkItemId": 401,
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260512,
                    "WorkItemId": 402,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260513,
                    "WorkItemId": 401,
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260513,
                    "WorkItemId": 402,
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
            ]

    monkeypatch.setattr(gather, "ADOClient", _FakeSprintADOClient)

    signals, ado_calls = gather._load_sprint_signals(program, workstreams, items, as_of)

    assert ado_calls == 3
    assert len(signals) == 1
    assert "last sprint burndown 2->1->1 open" in signals[0].text
    assert "last sprint completion 0->1->1 done" in signals[0].text
    assert signals[0].metadata is not None
    assert signals[0].metadata["previous_iteration_open_history"] == {
        "2026-05-06": 2,
        "2026-05-07": 1,
        "2026-05-08": 1,
    }
    assert signals[0].metadata["previous_iteration_completed_history"] == {
        "2026-05-06": 0,
        "2026-05-07": 1,
        "2026-05-08": 1,
    }


def test_load_sprint_signals_records_snapshot_backed_three_sprint_history_summaries(monkeypatch) -> None:
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=21,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="deployment_readiness",
            name="Deployment Readiness",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    items = (
        WorkItem(
            id=501,
            type="Feature",
            title="Checkpoint A",
            state="Closed",
            assigned_to="Priya",
            assigned_to_email="priya@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="One\\Sprint 24",
            target_date=date(2026, 5, 16),
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc),
        ),
        WorkItem(
            id=502,
            type="Feature",
            title="Checkpoint B",
            state="Closed",
            assigned_to="Priya",
            assigned_to_email="priya@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="One\\Sprint 24",
            target_date=date(2026, 5, 16),
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc),
        ),
        WorkItem(
            id=503,
            type="Feature",
            title="Checkpoint C",
            state="Closed",
            assigned_to="Priya",
            assigned_to_email="priya@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="One\\Sprint 24",
            target_date=date(2026, 5, 16),
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc),
        ),
    )
    as_of = datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc)

    class _FakeSprintADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def list_team_iterations(self, timeframe: str | None = None) -> list[dict[str, object]]:
            assert timeframe == "current"
            return [
                {
                    "id": "iteration-24",
                    "name": "Sprint 24",
                    "path": "One\\Sprint 24",
                    "attributes": {
                        "startDate": "2026-05-11T00:00:00Z",
                        "finishDate": "2026-05-15T00:00:00Z",
                        "timeFrame": "current",
                    },
                }
            ]

        def list_iteration_capacities(self, iteration_id: str) -> list[dict[str, object]]:
            assert iteration_id == "iteration-24"
            return []

        def query_work_item_snapshot(self, filter_expression: str, select_fields: tuple[str, ...], top=None) -> list[dict[str, object]]:
            del filter_expression, top
            assert "IterationPath" in select_fields
            return [
                {
                    "DateSK": 20260422,
                    "WorkItemId": 101,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 22",
                },
                {
                    "DateSK": 20260422,
                    "WorkItemId": 102,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 22",
                },
                {
                    "DateSK": 20260422,
                    "WorkItemId": 103,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 22",
                },
                {
                    "DateSK": 20260423,
                    "WorkItemId": 101,
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 22",
                },
                {
                    "DateSK": 20260423,
                    "WorkItemId": 102,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 22",
                },
                {
                    "DateSK": 20260423,
                    "WorkItemId": 103,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 22",
                },
                {
                    "DateSK": 20260424,
                    "WorkItemId": 101,
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 22",
                },
                {
                    "DateSK": 20260424,
                    "WorkItemId": 102,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 22",
                },
                {
                    "DateSK": 20260424,
                    "WorkItemId": 103,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 22",
                },
                {
                    "DateSK": 20260506,
                    "WorkItemId": 301,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 23",
                },
                {
                    "DateSK": 20260506,
                    "WorkItemId": 302,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 23",
                },
                {
                    "DateSK": 20260506,
                    "WorkItemId": 303,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 23",
                },
                {
                    "DateSK": 20260507,
                    "WorkItemId": 301,
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 23",
                },
                {
                    "DateSK": 20260507,
                    "WorkItemId": 302,
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 23",
                },
                {
                    "DateSK": 20260507,
                    "WorkItemId": 303,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 23",
                },
                {
                    "DateSK": 20260508,
                    "WorkItemId": 301,
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 23",
                },
                {
                    "DateSK": 20260508,
                    "WorkItemId": 302,
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 23",
                },
                {
                    "DateSK": 20260508,
                    "WorkItemId": 303,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 23",
                },
                {
                    "DateSK": 20260511,
                    "WorkItemId": 501,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260511,
                    "WorkItemId": 502,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260511,
                    "WorkItemId": 503,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260512,
                    "WorkItemId": 501,
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260512,
                    "WorkItemId": 502,
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260512,
                    "WorkItemId": 503,
                    "State": "Active",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260513,
                    "WorkItemId": 501,
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260513,
                    "WorkItemId": 502,
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
                {
                    "DateSK": 20260513,
                    "WorkItemId": 503,
                    "State": "Closed",
                    "AreaPath": "One\\Adventure\\Acme",
                    "IterationPath": "One\\Sprint 24",
                },
            ]

    monkeypatch.setattr(gather, "ADOClient", _FakeSprintADOClient)

    signals, ado_calls = gather._load_sprint_signals(program, workstreams, items, as_of)

    assert ado_calls == 3
    assert len(signals) == 1
    assert signals[0].metadata is not None
    assert signals[0].metadata["three_iteration_average_completion_per_business_day"] == 1.0
    assert signals[0].metadata["three_iteration_completion_per_business_day_history"] == (0.5, 1.0, 1.5)
    assert signals[0].metadata["three_iteration_completed_history_series"] == ((0, 1, 1), (0, 2, 2), (0, 2, 3))
    assert signals[0].metadata["three_iteration_throughput_trend_direction"] == "up"
    assert signals[0].metadata["three_iteration_throughput_trend_delta_per_business_day"] == 1.0
    assert signals[0].metadata["three_iteration_average_open_item_count"] == 1
    assert signals[0].metadata["three_iteration_open_item_count_history"] == (2, 1, 0)
    assert signals[0].metadata["three_iteration_open_history_series"] == ((3, 2, 2), (3, 1, 1), (3, 1, 0))
    assert signals[0].metadata["three_iteration_open_trend_direction"] == "down"
    assert signals[0].metadata["three_iteration_open_trend_delta_count"] == -2


def test_load_sprint_signals_records_snapshot_backed_broader_historical_sprint_window(monkeypatch) -> None:
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=28,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="deployment_readiness",
            name="Deployment Readiness",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    items = (
        WorkItem(
            id=501,
            type="Feature",
            title="Checkpoint A",
            state="Closed",
            assigned_to="Priya",
            assigned_to_email="priya@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="One\\Sprint 24",
            target_date=date(2026, 5, 16),
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc),
        ),
    )
    as_of = datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc)

    class _FakeSprintADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def list_team_iterations(self, timeframe: str | None = None) -> list[dict[str, object]]:
            assert timeframe == "current"
            return [
                {
                    "id": "iteration-24",
                    "name": "Sprint 24",
                    "path": "One\\Sprint 24",
                    "attributes": {
                        "startDate": "2026-05-11T00:00:00Z",
                        "finishDate": "2026-05-15T00:00:00Z",
                        "timeFrame": "current",
                    },
                }
            ]

        def list_iteration_capacities(self, iteration_id: str) -> list[dict[str, object]]:
            assert iteration_id == "iteration-24"
            return []

        def query_work_item_snapshot(self, filter_expression: str, select_fields: tuple[str, ...], top=None) -> list[dict[str, object]]:
            del filter_expression, top
            assert "IterationPath" in select_fields
            return [
                {"DateSK": 20260414, "WorkItemId": 1, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 21"},
                {"DateSK": 20260414, "WorkItemId": 2, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 21"},
                {"DateSK": 20260414, "WorkItemId": 3, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 21"},
                {"DateSK": 20260415, "WorkItemId": 1, "State": "Closed", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 21"},
                {"DateSK": 20260415, "WorkItemId": 2, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 21"},
                {"DateSK": 20260415, "WorkItemId": 3, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 21"},
                {"DateSK": 20260416, "WorkItemId": 1, "State": "Closed", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 21"},
                {"DateSK": 20260416, "WorkItemId": 2, "State": "Closed", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 21"},
                {"DateSK": 20260416, "WorkItemId": 3, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 21"},
                {"DateSK": 20260422, "WorkItemId": 101, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 22"},
                {"DateSK": 20260422, "WorkItemId": 102, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 22"},
                {"DateSK": 20260422, "WorkItemId": 103, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 22"},
                {"DateSK": 20260423, "WorkItemId": 101, "State": "Closed", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 22"},
                {"DateSK": 20260423, "WorkItemId": 102, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 22"},
                {"DateSK": 20260423, "WorkItemId": 103, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 22"},
                {"DateSK": 20260424, "WorkItemId": 101, "State": "Closed", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 22"},
                {"DateSK": 20260424, "WorkItemId": 102, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 22"},
                {"DateSK": 20260424, "WorkItemId": 103, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 22"},
                {"DateSK": 20260506, "WorkItemId": 301, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 23"},
                {"DateSK": 20260506, "WorkItemId": 302, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 23"},
                {"DateSK": 20260506, "WorkItemId": 303, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 23"},
                {"DateSK": 20260507, "WorkItemId": 301, "State": "Closed", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 23"},
                {"DateSK": 20260507, "WorkItemId": 302, "State": "Closed", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 23"},
                {"DateSK": 20260507, "WorkItemId": 303, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 23"},
                {"DateSK": 20260508, "WorkItemId": 301, "State": "Closed", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 23"},
                {"DateSK": 20260508, "WorkItemId": 302, "State": "Closed", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 23"},
                {"DateSK": 20260508, "WorkItemId": 303, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 23"},
                {"DateSK": 20260511, "WorkItemId": 501, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 24"},
                {"DateSK": 20260511, "WorkItemId": 502, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 24"},
                {"DateSK": 20260511, "WorkItemId": 503, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 24"},
                {"DateSK": 20260512, "WorkItemId": 501, "State": "Closed", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 24"},
                {"DateSK": 20260512, "WorkItemId": 502, "State": "Closed", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 24"},
                {"DateSK": 20260512, "WorkItemId": 503, "State": "Active", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 24"},
                {"DateSK": 20260513, "WorkItemId": 501, "State": "Closed", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 24"},
                {"DateSK": 20260513, "WorkItemId": 502, "State": "Closed", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 24"},
                {"DateSK": 20260513, "WorkItemId": 503, "State": "Closed", "AreaPath": "One\\Adventure\\Acme", "IterationPath": "One\\Sprint 24"},
            ]

    monkeypatch.setattr(gather, "ADOClient", _FakeSprintADOClient)

    signals, ado_calls = gather._load_sprint_signals(program, workstreams, items, as_of)

    assert ado_calls == 3
    assert len(signals) == 1
    assert signals[0].metadata is not None
    assert signals[0].metadata["historical_iteration_window_count"] == 4
    assert signals[0].metadata["historical_completion_per_business_day_history"] == (1.0, 0.5, 1.0, 1.5)
    assert signals[0].metadata["historical_completed_history_series"] == ((0, 1, 2), (0, 1, 1), (0, 2, 2), (0, 2, 3))
    assert signals[0].metadata["historical_throughput_trend_direction"] is None
    assert signals[0].metadata["historical_throughput_trend_delta_per_business_day"] is None
    assert signals[0].metadata["historical_open_item_count_history"] == (1, 2, 1, 0)
    assert signals[0].metadata["historical_open_history_series"] == ((3, 2, 1), (3, 2, 2), (3, 1, 1), (3, 1, 0))
    assert signals[0].metadata["historical_open_trend_direction"] is None
    assert signals[0].metadata["historical_open_trend_delta_count"] is None


def test_load_pipeline_signals_summarizes_recent_failed_runs(monkeypatch) -> None:
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="deployment_readiness",
            name="Deployment Readiness",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
            ado_pipeline_ids=("42",),
        ),
    )
    as_of = datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc)
    created_clients: list[object] = []

    class _FakePipelineADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs
            self.calls: list[tuple[str, str, int]] = []
            created_clients.append(self)

        def list_pipeline_runs(self, pipeline_id: str, top: int = 10) -> list[dict[str, object]]:
            self.calls.append(("list_pipeline_runs", pipeline_id, top))
            return [
                {
                    "id": 105,
                    "name": "Build Validation",
                    "state": "completed",
                    "result": "succeeded",
                    "createdDate": "2026-05-11T10:00:00Z",
                    "finishedDate": "2026-05-11T10:15:00Z",
                    "_links": {"web": {"href": "https://dev.azure.com/your-org/One/_build/results?buildId=105"}},
                },
                {
                    "id": 104,
                    "name": "Build Validation",
                    "state": "completed",
                    "result": "failed",
                    "createdDate": "2026-05-10T09:00:00Z",
                    "finishedDate": "2026-05-10T09:16:00Z",
                    "_links": {"web": {"href": "https://dev.azure.com/your-org/One/_build/results?buildId=104"}},
                },
                {
                    "id": 103,
                    "name": "Build Validation",
                    "state": "completed",
                    "result": "succeeded",
                    "createdDate": "2026-05-09T09:00:00Z",
                    "finishedDate": "2026-05-09T09:10:00Z",
                },
            ]

    monkeypatch.setattr(ado_pipeline_stage, "ADOClient", _FakePipelineADOClient)

    signals, ado_calls = gather._load_pipeline_signals(program, workstreams, as_of)

    assert ado_calls == 1
    assert len(signals) == 1
    assert signals[0].source == "ado/pipeline"
    assert signals[0].workstream_id == "deployment_readiness"
    assert "pipeline Build Validation failed 1 of last 3 runs in 14d" in signals[0].text
    assert "latest failure #104 on 2026-05-10" in signals[0].text
    assert "latest run #105 succeeded" in signals[0].text
    assert signals[0].entity_refs == ("ado/pipeline:42", "WS:deployment_readiness")
    assert signals[0].metadata is not None
    assert signals[0].metadata["pipeline_ids"] == ["42"]
    assert signals[0].metadata["pipelines"][0]["failed_run_count"] == 1
    assert signals[0].metadata["pipelines"][0]["latest_failure_run_id"] == 104
    assert signals[0].metadata["pipelines"][0]["latest_failure_url"] == "https://dev.azure.com/your-org/One/_build/results?buildId=104"
    assert created_clients[0].calls == [("list_pipeline_runs", "42", 10)]


def test_load_pipeline_signals_appends_open_pull_request_summary(monkeypatch) -> None:
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="deployment_readiness",
            name="Deployment Readiness",
            area_paths=("One\\Adventure\\Acme",),
            ado_repository_ids=("repo-42",),
            dri_email="maintainer@example.com",
        ),
    )
    as_of = datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc)
    created_clients: list[object] = []

    class _FakePullRequestADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs
            self.calls: list[tuple[str, str, str, int]] = []
            created_clients.append(self)

        def list_pipeline_runs(self, pipeline_id: str, top: int = 10) -> list[dict[str, object]]:
            self.calls.append(("list_pipeline_runs", pipeline_id, "", top))
            return []

        def list_pull_requests(self, repository_id: str, *, status: str = "active", top: int = 100) -> list[dict[str, object]]:
            self.calls.append(("list_pull_requests", repository_id, status, top))
            return [
                {
                    "pullRequestId": 301,
                    "title": "Stabilize rollout for WI:12345",
                    "status": "active",
                    "creationDate": "2026-05-02T08:00:00Z",
                    "isDraft": False,
                    "repository": {"id": repository_id, "name": "XStoreApp"},
                },
                {
                    "pullRequestId": 302,
                    "title": "Tune validation gates",
                    "status": "active",
                    "creationDate": "2026-05-08T08:00:00Z",
                    "isDraft": True,
                    "repository": {"id": repository_id, "name": "XStoreApp"},
                },
            ]

    monkeypatch.setattr(ado_pipeline_stage, "ADOClient", _FakePullRequestADOClient)

    signals, ado_calls = gather._load_pipeline_signals(program, workstreams, as_of)

    assert ado_calls == 1
    assert len(signals) == 1
    assert signals[0].source == "ado/pr"
    assert signals[0].workstream_id == "deployment_readiness"
    assert "repo XStoreApp has 2 open PRs; P90 age 10.0d; oldest #301 10.0d" in signals[0].text
    assert signals[0].entity_refs == ("PR:XStoreApp/301", "WI:12345", "PR:XStoreApp/302", "WS:deployment_readiness")
    assert signals[0].metadata is not None
    assert signals[0].metadata["repository_ids"] == ["repo-42"]
    assert signals[0].metadata["repositories"][0]["open_pr_count"] == 2
    assert signals[0].metadata["repositories"][0]["p90_age_days"] == 10.0
    assert signals[0].metadata["repositories"][0]["oldest_pr_id"] == 301
    assert created_clients[0].calls == [("list_pull_requests", "repo-42", "active", 100)]


def test_load_sprint_signals_degrades_when_capacity_query_fails(monkeypatch) -> None:
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="deployment_readiness",
            name="Deployment Readiness",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    items = (
        WorkItem(
            id=101,
            type="Feature",
            title="Checkpoint A",
            state="Active",
            assigned_to="Priya",
            assigned_to_email="priya@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="One\\Sprint 24",
            target_date=date(2026, 5, 16),
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        ),
    )
    as_of = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)

    class _FailingCapacityADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def list_team_iterations(self, timeframe: str | None = None) -> list[dict[str, object]]:
            assert timeframe == "current"
            return [
                {
                    "id": "iteration-24",
                    "name": "Sprint 24",
                    "path": "One\\Sprint 24",
                    "attributes": {
                        "startDate": "2026-05-04T00:00:00Z",
                        "finishDate": "2026-05-15T00:00:00Z",
                        "timeFrame": "current",
                    },
                }
            ]

        def list_iteration_capacities(self, iteration_id: str) -> list[dict[str, object]]:
            raise QueryError(f"capacity unavailable for {iteration_id}")

        def query_work_item_snapshot(self, filter_expression: str, select_fields: tuple[str, ...], top=None) -> list[dict[str, object]]:
            del filter_expression, select_fields, top
            return []

    monkeypatch.setattr(gather, "ADOClient", _FailingCapacityADOClient)

    signals, ado_calls = gather._load_sprint_signals(program, workstreams, items, as_of)

    assert ado_calls == 3
    assert len(signals) == 1
    assert "team capacity" not in signals[0].text
    assert signals[0].metadata is not None
    assert signals[0].metadata["team_member_count"] is None
    assert signals[0].metadata["total_capacity_per_day"] is None


def test_load_sprint_signals_uses_team_specific_iterations_when_configured(monkeypatch) -> None:
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme", "One\\Adventure\\Contoso"),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="deployment_readiness",
            name="Deployment Readiness",
            area_paths=("One\\Adventure\\Acme",),
            ado_team="Acme Team",
            dri_email="maintainer@example.com",
        ),
        Workstream(
            id="dd_readiness",
            name="DD Readiness",
            area_paths=("One\\Adventure\\Contoso",),
            ado_team="DD Team",
            dri_email="maintainer@example.com",
        ),
    )
    items = (
        WorkItem(
            id=101,
            type="Feature",
            title="Acme checkpoint",
            state="Active",
            assigned_to="Priya",
            assigned_to_email="priya@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="One\\Sprint 24",
            target_date=date(2026, 5, 16),
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        ),
        WorkItem(
            id=202,
            type="Feature",
            title="DD checkpoint",
            state="Closed",
            assigned_to="Alex",
            assigned_to_email="alex@example.com",
            area_path="One\\Adventure\\Contoso",
            iteration_path="One\\Sprint 30",
            target_date=date(2026, 5, 20),
            risk_level=RiskLevel.LOW,
            tags=[],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        ),
    )
    as_of = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    created_clients: list[object] = []

    class _FakeTeamSprintADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs
            self.calls: list[tuple[str, str | None, str | None]] = []
            created_clients.append(self)

        def list_team_iterations(self, timeframe: str | None = None, team: str | None = None) -> list[dict[str, object]]:
            self.calls.append(("list_team_iterations", team, timeframe))
            if team == "Acme Team":
                return [
                    {
                        "id": "iteration-acme",
                        "name": "Sprint 24",
                        "path": "One\\Sprint 24",
                        "attributes": {
                            "startDate": "2026-05-04T00:00:00Z",
                            "finishDate": "2026-05-15T00:00:00Z",
                            "timeFrame": "current",
                        },
                    }
                ]
            if team == "DD Team":
                return [
                    {
                        "id": "iteration-dd",
                        "name": "Sprint 30",
                        "path": "One\\Sprint 30",
                        "attributes": {
                            "startDate": "2026-05-04T00:00:00Z",
                            "finishDate": "2026-05-15T00:00:00Z",
                            "timeFrame": "current",
                        },
                    }
                ]
            return []

        def list_iteration_capacities(self, iteration_id: str, team: str | None = None) -> list[dict[str, object]]:
            self.calls.append(("list_iteration_capacities", team, iteration_id))
            if team == "Acme Team":
                return [
                    {
                        "teamMember": {"displayName": "Priya"},
                        "activities": [{"name": "Development", "capacityPerDay": 6}],
                        "daysOff": [],
                    }
                ]
            if team == "DD Team":
                return [
                    {
                        "teamMember": {"displayName": "Alex"},
                        "activities": [{"name": "Development", "capacityPerDay": 3}],
                        "daysOff": [],
                    }
                ]
            return []

        def query_work_item_snapshot(self, filter_expression: str, select_fields: tuple[str, ...], top=None) -> list[dict[str, object]]:
            del filter_expression, select_fields, top
            return []

    monkeypatch.setattr(gather, "ADOClient", _FakeTeamSprintADOClient)

    signals, ado_calls = gather._load_sprint_signals(program, workstreams, items, as_of)

    assert ado_calls == 5
    assert len(signals) == 2
    signal_by_workstream = {signal.workstream_id: signal for signal in signals}
    assert signal_by_workstream["deployment_readiness"].metadata["ado_team"] == "Acme Team"
    assert signal_by_workstream["deployment_readiness"].metadata["iteration_name"] == "Sprint 24"
    assert signal_by_workstream["deployment_readiness"].metadata["total_capacity_per_day"] == 6.0
    assert signal_by_workstream["dd_readiness"].metadata["ado_team"] == "DD Team"
    assert signal_by_workstream["dd_readiness"].metadata["iteration_name"] == "Sprint 30"
    assert signal_by_workstream["dd_readiness"].metadata["completed_item_count"] == 1
    assert signal_by_workstream["dd_readiness"].metadata["total_capacity_per_day"] == 3.0
    assert created_clients[0].calls == [
        ("list_team_iterations", "Acme Team", "current"),
        ("list_iteration_capacities", "Acme Team", "iteration-acme"),
        ("list_team_iterations", "DD Team", "current"),
        ("list_iteration_capacities", "DD Team", "iteration-dd"),
    ]


def test_gather_command_with_analytics_appends_auto_approved_signals(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    db_root = tmp_path / "vertex-db"
    monkeypatch.setattr(gather, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setenv("VERTEX_DB_PATH", str(db_root))

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_resolve_uil_channel_binding_for_gather", lambda *_a, **_k: object())
    monkeypatch.setattr(gather, "_load_ado_items_via_uil", lambda _p, _ws, _ao, **__: ((), (), 0))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(
        gather,
        "_load_analytics_signals",
        lambda program, workstreams, as_of, programs_root=None, **_: (
            (
                Signal(
                    id="analytics-1",
                    timestamp=as_of,
                    source="ado/analytics",
                    program_id="acme",
                    workstream_id="acme",
                    entity_refs=(),
                    text="Acme: ADO Analytics snapshot 2026-05-10; 2 items in scope; 1 completed in window",
                    raw_ref="ado-analytics:acme:20260510:20260426:20260510",
                    confidence=Confidence.HIGH,
                    metadata={"latest_snapshot_date_sk": 20260510},
                ),
            ),
            1,
        ),
    )

    result = runner.invoke(app, ["gather", "--program", "acme", "--analytics"])

    signals = read_signals("acme", programs_root=programs_root)
    reviews = read_review_log("acme", programs_root=programs_root)

    assert result.exit_code == 0
    assert "Gathered 1 signals (1 new, 0 pending review) for acme" in result.output
    assert len(signals) == 1
    assert signals[0].source == "ado/analytics"
    assert len(reviews) == 1
    assert reviews[0].signal_id == signals[0].id
    assert reviews[0].decision == "approved"
    assert reviews[0].reviewed_by == "system"
    assert _read_ingestion_run_rows("acme", db_root=db_root) == [
        ("ado/analytics", "success", 1),
        ("ado/comment", "success", 0),
        ("ado/dependency", "success", 0),
        ("ado/revision", "success", 0),
        ("vertex/freshness", "success", 0),
    ]


def test_gather_program_writes_extracted_proposed_decisions(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    revision = Revision(
        work_item_id=1234,
        rev_number=2,
        changed_date=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        changed_by="Priya",
        changed_by_email="priya@example.com",
        fields_changed={"System.History": (None, "LT approved the guarded rollout for WI:1234.")},
    )
    item = WorkItem(
        id=1234,
        type="Feature",
        title="Ramp checkpoint",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="One\\FY26\\Q4",
        target_date=date(2026, 5, 17),
        risk_level=RiskLevel.MEDIUM,
        tags=["acme"],
        custom_fields={},
        revisions=[revision],
        comments=[],
        fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_resolve_uil_channel_binding_for_gather", lambda *_a, **_k: object())
    monkeypatch.setattr(gather, "_load_ado_items_via_uil", lambda _p, _ws, _ao, **__: ((item,), (item,), 0))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(
        "src.commands.gather_pipeline.persistence_stage.extract_actions_from_signals",
        lambda signals, program_id: (),
    )

    def _fake_extract_decisions(signals, program_id):
        if not signals:
            return ()
        return (
            DecisionEntry(
                id="decision-auto-1",
                program_id=program_id,
                title="Guarded rollout approved",
                context=f"Derived from {signals[0].source} signal {signals[0].id}.",
                decision="LT approved the guarded rollout for WI:1234.",
                rationale=None,
                alternatives_considered=(),
                decided_by="priya",
                decision_date=signals[0].timestamp.date(),
                status=DecisionStatus.PROPOSED,
                superseded_by=None,
                linked_claim_id=None,
                linked_risk_id=None,
                linked_action_ids=(),
                workstream_id="acme",
                entity_refs=("WI:1234",),
            ),
        )

    monkeypatch.setattr(
        "src.commands.gather_pipeline.persistence_stage.extract_decisions_from_signals",
        _fake_extract_decisions,
    )

    artifacts = gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    decisions = load_decisions("acme", programs_root=programs_root)

    assert artifacts.new_signals == 1
    assert len(decisions) == 1
    assert decisions[0].id == "decision-auto-1"
    assert decisions[0].status is DecisionStatus.PROPOSED
    assert decisions[0].entity_refs == ("WI:1234",)


def test_gather_command_with_sprints_appends_auto_approved_signals(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    db_root = tmp_path / "vertex-db"
    monkeypatch.setattr(gather, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setenv("VERTEX_DB_PATH", str(db_root))

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_resolve_uil_channel_binding_for_gather", lambda *_a, **_k: object())
    monkeypatch.setattr(gather, "_load_ado_items_via_uil", lambda _p, _ws, _ao, **__: ((), (), 0))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(
        gather,
        "_load_sprint_signals",
        lambda program, workstreams, items, as_of, **_: (
            (
                Signal(
                    id="sprint-1",
                    timestamp=as_of,
                    source="ado/sprint",
                    program_id="acme",
                    workstream_id="acme",
                    entity_refs=(),
                    text="Acme: ADO sprint Sprint 24; 4 committed; 3 completed; 1 open; 75% complete",
                    raw_ref="ado-sprint:acme:iteration-24:2026-05-10",
                    confidence=Confidence.HIGH,
                    metadata={"iteration_name": "Sprint 24", "committed_item_count": 4},
                ),
            ),
            1,
        ),
    )

    result = runner.invoke(app, ["gather", "--program", "acme", "--sprints"])

    signals = read_signals("acme", programs_root=programs_root)
    reviews = read_review_log("acme", programs_root=programs_root)

    assert result.exit_code == 0
    assert "Gathered 1 signals (1 new, 0 pending review) for acme" in result.output
    assert len(signals) == 1
    assert signals[0].source == "ado/sprint"
    assert len(reviews) == 1
    assert reviews[0].signal_id == signals[0].id
    assert reviews[0].decision == "approved"
    assert reviews[0].reviewed_by == "system"
    assert _read_ingestion_run_rows("acme", db_root=db_root) == [
        ("ado/comment", "success", 0),
        ("ado/dependency", "success", 0),
        ("ado/revision", "success", 0),
        ("ado/sprint", "success", 1),
        ("vertex/freshness", "success", 0),
    ]


def test_gather_command_with_pipelines_appends_auto_approved_signals(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    db_root = tmp_path / "vertex-db"
    monkeypatch.setattr(gather, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setenv("VERTEX_DB_PATH", str(db_root))

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
            ado_pipeline_ids=("42",),
        ),
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_resolve_uil_channel_binding_for_gather", lambda *_a, **_k: object())
    monkeypatch.setattr(gather, "_load_ado_items_via_uil", lambda _p, _ws, _ao, **__: ((), (), 0))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(
        gather,
        "_load_pipeline_signals",
        lambda program, workstreams, as_of, **kwargs: (
            (
                Signal(
                    id="pipeline-1",
                    timestamp=as_of,
                    source="ado/pipeline",
                    program_id="acme",
                    workstream_id="acme",
                    entity_refs=(),
                    text="Acme: pipeline Build Validation failed 1 of last 3 runs in 14d; latest failure #104 on 2026-05-10",
                    raw_ref="ado-pipeline:acme:42:2026-05-12",
                    confidence=Confidence.HIGH,
                    metadata={"pipeline_ids": ["42"]},
                ),
            ),
            1,
        ),
    )

    result = runner.invoke(app, ["gather", "--program", "acme", "--pipelines"])

    signals = read_signals("acme", programs_root=programs_root)
    reviews = read_review_log("acme", programs_root=programs_root)

    assert result.exit_code == 0
    assert "Gathered 1 signals (1 new, 0 pending review) for acme" in result.output
    assert len(signals) == 1
    assert signals[0].source == "ado/pipeline"
    assert len(reviews) == 1
    assert reviews[0].signal_id == signals[0].id
    assert reviews[0].decision == "approved"
    assert reviews[0].reviewed_by == "system"
    ingestion_runs = _read_ingestion_run_rows("acme", db_root=db_root)
    assert ("ado/pipeline", "success", 1) in ingestion_runs
    assert ("ado/pr", "success", 0) in ingestion_runs
    assert ("ado/revision", "success", 0) in ingestion_runs
    assert ("vertex/freshness", "success", 0) in ingestion_runs


def test_gather_command_with_pull_request_signals_records_distinct_pr_ingestion_run(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    db_root = tmp_path / "vertex-db"
    monkeypatch.setattr(gather, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setenv("VERTEX_DB_PATH", str(db_root))

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
            ado_repository_ids=("repo-42",),
        ),
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_resolve_uil_channel_binding_for_gather", lambda *_a, **_k: object())
    monkeypatch.setattr(gather, "_load_ado_items_via_uil", lambda _p, _ws, _ao, **__: ((), (), 0))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(
        gather,
        "_load_pipeline_signals",
        lambda program, workstreams, as_of, **kwargs: (
            (
                Signal(
                    id="pr-1",
                    timestamp=as_of,
                    source="ado/pr",
                    program_id="acme",
                    workstream_id="acme",
                    entity_refs=("PR:XStoreApp/301",),
                    text="Acme: repo XStoreApp has 1 open PR; P90 age 10.0d; oldest #301 10.0d",
                    raw_ref="ado-pr:acme:repo-42:2026-05-12",
                    confidence=Confidence.HIGH,
                    metadata={"repository_ids": ["repo-42"]},
                ),
            ),
            1,
        ),
    )

    result = runner.invoke(app, ["gather", "--program", "acme", "--pipelines"])

    signals = read_signals("acme", programs_root=programs_root)
    reviews = read_review_log("acme", programs_root=programs_root)

    assert result.exit_code == 0
    assert len(signals) == 1
    assert signals[0].source == "ado/pr"
    assert len(reviews) == 1
    assert reviews[0].signal_id == signals[0].id
    ingestion_runs = _read_ingestion_run_rows("acme", db_root=db_root)
    assert ("ado/pr", "success", 1) in ingestion_runs
    assert ("ado/pipeline", "success", 0) in ingestion_runs


def test_gather_program_appends_kusto_signals_and_auto_reviews_validated_queries(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        kusto=KustoConfig(enabled=True),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    knowledge = KnowledgeStore(
        people_directory=(),
        people_profiles=(),
        teams=(),
        products=(),
        golden_queries=(
            KustoQuery(
                id="velocity-p50",
                cluster="https://adventure.kusto.windows.net",
                database="xdataanalytics",
                kql="Metrics | take 1",
                section="Deployment Velocity",
                render_as="metric_highlight",
                confidence="high",
                program_ids=("acme",),
                validated=True,
            ),
            KustoQuery(
                id="fleet-health",
                cluster="https://adventure.kusto.windows.net",
                database="xdataanalytics",
                kql="FleetHealth | take 1",
                section="Fleet Health",
                render_as="table",
                confidence="medium",
                program_ids=("acme",),
                validated=True,
            ),
        ),
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(gather, "load_program_knowledge", lambda program_id, programs_root: knowledge)

    artifacts = gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_kusto=True,
        kusto_query_executor=_fake_kusto_query_executor,
    )

    signals = read_signals("acme", programs_root=programs_root)
    reviews = read_review_log("acme", programs_root=programs_root)
    signals_by_query = {signal.metadata["query_id"]: signal for signal in signals if signal.metadata is not None}

    assert artifacts.discovered_signals == 2
    assert artifacts.new_signals == 2
    assert artifacts.pending_review == 0
    assert artifacts.auto_reviews_written == 2
    assert len(reviews) == 2
    assert {review.signal_id for review in reviews} == {signal.id for signal in signals_by_query.values()}
    assert signals_by_query["velocity-p50"].metadata["validated"] is True
    assert signals_by_query["fleet-health"].metadata["validated"] is True
def test_gather_program_appends_program_activated_golden_query_even_when_program_ids_do_not_match(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        kusto=KustoConfig(enabled=True),
        golden_queries=("fabrikam-xhealth-m0",),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    knowledge = KnowledgeStore(
        people_directory=(),
        people_profiles=(),
        teams=(),
        products=(),
        golden_queries=(
            KustoQuery(
                id="fabrikam-xhealth-m0",
                cluster="https://1es.kusto.windows.net",
                database="AzureDevOps",
                kql="WorkItem | take 1",
                section="Fabrikam M0 xHealth Backlog",
                render_as="table",
                confidence="high",
                program_ids=("fabrikam",),
                validated=True,
            ),
        ),
    )
    executed_query_ids: list[str] = []

    def _activated_query_executor(query: KustoQuery) -> list[dict[str, object]]:
        executed_query_ids.append(query.id)
        return [{"WorkItemId": "1234", "Title": "Cross-program dependency item"}]

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(gather, "load_program_knowledge", lambda program_id, programs_root: knowledge)

    artifacts = gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_kusto=True,
        kusto_query_executor=_activated_query_executor,
    )

    signals = read_signals("acme", programs_root=programs_root)
    signals_by_query = {signal.metadata["query_id"]: signal for signal in signals if signal.metadata is not None}

    assert artifacts.discovered_signals == 1
    assert executed_query_ids == ["fabrikam-xhealth-m0"]
    assert signals_by_query["fabrikam-xhealth-m0"].source == "kusto"


def test_gather_program_renders_kusto_query_templates_before_execution(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        kusto=KustoConfig(enabled=True),
    )
    knowledge = KnowledgeStore(
        people_directory=(),
        people_profiles=(),
        teams=(),
        products=(),
        golden_queries=(
            KustoQuery(
                id="templated-query",
                cluster="https://adventure.kusto.windows.net",
                database="xdataanalytics",
                kql='Metrics | where Program == "{program_id}" | where AreaPath == "{area_path}" | where Timestamp > ago({date_range}) | take 1',
                section="Deployment Velocity",
                render_as="table",
                confidence="high",
                program_ids=("acme",),
                validated=True,
            ),
        ),
    )

    rendered_kql: list[str] = []

    def _templated_kusto_executor(query: KustoQuery) -> list[dict[str, object]]:
        rendered_kql.append(query.kql)
        return [{"Value": 1}]

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, ()))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(gather, "load_program_knowledge", lambda program_id, programs_root: knowledge)

    gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_kusto=True,
        kusto_query_executor=_templated_kusto_executor,
    )

    assert rendered_kql == [
        'Metrics | where Program == "acme" | where AreaPath == "One\\Adventure\\Acme" | where Timestamp > ago(14d) | take 1'
    ]


def test_gather_program_degrades_gracefully_when_kusto_fails(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    current_time = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        kusto=KustoConfig(enabled=True),
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, ()))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(
        gather,
        "load_program_knowledge",
        lambda program_id, programs_root: KnowledgeStore(
            people_directory=(),
            people_profiles=(),
            teams=(),
            products=(),
            golden_queries=(
                KustoQuery(
                    id="velocity-p50",
                    cluster="https://adventure.kusto.windows.net",
                    database="xdataanalytics",
                    kql="Metrics | take 1",
                    section="Deployment Velocity",
                    render_as="metric_highlight",
                    confidence="high",
                    program_ids=("acme",),
                    validated=True,
                ),
            ),
        ),
    )

    artifacts = gather.gather_program(
        "acme",
        as_of=current_time,
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_kusto=True,
        kusto_query_executor=lambda query: (_ for _ in ()).throw(QueryError("kusto unavailable")),
    )

    signals = read_signals("acme", programs_root=programs_root)
    gather_state = load_gather_state("acme", programs_root=programs_root)

    assert artifacts.integration_error_count == 1
    assert len(artifacts.integration_errors) == 1
    assert artifacts.integration_errors[0].source == "kusto"
    assert artifacts.integration_errors[0].stage == "gather"
    assert artifacts.integration_errors[0].operator_action is not None
    assert artifacts.discovered_signals == 1
    assert len(signals) == 1
    assert signals[0].source == "system"
    assert signals[0].metadata is not None
    assert signals[0].metadata["integration_source"] == "kusto"
    assert gather_state is not None
    assert gather_state.integration_errors == 1
    assert len(gather_state.integration_error_details) == 1
    assert gather_state.integration_error_details[0].source == "kusto"
    assert gather_state.channels["kusto"]["last_error"] == "kusto unavailable"
    outcomes = {outcome.channel: outcome for outcome in artifacts.channel_outcomes}
    assert outcomes["ado"].degraded is False
    assert outcomes["kusto"].degraded is True
    assert outcomes["kusto"].degrade_reason == "kusto unavailable"
    assert outcomes["kusto"].elapsed_seconds >= 0


def test_gather_program_persists_deduped_saved_query_runtime_failures(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    current_time = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
            ado_saved_query_ids=("query-1",),
        ),
    )

    class _FakeSavedQueryFailureADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def query_all(self, filter_expression: str, select_fields: tuple[str, ...]) -> list[dict[str, object]]:
            del filter_expression, select_fields
            return [
                {
                    "WorkItemId": 101,
                    "WorkItemType": "Feature",
                    "Title": "Primary row",
                    "State": "Active",
                    "ChangedDate": "2026-05-09T08:00:00Z",
                }
            ]

        def count_work_items(self, filter_expression: str) -> int:
            del filter_expression
            return 1

        def get_saved_query(self, query_id: str) -> dict[str, object]:
            assert query_id == "query-1"
            return {"id": query_id, "wiql": "Select [System.Id] From WorkItems"}

        def execute_wiql(self, wiql: str, top: int | None = None) -> list[int]:
            del wiql, top
            raise QueryError("TF51011 invalid area path")

        def query_work_items_batch(self, work_item_ids: list[int], fields: tuple[str, ...]) -> list[dict[str, object]]:
            assert fields == gather._BATCH_FIELDS
            return [
                {
                    "id": 101,
                    "fields": {
                        "System.Id": 101,
                        "System.WorkItemType": "Feature",
                        "System.Title": "Primary row",
                        "System.State": "Active",
                        "System.AreaPath": "One\\Adventure\\Acme",
                        "System.IterationPath": "One\\FY26\\Q4",
                        "System.ChangedDate": "2026-05-09T08:00:00Z",
                    },
                }
            ]

        def list_work_item_revisions(self, work_item_id: int) -> list[dict[str, object]]:
            del work_item_id
            return []

    def _fake_uil_with_saved_query_error(prog, wss, as_of, *, integration_error_sink=None, **kwargs):
        if integration_error_sink is not None:
            integration_error_sink.append(
                gather._build_integration_error(
                    source="ado",
                    stage="saved_query",
                    error="query-1: TF51011 invalid area path",
                )
            )
        return (), (), 5

    monkeypatch.setenv("VERTEX_UIL_ADO", "1")
    monkeypatch.setattr(gather, "_resolve_uil_channel_binding_for_gather", lambda *_a, **_k: object())
    monkeypatch.setattr(gather, "_load_ado_items_via_uil", _fake_uil_with_saved_query_error)
    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(
        gather,
        "load_program_knowledge",
        lambda program_id, programs_root: KnowledgeStore(
            people_directory=(),
            people_profiles=(),
            teams=(),
            products=(),
            golden_queries=(),
        ),
    )

    artifacts = gather.gather_program(
        "acme",
        as_of=current_time,
        programs_root=programs_root,
    )

    gather_state = load_gather_state("acme", programs_root=programs_root)

    assert artifacts.integration_error_count == 1
    assert len(artifacts.integration_errors) == 1
    assert artifacts.integration_errors[0].source == "ado"
    assert artifacts.integration_errors[0].stage == "saved_query"
    assert "query-1" in artifacts.integration_errors[0].message
    assert gather_state is not None
    assert gather_state.integration_errors == 1
    assert len(gather_state.integration_error_details) == 1
    assert gather_state.integration_error_details[0].source == "ado"
    assert gather_state.integration_error_details[0].stage == "saved_query"
    assert "query-1" in gather_state.integration_error_details[0].message
    assert gather_state.channels["ado"]["last_error"] == gather_state.integration_error_details[0].message
    assert gather_state.channels["ado"]["failure_mode"] == "degrade"


def test_gather_program_scopes_kusto_queries_by_workstream_signal_sources(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme", "One\\Adventure\\Contoso"),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        kusto=KustoConfig(enabled=True),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(kusto_query_ids=("velocity-p50",)),
        ),
        Workstream(
            id="dd_on_pf",
            name="Contoso",
            area_paths=("One\\Adventure\\Contoso",),
            signal_sources=WorkstreamSignalSources(kusto_query_ids=("fleet-health",)),
        ),
    )
    knowledge = KnowledgeStore(
        people_directory=(),
        people_profiles=(),
        teams=(),
        products=(),
        golden_queries=(
            KustoQuery(
                id="velocity-p50",
                cluster="https://adventure.kusto.windows.net",
                database="xdataanalytics",
                kql="Metrics | take 1",
                section="Deployment Velocity",
                render_as="metric_highlight",
                confidence="high",
                program_ids=("acme",),
                validated=True,
            ),
            KustoQuery(
                id="fleet-health",
                cluster="https://adventure.kusto.windows.net",
                database="xdataanalytics",
                kql="FleetHealth | take 1",
                section="Fleet Health",
                render_as="table",
                confidence="medium",
                program_ids=("acme",),
                validated=True,
            ),
            KustoQuery(
                id="unscoped-query",
                cluster="https://adventure.kusto.windows.net",
                database="xdataanalytics",
                kql="Other | take 1",
                section="Other",
                render_as="table",
                confidence="low",
                program_ids=("acme",),
                validated=True,
            ),
        ),
    )
    executed_query_ids: list[str] = []

    def _recording_executor(query: KustoQuery) -> list[dict[str, object]]:
        executed_query_ids.append(query.id)
        return _fake_kusto_query_executor(query)

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(gather, "load_program_knowledge", lambda program_id, programs_root: knowledge)

    artifacts = gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_kusto=True,
        kusto_query_executor=_recording_executor,
    )

    signals = read_signals("acme", programs_root=programs_root)
    signals_by_query = {signal.metadata["query_id"]: signal for signal in signals if signal.metadata is not None}

    assert artifacts.discovered_signals == 2
    assert executed_query_ids == ["velocity-p50", "fleet-health"]
    assert signals_by_query["velocity-p50"].workstream_id == "acme"
    assert signals_by_query["fleet-health"].workstream_id == "dd_on_pf"
    assert "unscoped-query" not in signals_by_query


def test_gather_program_appends_refresh_on_gather_kusto_kpi_signals(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
schema_version: "1.0"
kpis:
  - id: acme-deployment-velocity
    workstream_ids: [acme]
    cluster: https://adventure.kusto.windows.net
    database: xdataanalytics
    kql: Metrics | take 1
    section: Deployment Velocity
    render_as: metric_highlight
    confidence: high
    refresh_on_gather: true
    validated: true
    label: Deploy P50 (hrs)
    result_column: P50
  - id: contoso-perf-baseline
    workstream_ids: [dd_on_pf]
    cluster: https://adventure.kusto.windows.net
    database: xdataanalytics
    kql: Performance | take 1
    section: Performance
    render_as: metric_highlight
    confidence: medium
    refresh_on_gather: true
    validated: true
    label: DD P50 Latency (ms)
    result_column: LatestP50
""".strip(),
        encoding="utf-8",
    )

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme", "One\\Adventure\\Contoso"),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        kusto=KustoConfig(enabled=True),
    )
    workstreams = (
        Workstream(id="acme", name="Acme", area_paths=("One\\Adventure\\Acme",)),
        Workstream(id="dd_on_pf", name="Contoso", area_paths=("One\\Adventure\\Contoso",)),
    )
    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(
        gather,
        "load_program_knowledge",
        lambda program_id, programs_root: KnowledgeStore(
            people_directory=(),
            people_profiles=(),
            teams=(),
            products=(),
            golden_queries=(),
        ),
    )

    def _kpi_executor(query: KustoQuery) -> list[dict[str, object]]:
        if query.id == "acme-deployment-velocity":
            return [{"P50": 4.2, "P90": 7.8, "Timestamp": "2026-05-10T07:00:00Z"}]
        if query.id == "contoso-perf-baseline":
            return [{"LatestP50": 9.5, "Timestamp": "2026-05-10T06:00:00Z"}]
        raise AssertionError(f"Unexpected KPI query id: {query.id}")

    artifacts = gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_kusto=True,
        kusto_query_executor=_kpi_executor,
    )

    signals = read_signals("acme", programs_root=programs_root)
    reviews = read_review_log("acme", programs_root=programs_root)
    gather_state = load_gather_state("acme", programs_root=programs_root)
    kpi_signals = {signal.metadata["query_id"]: signal for signal in signals if signal.source == "kusto_kpi" and signal.metadata is not None}

    assert artifacts.discovered_signals == 2
    assert artifacts.auto_reviews_written == 2
    assert artifacts.pending_review == 0
    assert set(kpi_signals) == {"acme-deployment-velocity", "contoso-perf-baseline"}
    assert kpi_signals["acme-deployment-velocity"].workstream_id == "acme"
    assert kpi_signals["acme-deployment-velocity"].metadata["result_value"] == "4.2"
    assert kpi_signals["contoso-perf-baseline"].workstream_id == "dd_on_pf"
    assert kpi_signals["contoso-perf-baseline"].metadata["result_value"] == "9.5"
    assert len(reviews) == 2
    assert {review.signal_id for review in reviews} == {signal.id for signal in kpi_signals.values()}
    assert gather_state is not None
    assert gather_state.gather_flags["kusto"] is True
    assert gather_state.gather_flags["workiq"] is False
    assert gather_state.channels["kusto"]["signal_count"] == 2
    assert gather_state.channels["workiq"]["active"] is False


def test_gather_program_appends_refresh_on_gather_wiql_kpi_signals(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
schema_version: "1.0"
kpis:
  - id: acme-stg-validation-open
    workstream_ids: [acme]
    program_ids: [acme]
    engine: wiql
    wiql: SELECT [System.Id] FROM WorkItems WHERE [System.IterationPath] = '{current_iteration_path}'
    section: Scenarios / STG Sign-Off
    render_as: metric_highlight
    confidence: medium
    refresh_on_gather: true
    validated: true
    label: STG Validation Open
    result_column: OpenValidationItems
""".strip(),
        encoding="utf-8",
    )

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        kusto=KustoConfig(enabled=True),
    )
    workstreams = (Workstream(id="acme", name="Acme", area_paths=("One\\Adventure\\Acme",)),)
    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(
        gather,
        "load_program_knowledge",
        lambda program_id, programs_root: KnowledgeStore(
            people_directory=(),
            people_profiles=(),
            teams=(),
            products=(),
            golden_queries=(),
        ),
    )

    executed_wiql: list[str] = []

    class _FakeADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def list_team_iterations(self, timeframe: str | None = None, team: str | None = None) -> list[dict[str, object]]:
            assert timeframe == "current"
            assert team is None
            return [{"id": "iteration-24", "path": "One\\Sprint 24"}]

        def execute_wiql(self, wiql: str, top: int | None = None) -> list[int]:
            executed_wiql.append(wiql)
            return [1001, 1002, 1003]

    monkeypatch.setattr(gather, "ADOClient", _FakeADOClient)

    artifacts = gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_kusto=True,
        kusto_query_executor=lambda query: [],
    )

    signals = read_signals("acme", programs_root=programs_root)
    gather_state = load_gather_state("acme", programs_root=programs_root)
    kpi_signals = [signal for signal in signals if signal.source == "kusto_kpi" and signal.metadata is not None]

    assert artifacts.discovered_signals == 1
    assert len(kpi_signals) == 1
    assert kpi_signals[0].metadata["query_id"] == "acme-stg-validation-open"
    assert kpi_signals[0].metadata["engine"] == "wiql"
    assert kpi_signals[0].metadata["result_value"] == "3"
    assert kpi_signals[0].entity_refs == ("WI:1001", "WI:1002", "WI:1003", "WS:acme")
    assert executed_wiql == ["SELECT [System.Id] FROM WorkItems WHERE [System.IterationPath] = 'One\\Sprint 24'"]
    assert gather_state is not None
    assert gather_state.channels["kusto"]["signal_count"] == 1
    query_state = gather_state.query_states["acme-stg-validation-open"]
    assert query_state["last_cycle_succeeded"] is True
    assert query_state["row_count"] == 3
    assert query_state["value_last_4"] == [3.0]


def test_gather_program_appends_refresh_on_gather_wiql_table_kpi_signals(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
schema_version: "1.0"
kpis:
  - id: acme-buildout-pipeline
    workstream_ids: [acme]
    program_ids: [acme]
    engine: wiql
    wiql: SELECT [System.Id] FROM WorkItems WHERE [System.AreaPath] UNDER 'One\\Adventure\\XHealth\\Buildout'
    section: Fleet Health
    render_as: table
    confidence: medium
    refresh_on_gather: true
    validated: true
    label: Buildout Pipeline
""".strip(),
        encoding="utf-8",
    )

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        kusto=KustoConfig(enabled=True),
    )
    workstreams = (Workstream(id="acme", name="Acme", area_paths=("One\\Adventure\\Acme",)),)
    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(
        gather,
        "load_program_knowledge",
        lambda program_id, programs_root: KnowledgeStore(
            people_directory=(),
            people_profiles=(),
            teams=(),
            products=(),
            golden_queries=(),
        ),
    )

    executed_wiql: list[str] = []

    class _FakeADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def execute_wiql(self, wiql: str, top: int | None = None) -> list[int]:
            executed_wiql.append(wiql)
            return [2001, 2002]

        def query_work_items_batch(self, work_item_ids: list[int], fields: tuple[str, ...]) -> list[dict[str, object]]:
            assert work_item_ids == [2001, 2002]
            assert fields == gather._BATCH_FIELDS
            return [
                {
                    "id": 2001,
                    "fields": {
                        "System.Id": 2001,
                        "System.Title": "Dock cluster A",
                        "System.State": "Active",
                        "System.AreaPath": "One\\Adventure\\XHealth\\Buildout",
                        "System.IterationPath": "One\\FY26\\Q4",
                        "System.ChangedDate": "2026-05-10T08:00:00Z",
                        "Microsoft.VSTS.Scheduling.TargetDate": "2026-05-17",
                        "System.Tags": "buildout",
                    },
                },
                {
                    "id": 2002,
                    "fields": {
                        "System.Id": 2002,
                        "System.Title": "Dock cluster B",
                        "System.State": "Committed",
                        "System.AreaPath": "One\\Adventure\\XHealth\\Buildout",
                        "System.IterationPath": "One\\FY26\\Q4",
                        "System.ChangedDate": "2026-05-10T09:00:00Z",
                        "Microsoft.VSTS.Scheduling.TargetDate": "2026-05-18",
                        "System.Tags": "buildout",
                    },
                },
            ]

    monkeypatch.setattr(gather, "ADOClient", _FakeADOClient)

    artifacts = gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_kusto=True,
        kusto_query_executor=lambda query: [],
    )

    signals = read_signals("acme", programs_root=programs_root)
    kpi_signals = [signal for signal in signals if signal.source == "kusto_kpi" and signal.metadata is not None]

    assert artifacts.discovered_signals == 1
    assert len(kpi_signals) == 1
    assert kpi_signals[0].metadata["query_id"] == "acme-buildout-pipeline"
    assert kpi_signals[0].metadata["row_count"] == 2
    assert kpi_signals[0].metadata["result_value"] is None
    assert kpi_signals[0].entity_refs == ("WI:2001", "WI:2002", "WS:acme")
    assert json.loads(kpi_signals[0].metadata["result_json"])["WorkItemId"] == 2001
    assert executed_wiql == ["SELECT [System.Id] FROM WorkItems WHERE [System.AreaPath] UNDER 'One\\Adventure\\XHealth\\Buildout'"]
    gather_state = load_gather_state("acme", programs_root=programs_root)
    assert gather_state is not None
    query_state = gather_state.query_states["acme-buildout-pipeline"]
    assert query_state["last_cycle_succeeded"] is True
    assert query_state["row_count"] == 2
    assert query_state["value_last_4"] == [2.0]


def test_gather_program_appends_refresh_on_gather_ado_pr_kpi_signals(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
schema_version: "1.0"
kpis:
  - id: acme-open-pr-age-p90
    workstream_ids: [acme]
    engine: ado_pr
    section: Deployment Velocity
    render_as: metric_highlight
    confidence: medium
    refresh_on_gather: true
    validated: true
    label: Open PR Age P90 (days)
    result_column: P90AgeDays
""".strip(),
        encoding="utf-8",
    )

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        kusto=KustoConfig(enabled=True),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            ado_repository_ids=("repo-42", "repo-99"),
        ),
    )
    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(
        gather,
        "load_program_knowledge",
        lambda program_id, programs_root: KnowledgeStore(
            people_directory=(),
            people_profiles=(),
            teams=(),
            products=(),
            golden_queries=(),
        ),
    )

    class _FakeADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def list_pull_requests(self, repository_id: str, *, status: str = "active", top: int = 100) -> list[dict[str, object]]:
            assert status == "active"
            assert top == 100
            if repository_id == "repo-42":
                return [
                    {
                        "pullRequestId": 301,
                        "title": "Stabilize rollout for WI:12345",
                        "status": "active",
                        "creationDate": "2026-05-02T08:00:00Z",
                        "repository": {"id": repository_id, "name": "XStoreApp"},
                    },
                    {
                        "pullRequestId": 302,
                        "title": "Tune validation gates",
                        "status": "active",
                        "creationDate": "2026-05-08T08:00:00Z",
                        "repository": {"id": repository_id, "name": "XStoreApp"},
                    },
                ]
            if repository_id == "repo-99":
                return [
                    {
                        "pullRequestId": 401,
                        "title": "Northwind rollout cleanup on bug 45678",
                        "status": "active",
                        "creationDate": "2026-05-10T08:00:00Z",
                        "repository": {"id": repository_id, "name": "PilotfishInfra"},
                    }
                ]
            raise AssertionError(f"Unexpected repository id: {repository_id}")

    monkeypatch.setattr(gather, "ADOClient", _FakeADOClient)

    artifacts = gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_kusto=True,
        kusto_query_executor=lambda query: [],
    )

    signals = read_signals("acme", programs_root=programs_root)
    kpi_signals = [signal for signal in signals if signal.source == "kusto_kpi" and signal.metadata is not None]

    assert artifacts.discovered_signals == 1
    assert len(kpi_signals) == 1
    assert kpi_signals[0].metadata["query_id"] == "acme-open-pr-age-p90"
    assert kpi_signals[0].metadata["engine"] == "ado_pr"
    assert kpi_signals[0].metadata["result_value"] == "10.0"
    assert kpi_signals[0].entity_refs == (
        "PR:XStoreApp/301",
        "WI:12345",
        "PR:XStoreApp/302",
        "PR:PilotfishInfra/401",
        "WI:45678",
        "WS:acme",
    )
    assert "Open PR Age P90 (days): 10.0" in kpi_signals[0].text


def test_gather_program_projects_bound_kusto_kpi_signals_to_observations_idempotently(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    db_root = tmp_path / "vertex-db"
    monkeypatch.setenv("VERTEX_DB_PATH", str(db_root))
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
schema_version: "1.0"
kpis:
  - id: acme-deployment-velocity
    metric_id: acme.deploy_p50_hours
    workstream_ids: [acme]
    cluster: https://adventure.kusto.windows.net
    database: xdataanalytics
    kql: Metrics | take 1
    section: Deployment Velocity
    render_as: metric_highlight
    confidence: high
    refresh_on_gather: true
    validated: true
    label: Deploy P50 (hrs)
    result_column: P50
  - id: contoso-perf-baseline
    metric_id: contoso.latency_p50_ms
    workstream_ids: [dd_on_pf]
    cluster: https://adventure.kusto.windows.net
    database: xdataanalytics
    kql: Performance | take 1
    section: Performance
    render_as: metric_highlight
    confidence: medium
    refresh_on_gather: true
    validated: true
    label: DD P50 Latency (ms)
    result_column: LatestP50
""".strip(),
        encoding="utf-8",
    )

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme", "One\\Adventure\\Contoso"),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        kusto=KustoConfig(enabled=True),
    )
    workstreams = (
        Workstream(id="acme", name="Acme", area_paths=("One\\Adventure\\Acme",)),
        Workstream(id="dd_on_pf", name="Contoso", area_paths=("One\\Adventure\\Contoso",)),
    )
    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(
        gather,
        "load_program_knowledge",
        lambda program_id, programs_root: KnowledgeStore(
            people_directory=(),
            people_profiles=(),
            teams=(),
            products=(),
            golden_queries=(),
        ),
    )

    store = RealityStore("acme", db_root=db_root)
    store.initialize()
    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="acme-deploy-binding",
            metric_id="acme.deploy_p50_hours",
            program_id="acme",
            source_kind="kusto",
            cluster="https://adventure.kusto.windows.net",
            database="xdataanalytics",
            kql_template="Metrics | take 1",
            result_column="P50",
        )
    )
    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="contoso-perf-binding",
            metric_id="contoso.latency_p50_ms",
            program_id="acme",
            source_kind="kusto",
            cluster="https://adventure.kusto.windows.net",
            database="xdataanalytics",
            kql_template="Performance | take 1",
            result_column="LatestP50",
        )
    )

    def _kpi_executor(query: KustoQuery) -> list[dict[str, object]]:
        if query.id == "acme-deployment-velocity":
            return [{"P50": 4.2, "P90": 7.8, "Timestamp": "2026-05-10T07:00:00Z"}]
        if query.id == "contoso-perf-baseline":
            return [{"LatestP50": 9.5, "Timestamp": "2026-05-10T06:00:00Z"}]
        raise AssertionError(f"Unexpected KPI query id: {query.id}")

    gathered_at = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    gather.gather_program(
        "acme",
        as_of=gathered_at,
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_kusto=True,
        kusto_query_executor=_kpi_executor,
    )
    gather.gather_program(
        "acme",
        as_of=gathered_at,
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_kusto=True,
        kusto_query_executor=_kpi_executor,
    )

    observations_nova = store.list_metric_observations("acme.deploy_p50_hours")
    observations_ddpf = store.list_metric_observations("contoso.latency_p50_ms")

    assert len(observations_nova) == 1
    assert observations_nova[0].source_binding_id == "acme-deploy-binding"
    assert observations_nova[0].value_num == 4.2
    assert observations_nova[0].measurement_period_end == datetime(2026, 5, 10, 7, 0, tzinfo=timezone.utc)
    assert len(observations_ddpf) == 1
    assert observations_ddpf[0].source_binding_id == "contoso-perf-binding"
    assert observations_ddpf[0].value_num == 9.5
    assert observations_ddpf[0].measurement_period_end == datetime(2026, 5, 10, 6, 0, tzinfo=timezone.utc)

    with sqlite3.connect(store.db_path) as connection:
        rows = connection.execute(
            "SELECT source_ref, binding_id, status, metrics_observed, signals_written FROM reality_ingestion_runs WHERE source_kind = ? ORDER BY source_ref ASC",
            (SourceKind.KPI_QUERY.value,),
        ).fetchall()

    assert rows == [
        ("acme-deployment-velocity", "acme-deploy-binding", "success", 1, 1),
        ("contoso-perf-baseline", "contoso-perf-binding", "success", 1, 1),
    ]

    with sqlite3.connect(store.db_path) as connection:
        detail_rows = connection.execute(
            "SELECT source_ref, status, signals_written, query_hash, captured_window FROM reality_ingestion_runs WHERE source_kind = ? ORDER BY source_ref ASC",
            (SourceKind.KPI_QUERY.value,),
        ).fetchall()

    assert detail_rows == [
        (
            "acme-deployment-velocity",
            "success",
            1,
            hashlib.sha256("Metrics | take 1".encode("utf-8")).hexdigest(),
            "2026-05-10T07:00:00+00:00/2026-05-10T07:00:00+00:00",
        ),
        (
            "contoso-perf-baseline",
            "success",
            1,
            hashlib.sha256("Performance | take 1".encode("utf-8")).hexdigest(),
            "2026-05-10T06:00:00+00:00/2026-05-10T06:00:00+00:00",
        ),
    ]


def test_gather_program_projects_bound_wiql_kpi_signals_to_observations(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    db_root = tmp_path / "vertex-db"
    monkeypatch.setenv("VERTEX_DB_PATH", str(db_root))
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
schema_version: "1.0"
kpis:
  - id: acme-stg-validation-open
    metric_id: acme.stg_validation_open
    workstream_ids: [acme]
    program_ids: [acme]
    engine: wiql
    wiql: SELECT [System.Id] FROM WorkItems WHERE [System.IterationPath] = '{current_iteration_path}'
    section: Scenarios / STG Sign-Off
    render_as: metric_highlight
    confidence: medium
    refresh_on_gather: true
    validated: true
    label: STG Validation Open
    result_column: OpenValidationItems
""".strip(),
        encoding="utf-8",
    )

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        kusto=KustoConfig(enabled=True),
    )
    workstreams = (Workstream(id="acme", name="Acme", area_paths=("One\\Adventure\\Acme",)),)
    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(
        gather,
        "load_program_knowledge",
        lambda program_id, programs_root: KnowledgeStore(
            people_directory=(),
            people_profiles=(),
            teams=(),
            products=(),
            golden_queries=(),
        ),
    )

    store = RealityStore("acme", db_root=db_root)
    store.initialize()
    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="acme-stg-binding",
            metric_id="acme.stg_validation_open",
            program_id="acme",
            source_kind="wiql",
            cluster="",
            database="",
            kql_template="SELECT [System.Id] FROM WorkItems WHERE [System.IterationPath] = '{current_iteration_path}'",
            result_column="OpenValidationItems",
        )
    )

    executed_wiql: list[str] = []

    class _FakeADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def list_team_iterations(self, timeframe: str | None = None, team: str | None = None) -> list[dict[str, object]]:
            assert timeframe == "current"
            assert team is None
            return [{"id": "iteration-24", "path": "One\\Sprint 24"}]

        def execute_wiql(self, wiql: str, top: int | None = None) -> list[int]:
            executed_wiql.append(wiql)
            return [1001, 1002]

    monkeypatch.setattr(gather, "ADOClient", _FakeADOClient)

    gathered_at = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    gather.gather_program(
        "acme",
        as_of=gathered_at,
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_kusto=True,
        kusto_query_executor=lambda query: [],
    )

    observations = store.list_metric_observations("acme.stg_validation_open")

    assert executed_wiql == ["SELECT [System.Id] FROM WorkItems WHERE [System.IterationPath] = 'One\\Sprint 24'"]
    assert len(observations) == 1
    assert observations[0].source_binding_id == "acme-stg-binding"
    assert observations[0].value_num == 2.0
    assert observations[0].measurement_period_end == gathered_at

    with sqlite3.connect(store.db_path) as connection:
        rows = connection.execute(
            "SELECT source_ref, binding_id, status, metrics_observed, signals_written, query_hash FROM reality_ingestion_runs WHERE source_kind = ? ORDER BY source_ref ASC",
            (SourceKind.KPI_QUERY.value,),
        ).fetchall()

    assert rows == [
        (
            "acme-stg-validation-open",
            "acme-stg-binding",
            "success",
            1,
            1,
            hashlib.sha256("SELECT [System.Id] FROM WorkItems WHERE [System.IterationPath] = '{current_iteration_path}'".encode("utf-8")).hexdigest(),
        ),
    ]


def test_gather_program_projects_admin_provisioned_wiql_kpi_to_observation(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    metrics_root = tmp_path / "metrics"
    db_root = tmp_path / "vertex-db"
    monkeypatch.setenv("VERTEX_DB_PATH", str(db_root))

    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    metrics_root.mkdir(parents=True)
    (metrics_root / "acme.yaml").write_text(
        """
metrics:
  - id: acme.stg_validation_open
    title: STG Validation Open
    unit: count
    aggregation: last
    slo_target: 0
    slo_direction: lte
""".strip(),
        encoding="utf-8",
    )
    (program_dir / "kpis.yaml").write_text(
        """
schema_version: "1.0"
kpis:
  - id: acme-stg-validation-open
    metric_id: acme.stg_validation_open
    assertion_ids: [assertion-acme-stg-validation-open]
    workstream_ids: [acme]
    program_ids: [acme]
    engine: wiql
    wiql: SELECT [System.Id] FROM WorkItems WHERE [System.IterationPath] = '{current_iteration_path}'
    section: Scenarios / STG Sign-Off
    render_as: metric_highlight
    confidence: medium
    refresh_on_gather: true
    validated: true
    label: STG Validation Open
    result_column: OpenValidationItems
""".strip(),
        encoding="utf-8",
    )

    provision_result = runner.invoke(
        app,
        [
            "admin",
            "metric",
            "provision",
            "--program",
            "acme",
            "--query-id",
            "acme-stg-validation-open",
            "--programs-root",
            str(programs_root),
            "--metrics-root",
            str(metrics_root),
            "--db-root",
            str(db_root),
        ],
    )

    assert provision_result.exit_code == 0
    assert "Provisioned query acme-stg-validation-open" in provision_result.stdout

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        kusto=KustoConfig(enabled=True),
    )
    workstreams = (Workstream(id="acme", name="Acme", area_paths=("One\\Adventure\\Acme",)),)
    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(
        gather,
        "load_program_knowledge",
        lambda program_id, programs_root: KnowledgeStore(
            people_directory=(),
            people_profiles=(),
            teams=(),
            products=(),
            golden_queries=(),
        ),
    )

    executed_wiql: list[str] = []

    class _FakeADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def list_team_iterations(self, timeframe: str | None = None, team: str | None = None) -> list[dict[str, object]]:
            assert timeframe == "current"
            assert team is None
            return [{"id": "iteration-24", "path": "One\\Sprint 24"}]

        def execute_wiql(self, wiql: str, top: int | None = None) -> list[int]:
            executed_wiql.append(wiql)
            return [1001, 1002]

    monkeypatch.setattr(gather, "ADOClient", _FakeADOClient)

    gathered_at = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    gather.gather_program(
        "acme",
        as_of=gathered_at,
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_kusto=True,
        kusto_query_executor=lambda query: [],
    )

    store = RealityStore("acme", db_root=db_root)
    store.initialize()
    bindings = store.list_active_metric_source_bindings(metric_id="acme.stg_validation_open")
    assertion = store.get_telemetry_assertion("assertion-acme-stg-validation-open")
    observations = store.list_metric_observations("acme.stg_validation_open")

    assert len(bindings) == 1
    assert assertion is not None
    assert assertion.threshold == 0.0
    assert executed_wiql == ["SELECT [System.Id] FROM WorkItems WHERE [System.IterationPath] = 'One\\Sprint 24'"]
    assert len(observations) == 1
    assert observations[0].source_binding_id == bindings[0].binding_id
    assert observations[0].value_num == 2.0
    assert observations[0].measurement_period_end == gathered_at


def test_gather_program_skips_unvalidated_kusto_candidates_without_probe(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    current_time = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        kusto=KustoConfig(enabled=True),
    )
    knowledge = KnowledgeStore(
        people_directory=(),
        people_profiles=(),
        teams=(),
        products=(),
        golden_queries=(
            KustoQuery(
                id="legacy-golden-query",
                cluster="https://adventure.kusto.windows.net",
                database="xdataanalytics",
                kql="Legacy | take 1",
                section="Legacy",
                render_as="metric_highlight",
                confidence="high",
                program_ids=("acme",),
                validated=True,
            ),
            KustoQuery(
                id="candidate-query",
                cluster="https://adventure.kusto.windows.net",
                database="xdataanalytics",
                kql="Candidate | take 1",
                section="Candidate",
                render_as="metric_highlight",
                confidence="medium",
                program_ids=("acme",),
                validated=False,
            ),
        ),
    )

    executed_query_ids: list[str] = []

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, ()))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(gather, "load_program_knowledge", lambda program_id, programs_root: knowledge)

    def _executor(query: KustoQuery) -> list[dict[str, object]]:
        executed_query_ids.append(query.id)
        return [{"Value": 1}]

    gather.gather_program(
        "acme",
        as_of=current_time,
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_kusto=True,
        probe_kusto=False,
        kusto_query_executor=_executor,
    )

    assert executed_query_ids == ["legacy-golden-query"]


def test_gather_program_includes_unvalidated_kusto_candidates_with_probe_and_persists_query_state(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    current_time = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
schema_version: "1.0"
kpis:
  - id: candidate-kpi
    workstream_ids: [acme]
    cluster: https://adventure.kusto.windows.net
    database: xdataanalytics
    kql: CandidateMetric | take 1
    section: Candidate KPI
    render_as: metric_highlight
    confidence: medium
    refresh_on_gather: true
    validated: false
    result_column: Metric
""".strip(),
        encoding="utf-8",
    )

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        kusto=KustoConfig(enabled=True),
    )
    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, (Workstream(id="acme", name="Acme"),)))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(
        gather,
        "load_program_knowledge",
        lambda program_id, programs_root: KnowledgeStore(
            people_directory=(),
            people_profiles=(),
            teams=(),
            products=(),
            golden_queries=(),
        ),
    )

    artifacts = gather.gather_program(
        "acme",
        as_of=current_time,
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_kusto=True,
        probe_kusto=True,
        kusto_query_executor=lambda query: [{"Metric": 7.5}],
    )

    gather_state_path = programs_root / "acme" / "runtime" / "gather_state.json"
    payload = json.loads(gather_state_path.read_text(encoding="utf-8"))

    assert artifacts.discovered_signals == 1
    assert payload["schema_version"] == "2.0"
    assert payload["queries"]["candidate-kpi"]["row_count"] == 1
    assert payload["queries"]["candidate-kpi"]["last_cycle_succeeded"] is True


def test_gather_program_tracks_kusto_query_freshness_and_frozen_value_history(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    current_time = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
schema_version: "1.0"
kpis:
  - id: frozen-kpi
    workstream_ids: [acme]
    cluster: https://adventure.kusto.windows.net
    database: xdataanalytics
    kql: CandidateMetric | take 1
    section: Frozen KPI
    render_as: metric_highlight
    confidence: medium
    refresh_on_gather: true
    validated: true
    result_column: Metric
""".strip(),
        encoding="utf-8",
    )

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        kusto=KustoConfig(enabled=True),
    )
    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, (Workstream(id="acme", name="Acme"),)))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(
        gather,
        "load_program_knowledge",
        lambda program_id, programs_root: KnowledgeStore(
            people_directory=(),
            people_profiles=(),
            teams=(),
            products=(),
            golden_queries=(),
        ),
    )

    for day in range(4):
        gather.gather_program(
            "acme",
            as_of=current_time + timedelta(days=day),
            programs_root=programs_root,
            loader=lambda program, workstreams, as_of, **_: ((), 0),
            freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
            include_kusto=True,
            probe_kusto=False,
            kusto_query_executor=lambda query: [{"Metric": 7.5, "Timestamp": "2026-05-07T00:00:00Z"}],
        )

    gather_state = load_gather_state("acme", programs_root=programs_root)

    assert gather_state is not None
    query_state = gather_state.query_states["frozen-kpi"]
    assert query_state["expected_max_age_hours"] == 24
    assert query_state["data_freshness_ok"] is False
    assert query_state["value_last_4"] == [7.5, 7.5, 7.5, 7.5]
    assert query_state["value_frozen_warning"] is True
    assert query_state["max_data_timestamp"] == "2026-05-07T00:00:00Z"


def test_load_kusto_queries_keeps_legacy_sweep_when_no_signal_source_scope(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        kusto=KustoConfig(enabled=True),
    )
    knowledge = KnowledgeStore(
        people_directory=(),
        people_profiles=(),
        teams=(),
        products=(),
        golden_queries=(
            KustoQuery(
                id="velocity-p50",
                cluster="https://adventure.kusto.windows.net",
                database="xdataanalytics",
                kql="Metrics | take 1",
                section="Deployment Velocity",
                render_as="metric_highlight",
                confidence="high",
                program_ids=("acme",),
                validated=True,
            ),
            KustoQuery(
                id="fleet-health",
                cluster="https://adventure.kusto.windows.net",
                database="xdataanalytics",
                kql="FleetHealth | take 1",
                section="Fleet Health",
                render_as="table",
                confidence="medium",
                program_ids=("acme",),
                validated=False,
            ),
        ),
    )
    workstreams = (
        Workstream(id="acme", name="Acme"),
        Workstream(id="dd_on_pf", name="Contoso"),
    )

    monkeypatch.setattr(gather, "load_program_knowledge", lambda program_id, programs_root: knowledge)

    queries = gather._load_kusto_queries(
        "acme",
        program=program,
        programs_root=programs_root,
        workstreams=workstreams,
        apply_signal_source_scope=True,
    )

    assert [query.id for query in queries] == ["velocity-p50", "fleet-health"]


def test_gather_program_degrades_gracefully_when_workiq_fails(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    current_time = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={"feedback_search": "Find Acme feedback"},
        ),
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, ()))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(gather, "_build_workiq_signals", lambda *args, **kwargs: (_ for _ in ()).throw(QueryError("workiq unavailable")))

    artifacts = gather.gather_program(
        "acme",
        as_of=current_time,
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: _FakeAgencyBridge(capabilities=AgencyCapabilities(has_workiq=True, available=True), responses={}),
    )

    signals = read_signals("acme", programs_root=programs_root)
    gather_state = load_gather_state("acme", programs_root=programs_root)

    assert artifacts.integration_error_count == 1
    assert len(artifacts.integration_errors) == 1
    assert artifacts.integration_errors[0].source == "workiq"
    assert artifacts.integration_errors[0].operator_action is not None
    assert artifacts.discovered_signals == 1
    assert len(signals) == 1
    assert signals[0].source == "system"
    assert signals[0].metadata is not None
    assert signals[0].metadata["integration_source"] == "workiq"
    assert gather_state is not None
    assert gather_state.integration_errors == 1
    assert len(gather_state.integration_error_details) == 1
    assert gather_state.integration_error_details[0].source == "workiq"


def test_gather_program_persists_workiq_discovery_runtime_failures(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    current_time = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find feedback from Rushi on Acme newsletter drafts",
            },
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
            signal_sources=WorkstreamSignalSources(workiq_keywords=("SCHIE",)),
        ),
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find feedback from Rushi on Acme newsletter drafts. Focus on Acme. Keywords: SCHIE, Acme.": {
                "results": [
                    {
                        "messageId": "thread-1",
                        "threadId": "observed-email-thread-1",
                        "title": "Rushi feedback on WI:1234",
                        "sender": "rushi@example.com",
                        "snippet": "Please update WI:1234 before the next draft.",
                        "timestamp": "2026-05-10T09:00:00Z",
                        "link": "https://outlook.office.com/mail/thread-1",
                    }
                ]
            },
        },
        tool_payloads={"search_emails": {"results": []}},
    )
    original_ask_workiq = bridge.ask_workiq

    def _ask_workiq_with_teams_failure(question: str, **_: object) -> dict[str, object] | None:
        if question.startswith("Use my Microsoft Teams messages in any channel or chat to answer."):
            bridge.questions.append(question)
            bridge._last_mcp_error = "mcp request timed out"
            return None
        bridge._last_mcp_error = None
        return original_ask_workiq(question)

    bridge.ask_workiq = _ask_workiq_with_teams_failure

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    artifacts = gather.gather_program(
        "acme",
        as_of=current_time,
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: bridge,
    )

    gather_state = load_gather_state("acme", programs_root=programs_root)

    assert artifacts.integration_error_count == 1
    assert artifacts.integration_errors[0].source == "workiq"
    assert artifacts.integration_errors[0].stage == "discovery"
    assert artifacts.integration_errors[0].message == "teams discovery failed: mcp request timed out"
    assert gather_state is not None
    assert gather_state.integration_error_details[0].source == "workiq"
    assert gather_state.integration_error_details[0].stage == "discovery"
    assert gather_state.channels["workiq"]["last_error"] == "teams discovery failed: mcp request timed out"


def test_gather_program_persists_previous_run_snapshot(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    current_time = datetime(2026, 5, 11, 8, 0, tzinfo=timezone.utc)

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, ()))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    write_gather_state(
        "acme",
        gathered_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        scanned_items=2,
        discovered_signals=1,
        new_signals=1,
        pending_review=0,
        trajectory_updates=0,
        auto_reviews_written=0,
        ado_calls=1,
        archived_journal_files=0,
        background_proposals=0,
        channels={
            "ado": {"active": True, "signal_count": 4, "expected_min": 1, "meets_expected_min": True},
            "kusto": {"active": True, "signal_count": 10, "expected_min": 10, "meets_expected_min": True},
        },
        m365_discovery={"active": True, "untracked_observed_thread_ids": 1, "signals_without_workstream": 0},
        query_states={"acme-deployment-p50-p90": {"last_cycle_succeeded": False, "data_freshness_ok": False}},
        programs_root=programs_root,
    )

    gather.gather_program(
        "acme",
        as_of=current_time,
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
    )

    gather_state = load_gather_state("acme", programs_root=programs_root)

    assert gather_state is not None
    assert gather_state.previous_gathered_at == datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    assert gather_state.previous_channels["kusto"]["signal_count"] == 10
    assert gather_state.previous_query_states["acme-deployment-p50-p90"]["last_cycle_succeeded"] is False
    assert gather_state.previous_m365_discovery["untracked_observed_thread_ids"] == 1


def test_gather_program_appends_icm_signals_and_auto_reviews_them(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    db_root = tmp_path / "vertex-db"
    monkeypatch.setenv("VERTEX_DB_PATH", str(db_root))

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        kusto=KustoConfig(enabled=True),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    knowledge = KnowledgeStore(
        people_directory=(),
        people_profiles=(),
        teams=(
            Team(
                id="adventure-core",
                name="Adventure Core",
                area_paths=("One\\Adventure\\Acme",),
                programs=("acme",),
            ),
        ),
        products=(),
        golden_queries=(
            KustoQuery(
                id="icm-active",
                cluster="https://icmcluster.kusto.windows.net",
                database="IcMDataWarehouse",
                kql="Incidents | take 1",
                section="Active Incidents",
                render_as="table",
                confidence="high",
                program_ids=("acme",),
                validated=False,
            ),
        ),
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(gather, "load_program_knowledge", lambda program_id, programs_root: knowledge)
    bridge = _FakeIcmBridge(capabilities=AgencyCapabilities(), payload=None)

    artifacts = gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_icm=True,
        bridge_factory=lambda: bridge,
        kusto_query_executor=_fake_kusto_query_executor,
    )

    signals = read_signals("acme", programs_root=programs_root)
    reviews = read_review_log("acme", programs_root=programs_root)
    incidents = read_incident_entries("acme", programs_root=programs_root)

    assert artifacts.discovered_signals == 1
    assert artifacts.new_signals == 1
    assert artifacts.pending_review == 0
    assert artifacts.auto_reviews_written == 1
    assert len(reviews) == 1
    assert len(signals) == 1
    assert len(incidents) == 1
    assert signals[0].source == "icm"
    assert signals[0].workstream_id == "acme"
    assert signals[0].entity_refs == ("ICM:12345", "WI:1234", "WS:acme")
    assert incidents[0].incident_id == "12345"
    assert incidents[0].signal_id == signals[0].id
    assert incidents[0].belief_change_summary == signals[0].text
    assert incidents[0].severity == 2
    assert incidents[0].owning_team == "Adventure Core"
    assert incidents[0].ado_entity_refs == ("WI:1234",)
    assert signals[0].metadata is not None
    assert signals[0].metadata["incident_id"] == "12345"
    assert signals[0].metadata["severity"] == 2
    assert signals[0].metadata["owning_team"] == "Adventure Core"
    assert signals[0].metadata["query_id"] == "icm-active"
    assert signals[0].metadata["signal_class"] == SignalClass.RISK.value
    assert _read_ingestion_run_rows("acme", db_root=db_root) == [
        ("ado/comment", "success", 0),
        ("ado/dependency", "success", 0),
        ("ado/revision", "success", 0),
        ("icm", "success", 1),
        ("vertex/freshness", "success", 0),
    ]


class _FakeIcmBridge:
    def __init__(self, *, capabilities: AgencyCapabilities, payload: dict[str, object] | None) -> None:
        self._capabilities = capabilities
        self._payload = payload
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def probe(self) -> AgencyCapabilities:
        return self._capabilities

    def invoke_mcp_tool(self, server: str, tool: str, args: dict[str, object]) -> dict[str, object] | None:
        self.calls.append((server, tool, args))
        return self._payload


class _FakeDirectIcmClient:
    def __init__(self, *, incidents_url: str | None = None) -> None:
        self.incidents_url = incidents_url

    def list_incidents(self) -> dict[str, object]:
        return {
            "items": [
                {
                    "incidentId": "12345",
                    "severity": 2,
                    "status": "Active",
                    "title": "Fleet capacity alert on WI:1234",
                    "owningTeam": "Adventure Core",
                    "createDate": "2026-05-09T06:00:00Z",
                }
            ]
        }


def test_gather_program_prefers_direct_icm_client_when_configured(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(enabled=True, prefer_agency=True, icm_incidents_url="https://icm.example.test/incidents"),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    knowledge = KnowledgeStore(
        people_directory=(),
        people_profiles=(),
        teams=(
            Team(
                id="adventure-core",
                name="Adventure Core",
                area_paths=("One\\Adventure\\Acme",),
                programs=("acme",),
            ),
        ),
        products=(),
        golden_queries=(),
    )
    bridge = _FakeIcmBridge(
        capabilities=AgencyCapabilities(
            available=True,
            has_icm=True,
            tier="msft",
            server_tools={"icm": ("list_incidents",)},
        ),
        payload={"items": []},
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(gather, "load_program_knowledge", lambda program_id, programs_root: knowledge)

    artifacts = gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_icm=True,
        bridge_factory=lambda: bridge,
        icm_client_factory=_FakeDirectIcmClient,
    )

    signals = read_signals("acme", programs_root=programs_root)

    assert bridge.calls == []
    assert artifacts.discovered_signals == 1
    assert len(signals) == 1
    assert signals[0].entity_refs == ("ICM:12345", "WI:1234", "WS:acme")
    assert signals[0].metadata is not None
    assert signals[0].metadata["incident_id"] == "12345"
    assert signals[0].metadata["severity"] == 2
    assert signals[0].metadata["owning_team"] == "Adventure Core"
    assert signals[0].metadata["source_path"] == "direct"
    assert signals[0].metadata["tool"] == "list_incidents"
    assert signals[0].metadata["signal_class"] == SignalClass.RISK.value


def test_gather_program_prefers_agency_icm_signals_when_list_incidents_is_discovered(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(enabled=True, prefer_agency=True),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    knowledge = KnowledgeStore(
        people_directory=(),
        people_profiles=(),
        teams=(
            Team(
                id="adventure-core",
                name="Adventure Core",
                area_paths=("One\\Adventure\\Acme",),
                programs=("acme",),
            ),
        ),
        products=(),
        golden_queries=(),
    )
    bridge = _FakeIcmBridge(
        capabilities=AgencyCapabilities(
            available=True,
            has_icm=True,
            tier="msft",
            server_tools={"icm": ("list_incidents",)},
        ),
        payload={
            "items": [
                {
                    "incidentId": "12345",
                    "severity": 2,
                    "status": "Active",
                    "title": "Fleet capacity alert on WI:1234",
                    "owningTeam": "Adventure Core",
                    "createDate": "2026-05-09T06:00:00Z",
                }
            ]
        },
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(gather, "load_program_knowledge", lambda program_id, programs_root: knowledge)

    artifacts = gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_icm=True,
        bridge_factory=lambda: bridge,
    )

    signals = read_signals("acme", programs_root=programs_root)
    reviews = read_review_log("acme", programs_root=programs_root)

    assert bridge.calls == [("icm", "list_incidents", {})]
    assert artifacts.discovered_signals == 1
    assert artifacts.new_signals == 1
    assert artifacts.pending_review == 0
    assert artifacts.auto_reviews_written == 1
    assert len(reviews) == 1
    assert len(signals) == 1
    assert signals[0].source == "icm"
    assert signals[0].workstream_id == "acme"
    assert signals[0].entity_refs == ("ICM:12345", "WI:1234", "WS:acme")
    assert signals[0].metadata is not None
    assert signals[0].metadata["incident_id"] == "12345"
    assert signals[0].metadata["severity"] == 2
    assert signals[0].metadata["owning_team"] == "Adventure Core"
    assert signals[0].metadata["source_path"] == "agency"
    assert signals[0].metadata["tool"] == "list_incidents"
    assert signals[0].metadata["signal_class"] == SignalClass.RISK.value


def test_gather_program_skips_icm_when_agency_and_kusto_are_unavailable(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(enabled=True, prefer_agency=True),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    knowledge = KnowledgeStore(
        people_directory=(),
        people_profiles=(),
        teams=(),
        products=(),
        golden_queries=(),
    )
    bridge = _FakeIcmBridge(capabilities=AgencyCapabilities(), payload=None)

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(gather, "load_program_knowledge", lambda program_id, programs_root: knowledge)

    artifacts = gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_icm=True,
        bridge_factory=lambda: bridge,
    )

    assert bridge.calls == []
    assert artifacts.discovered_signals == 0
    assert artifacts.new_signals == 0
    assert artifacts.pending_review == 0
    assert artifacts.auto_reviews_written == 0
    assert read_signals("acme", programs_root=programs_root) == ()
class _FakeWorkIQBridge:
    def __init__(
        self,
        *,
        responses: dict[str, dict[str, object] | None],
        tool_payloads: dict[str, dict[str, object] | None] | None = None,
        tool_errors: dict[str, str] | None = None,
    ) -> None:
        self._responses = responses
        self._tool_payloads = tool_payloads or {}
        self._tool_errors = tool_errors or {}
        self.questions: list[str] = []
        self.tool_calls: list[tuple[str, str, dict[str, object]]] = []
        self._last_mcp_error: str | None = None

    def probe(self) -> AgencyCapabilities:
        return AgencyCapabilities(available=True, has_workiq=True, tier="msft")

    def ask_workiq(self, question: str, *, timeout_seconds: int | None = None) -> dict[str, object] | None:
        self.questions.append(question)
        if question in self._responses:
            return self._responses[question]
        # Per-workstream scoping (discover.md §8.5) appends ". Focus on <ws>.
        # Keywords: ..." to the authored base question; adaptive learning can also
        # vary the trailing keyword list. Match on the authored base (everything
        # before ". Focus on") so fixtures stay robust to scoping + expansion.
        def _base(value: str) -> str:
            return value.split(". Focus on", 1)[0].strip()

        question_base = _base(question)
        for key, value in self._responses.items():
            if key and (_base(key) == question_base or question.startswith(key)):
                return value
        return None

    def invoke_mcp_tool(
        self,
        server: str,
        tool: str,
        args: dict[str, object],
        timeout_seconds: int | None = None,
    ) -> dict[str, object] | None:
        del timeout_seconds
        self.tool_calls.append((server, tool, args))
        self._last_mcp_error = self._tool_errors.get(tool)
        if self._last_mcp_error is not None:
            return None
        return self._tool_payloads.get(tool)

    def last_mcp_error(self) -> str | None:
        return self._last_mcp_error


def _fake_kusto_query_executor(query: KustoQuery) -> list[dict[str, object]]:
    if query.id == "velocity-p50":
        return [{"Timestamp": "2026-05-10T07:00:00Z", "P50Hours": 4.2, "P90Hours": 7.8}]
    if query.id == "fleet-health":
        return [{"Date": "2026-05-09", "HealthyPct": 98.4, "Nodes": 1215}]
    if query.id == "icm-active":
        return [{"CreateDate": "2026-05-09T06:00:00Z", "IncidentId": "ICM-12345", "Severity": 2, "OwningTeamName": "Adventure Core", "Title": "Fleet capacity alert on WI:1234", "Status": "Active"}]
    if query.id == "bios-ap-shared-service-pct":
        return [{"IsGoodStorageTotal": 95.0, "IsGoodStorageGen7": 92.0, "IsGoodStorageGen8": 96.0, "IsGoodStorageGen9": 98.0}]
    if query.id == "wingtip-fleet-rollout-pct":
        return [{"RolloutPct": 88.5}]
    raise AssertionError(f"Unexpected Kusto query id: {query.id}")


def test_gather_program_runs_background_synthesis_for_triggered_workstream(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr(gather, "PROGRAMS_ROOT", programs_root)

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        ai=AIConfig(
            enabled=True,
            budget_usd_per_run=0.5,
            blurb_deployment="fake",
            exec_summary_deployment="fake",
            temperature=0.2,
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    item = WorkItem(
        id=1234,
        type="Feature",
        title="Ramp checkpoint",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="One\\FY26\\Q4",
        target_date=date(2026, 5, 17),
        risk_level=RiskLevel.MEDIUM,
        tags=["acme"],
        custom_fields={},
        revisions=[],
        comments=[],
        fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_resolve_uil_channel_binding_for_gather", lambda *_a, **_k: object())
    monkeypatch.setattr(gather, "_load_ado_items_via_uil", lambda _p, _ws, _ao, **__: ((item,), (item,), 0))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    from src.commands.gather_pipeline.models import BackgroundSynthesisTrigger

    monkeypatch.setattr(
        "src.commands.gather_pipeline.projection_stage.evaluate_background_synthesis_triggers",
        lambda *args, **kwargs: (BackgroundSynthesisTrigger(workstream_id="acme", reasons=("leakage ratio 1.00 with 2 ETA slips",)),),
    )

    calls: list[tuple[str, str]] = []
    artifacts = gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        background_synthesis_runner=lambda program_id, workstream_id, programs_root, as_of: calls.append((program_id, workstream_id)) or True,
    )

    assert artifacts.background_proposals == 1
    assert calls == [("acme", "acme")]


def test_gather_program_appends_ai_extracted_actions_when_ai_is_enabled(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    current_time = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={
                "feedback_search": "Find feedback from Rushi on Acme newsletter drafts",
            },
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
            signal_sources=WorkstreamSignalSources(workiq_keywords=("SCHIE",)),
        ),
    )
    bridge = _FakeWorkIQBridge(
        responses={
            "Find feedback from Rushi on Acme newsletter drafts. Focus on Acme. Keywords: SCHIE, Acme.": {
                "results": [
                    {
                        "messageId": "thread-1",
                        "threadId": "observed-email-thread-1",
                        "title": "Rushi feedback on WI:1234",
                        "sender": "rushi@example.com",
                        "snippet": "Please update WI:1234 before the next draft.",
                        "timestamp": "2026-05-10T09:00:00Z",
                        "link": "https://outlook.office.com/mail/thread-1",
                    }
                ]
            },
        },
        tool_payloads={"search_emails": {"results": []}},
    )
    original_ask_workiq = bridge.ask_workiq

    def _ask_workiq_with_teams_failure(question: str, **_: object) -> dict[str, object] | None:
        if question.startswith("Use my Microsoft Teams messages in any channel or chat to answer."):
            bridge.questions.append(question)
            bridge._last_mcp_error = "mcp request timed out"
            return None
        bridge._last_mcp_error = None
        return original_ask_workiq(question)

    bridge.ask_workiq = _ask_workiq_with_teams_failure

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    artifacts = gather.gather_program(
        "acme",
        as_of=current_time,
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((), 0),
        freshness_loader=lambda program, workstreams, as_of, **_: ((), 0),
        include_workiq=True,
        bridge_factory=lambda: bridge,
    )

    gather_state = load_gather_state("acme", programs_root=programs_root)

    assert artifacts.integration_error_count == 1
    assert artifacts.integration_errors[0].source == "workiq"
    assert artifacts.integration_errors[0].stage == "discovery"
    assert artifacts.integration_errors[0].message == "teams discovery failed: mcp request timed out"
    assert gather_state is not None
    assert gather_state.integration_error_details[0].source == "workiq"
    assert gather_state.integration_error_details[0].stage == "discovery"
    assert gather_state.channels["workiq"]["last_error"] == "teams discovery failed: mcp request timed out"

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        ai=AIConfig(
            enabled=True,
            budget_usd_per_run=0.5,
            blurb_deployment="fake",
            exec_summary_deployment="fake",
            temperature=0.2,
        ),
    )
    workstreams = (
        Workstream(id="acme", name="Acme", area_paths=("One\\Adventure\\Acme",), dri_email="maintainer@example.com"),
    )
    signal = Signal(
        id="sig-teams-1",
        timestamp=current_time,
        source="workiq/teams",
        program_id="acme",
        workstream_id="acme",
        entity_refs=("WI:1234",),
        text="Priya will follow up on the ramp packet by 2026-05-20.",
        raw_ref=None,
        confidence=Confidence.MEDIUM,
        metadata={"sender_alias": "alex"},
        thread_id="thread-1",
    )
    item = WorkItem(
        id=1234,
        type="Feature",
        title="Ramp checkpoint",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="One\\FY26\\Q4",
        target_date=date(2026, 5, 17),
        risk_level=RiskLevel.MEDIUM,
        tags=["acme"],
        custom_fields={},
        revisions=[],
        comments=[],
        fetched_at=current_time,
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_resolve_uil_channel_binding_for_gather", lambda *_a, **_k: object())
    monkeypatch.setattr(gather, "_load_ado_items_via_uil", lambda _p, _ws, _ao, **__: ((item,), (item,), 0))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(gather, "_build_ado_revision_signals", lambda *args, **kwargs: ())
    monkeypatch.setattr(gather, "_build_freshness_signals", lambda *args, **kwargs: ())
    monkeypatch.setattr(gather, "_build_workiq_signals", lambda *args, **kwargs: (signal,))
    monkeypatch.setattr(
        "src.commands.gather_pipeline.persistence_stage.extract_actions_from_signals",
        lambda signals, program_id: (),
    )
    monkeypatch.setattr(
        "src.commands.gather_pipeline.projection_stage.evaluate_background_synthesis_triggers",
        lambda *args, **kwargs: (),
    )

    artifacts = gather.gather_program(
        "acme",
        as_of=current_time,
        programs_root=programs_root,
        include_workiq=True,
        ai_action_extractor=lambda program, signals: (
            ActionItem(
                id="ai-action-1",
                program_id=program.id,
                text="Follow up on the ramp packet",
                owner_alias="priya",
                due_date=date(2026, 5, 20),
                status=ActionStatus.PROPOSED,
                source_signal_id=signals[0].id,
                source_type=ActionSourceType.MEETING_TRANSCRIPT,
                linked_work_item_ids=(1234,),
                linked_claim_id=None,
                linked_risk_id=None,
                workstream_id="acme",
                created_at=signals[0].timestamp,
                resolved_at=None,
                resolution_note=None,
            ),
        ),
    )

    actions = load_actions("acme", programs_root=programs_root)

    assert artifacts.new_signals == 1
    assert len(actions) == 1
    assert actions[0].id == "ai-action-1"
    assert actions[0].source_type is ActionSourceType.MEETING_TRANSCRIPT


def test_gather_program_skips_ai_action_extractor_when_ai_is_disabled(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    current_time = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(id="acme", name="Acme", area_paths=("One\\Adventure\\Acme",), dri_email="maintainer@example.com"),
    )
    item = WorkItem(
        id=1234,
        type="Feature",
        title="Ramp checkpoint",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="One\\FY26\\Q4",
        target_date=date(2026, 5, 17),
        risk_level=RiskLevel.MEDIUM,
        tags=["acme"],
        custom_fields={},
        revisions=[],
        comments=[],
        fetched_at=current_time,
    )

    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_resolve_uil_channel_binding_for_gather", lambda *_a, **_k: object())
    monkeypatch.setattr(gather, "_load_ado_items_via_uil", lambda _p, _ws, _ao, **__: ((item,), (item,), 0))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(gather, "_build_ado_revision_signals", lambda *args, **kwargs: ())
    monkeypatch.setattr(gather, "_build_freshness_signals", lambda *args, **kwargs: ())
    monkeypatch.setattr(
        "src.commands.gather_pipeline.persistence_stage.extract_actions_from_signals",
        lambda signals, program_id: (),
    )

    artifacts = gather.gather_program(
        "acme",
        as_of=current_time,
        programs_root=programs_root,
        ai_action_extractor=lambda program, signals: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    actions = load_actions("acme", programs_root=programs_root)

    assert artifacts.new_signals == 0
    assert actions == ()


def test_evaluate_background_synthesis_triggers_detects_leakage_and_eta_slips(monkeypatch, tmp_path: Path) -> None:
    from src.commands.gather_pipeline.projection_stage import evaluate_background_synthesis_triggers

    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text("schema_version: '2.0'\nid: acme\nname: Acme\n", encoding="utf-8")

    program = Program(schema_version="2.0", id="acme", name="Acme")
    workstreams = (
        Workstream(id="acme", name="Acme", area_paths=("One\\Adventure\\Acme",), dri_email="maintainer@example.com"),
    )
    item = WorkItem(
        id=1234,
        type="Feature",
        title="Ramp checkpoint",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="One\\FY26\\Q4",
        target_date=date(2026, 5, 17),
        risk_level=RiskLevel.MEDIUM,
        tags=["acme"],
        custom_fields={},
        revisions=[],
        comments=[],
        fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    monkeypatch.setattr(
        "src.commands.gather_pipeline.projection_stage.load_item_trajectories",
        lambda program_id, items, programs_root, storage_backend="file": {
            1234: (
                TrajectoryPoint(date=date(2026, 5, 1), state="Active", assigned_to="Priya", target_date=date(2026, 5, 10), risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme"),
                TrajectoryPoint(date=date(2026, 5, 3), state="Active", assigned_to="Priya", target_date=date(2026, 5, 12), risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme"),
                TrajectoryPoint(date=date(2026, 5, 5), state="Active", assigned_to="Priya", target_date=date(2026, 5, 15), risk_level=RiskLevel.HIGH, area_path="One\\Adventure\\Acme"),
            )
        },
    )
    monkeypatch.setattr(
        "src.commands.gather_pipeline.projection_stage.detect_background_leakage",
        lambda *args, **kwargs: LeakageReport(events=(), signal_counts_by_item={1234: 2}, leakage_counts_by_item={1234: 2}, owner_leakage_ratios={}),
    )
    monkeypatch.setattr(
        "src.commands.gather_pipeline.projection_stage.generate_background_vitality_artifacts",
        lambda *args, **kwargs: SimpleNamespace(scored_items=(), workstream_aggregates=()),
    )
    monkeypatch.setattr(
        "src.commands.gather_pipeline.projection_stage.load_recent_workstream_vitality_scores",
        lambda *args, **kwargs: {},
    )

    triggers = evaluate_background_synthesis_triggers(
        "acme",
        program=program,
        workstreams=workstreams,
        items=(item,),
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        resolve_workstream_id=lambda area_path, workstreams: "acme",
        trajectories_by_item={
            1234: (
                TrajectoryPoint(date=date(2026, 5, 1), state="Active", assigned_to="Priya", target_date=date(2026, 5, 10), risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme"),
                TrajectoryPoint(date=date(2026, 5, 3), state="Active", assigned_to="Priya", target_date=date(2026, 5, 12), risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme"),
                TrajectoryPoint(date=date(2026, 5, 5), state="Active", assigned_to="Priya", target_date=date(2026, 5, 15), risk_level=RiskLevel.HIGH, area_path="One\\Adventure\\Acme"),
            )
        },
    )

    assert len(triggers) == 1
    assert triggers[0].workstream_id == "acme"
    assert any("leakage ratio" in reason for reason in triggers[0].reasons)


def test_has_sustained_low_vitality_without_history_when_no_items_are_fresh() -> None:
    aggregate = VitalityAggregate(
        scope_id="acme",
        scope_type="workstream",
        total_items=2,
        fresh_items=0,
        avg_richness=25.0,
        total_leakage=0,
        workiq_signal_count=0,
        leakage_ratio=0.0,
        composite_score=35,
    )
    scores = (
        VitalityScore(
            work_item_id=1,
            owner_alias="priya",
            workstream_id="acme",
            freshness_days=21,
            freshness_grade="red",
            richness_score=20,
            richness_missing=("recent_comment",),
            leakage_events=0,
            workiq_signal_count=0,
            composite_score=35,
            suggested_update=None,
        ),
        VitalityScore(
            work_item_id=2,
            owner_alias="alex",
            workstream_id="acme",
            freshness_days=18,
            freshness_grade="red",
            richness_score=30,
            richness_missing=("recent_comment",),
            leakage_events=0,
            workiq_signal_count=0,
            composite_score=35,
            suggested_update=None,
        ),
    )

    from src.commands.gather_pipeline.projection_stage import has_sustained_low_vitality

    assert has_sustained_low_vitality(
        "acme",
        aggregate,
        scores=scores,
        recent_history_scores={},
        threshold=40,
        stale_days=14,
    )



# ---------------------------------------------------------------------------
# eng.ms secondary extractor wiring (G12)
# ---------------------------------------------------------------------------

def _engms_work_item(item_id: int, url: str) -> WorkItem:
    return WorkItem(
        id=item_id,
        type="Feature",
        title="Item",
        state="Active",
        assigned_to=None,
        assigned_to_email=None,
        area_path="One\\Demo",
        iteration_path="One\\Demo\\Sprint",
        target_date=None,
        risk_level=RiskLevel.UNKNOWN,
        tags=[],
        custom_fields={"System.Description": f"See {url} for details."},
        fetched_at=datetime(2026, 5, 29, tzinfo=timezone.utc),
    )


def test_build_engms_signals_emits_then_skips_unchanged_on_run_two(monkeypatch) -> None:
    url = "https://eng.ms/docs/acme/spec"
    items = (_engms_work_item(1, url),)
    monkeypatch.setattr(
        "src.core.engms_signal_extractor.fetch_engms_page_summary",
        lambda u: "Stable page content",
    )

    errors: list = []
    # Run 1: nothing persisted yet -> a new-reference signal is emitted.
    signals_1, hash_state_1 = gather._build_engms_signals(
        items=items,
        program_id="acme",
        previous_query_states={},
        integration_error_sink=errors,
    )
    assert len(signals_1) == 1
    assert signals_1[0].source == "engms"
    assert any(k.startswith("engms_hash:") for k in hash_state_1)
    assert errors == []

    # Run 2: feed the persisted hashes back -> unchanged page yields no signal.
    signals_2, hash_state_2 = gather._build_engms_signals(
        items=items,
        program_id="acme",
        previous_query_states={"engms": hash_state_1},
        integration_error_sink=errors,
    )
    assert signals_2 == ()
    assert hash_state_2 == hash_state_1
    assert errors == []


def test_build_engms_signals_records_error_and_preserves_hashes_on_failure(monkeypatch) -> None:
    class _BoomExtractor:
        def extract(self, *args, **kwargs):
            raise RuntimeError("boom")

    errors: list = []
    prior = {"engms": {"engms_hash:https://eng.ms/a": "deadbeefdeadbeef"}}
    signals, hash_state = gather._build_engms_signals(
        items=(),
        program_id="acme",
        previous_query_states=prior,
        extractor=_BoomExtractor(),
        integration_error_sink=errors,
    )
    assert signals == ()
    # Prior hashes are preserved so a transient failure does not re-emit every signal next run.
    assert hash_state == prior["engms"]
    assert len(errors) == 1
    assert errors[0].source == "engms"


def test_build_engms_signals_preserves_prior_hashes_for_unfetched_urls(monkeypatch) -> None:
    # URL-A was hashed on a previous run but is not referenced by any work item this run,
    # so it is never fetched. Its prior hash must survive the merge (else it would re-emit
    # as a "new reference" the next time it is fetched).
    url_b = "https://eng.ms/docs/b"
    items = (_engms_work_item(2, url_b),)
    monkeypatch.setattr(
        "src.core.engms_signal_extractor.fetch_engms_page_summary",
        lambda u: "Fresh content for B",
    )

    prior = {"engms": {"engms_hash:https://eng.ms/docs/a": "aaaaaaaaaaaaaaaa"}}
    signals, hash_state = gather._build_engms_signals(
        items=items,
        program_id="acme",
        previous_query_states=prior,
        integration_error_sink=[],
    )
    assert len(signals) == 1  # URL-B is new -> one signal
    # URL-A's prior hash is preserved AND URL-B's fresh hash is recorded.
    assert hash_state["engms_hash:https://eng.ms/docs/a"] == "aaaaaaaaaaaaaaaa"
    assert any(k.startswith("engms_hash:https://eng.ms/docs/b") for k in hash_state)


def test_gather_command_engms_flag_threaded(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_gather_program(*args, **kwargs):
        captured["kwargs"] = kwargs
        return gather.GatherArtifacts(
            program_id="acme",
            scanned_items=0,
            discovered_signals=0,
            new_signals=0,
            pending_review=0,
            trajectory_updates=0,
            auto_reviews_written=0,
            ado_calls=0,
        )

    monkeypatch.setattr(gather, "gather_program", _fake_gather_program)

    result = runner.invoke(app, ["gather", "--program", "acme", "--engms"])

    assert result.exit_code == 0
    assert captured["kwargs"]["include_engms"] is True


# ---------------------------------------------------------------------------
# WorkIQ query-plan: email_subject_filters (G10/G13 non-ADO email coverage)
# ---------------------------------------------------------------------------

def _m365_query_plan_program() -> Program:
    return Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(enabled=True, prefer_agency=True),
    )


def test_query_plans_email_subject_filters_emit_when_no_threads_or_keywords() -> None:
    program = _m365_query_plan_program()
    workstreams = (
        Workstream(
            id="ws1",
            name="Ramp",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(
                email_subject_filters=("Northwind ramp", "SCHIE blocker"),
            ),
        ),
    )
    plans = gather._build_workiq_query_plans(program=program, workstreams=workstreams)
    subject_plans = [p for p in plans if p.query_name == "subject_filter:ws1"]
    assert len(subject_plans) == 1
    assert subject_plans[0].mcp_tool == "search_emails"
    assert subject_plans[0].tool_args["query"] == '"Northwind ramp" OR "SCHIE blocker"'


def test_query_plans_structured_discovery_uses_fixed_window_and_union_runs() -> None:
    base = _m365_query_plan_program()
    program = replace(
        base,
        m365=replace(
            base.m365,
            retrieval=WorkIQRetrievalConfig(
                discovery_mode="structured_json",
                discovery_union_runs=3,
                discovery_lookback_days=14,
            ),
        ),
    )
    workstreams = (
        Workstream(
            id="ws1",
            name="Ramp",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(workiq_keywords=("Northwind", "launch readiness")),
        ),
    )

    plans = gather._build_workiq_query_plans(
        program=program,
        workstreams=workstreams,
        as_of=datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc),
    )
    broad = [plan for plan in plans if plan.mcp_tool is None]

    assert len(broad) == 3
    assert len({plan.question for plan in broad}) == 1
    assert "between 2026-06-06 and 2026-06-20" in (broad[0].question or "")
    assert broad[0].structured_result_limit == 8
    assert [plan.bypass_ask_cache for plan in broad] == [False, True, True]


def test_query_plans_structured_discovery_lane_override_precedes_program_config() -> None:
    base = _m365_query_plan_program()
    program = replace(
        base,
        m365=replace(base.m365, retrieval=WorkIQRetrievalConfig(discovery_mode="structured_json", discovery_union_runs=4)),
    )
    workstreams = (
        Workstream(
            id="ws1",
            name="Ramp",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(
                workiq_keywords=("Northwind",),
                workiq_discovery_mode="legacy_nl",
            ),
        ),
    )

    plans = gather._build_workiq_query_plans(program=program, workstreams=workstreams)
    broad = [plan for plan in plans if plan.mcp_tool is None]

    assert len(broad) == 1
    assert broad[0].structured_window_start is None
    assert "Return JSON only" not in (broad[0].question or "")


def test_build_workiq_signals_structured_discovery_validates_and_unions_results() -> None:
    base = _m365_query_plan_program()
    program = replace(
        base,
        m365=replace(
            base.m365,
            retrieval=WorkIQRetrievalConfig(discovery_mode="structured_json", discovery_union_runs=2),
        ),
    )
    workstreams = (
        Workstream(
            id="ws1",
            name="Ramp",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(workiq_keywords=("Northwind",)),
        ),
    )

    class StructuredBridge:
        def __init__(self) -> None:
            self.questions: list[str] = []

        def probe(self) -> AgencyCapabilities:
            return AgencyCapabilities(available=True, has_workiq=True, tier="msft")

        def ask_workiq(self, question: str, **_: object) -> dict[str, object]:
            self.questions.append(question)
            return {
                "emails": [
                    {
                        "id": "mail-1",
                        "conversationId": "thread-1",
                        "subject": "Northwind ramp",
                        "bodyPreview": "Launch readiness is green.",
                        "receivedDateTime": "2026-06-10T08:00:00Z",
                        "webUrl": "https://outlook.office.com/mail/deeplink/read/1",
                    },
                    {
                        "id": "mail-2",
                        "conversationId": "thread-2",
                        "subject": "Rejected host",
                        "receivedDateTime": "2026-06-10T08:00:00Z",
                        "webUrl": "https://example.invalid/mail/2",
                    },
                ]
            }

    bridge = StructuredBridge()
    signals = gather._build_workiq_signals(
        program=program,
        program_id="acme",
        as_of=datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc),
        items=(),
        workstreams=workstreams,
        bridge=bridge,
    )

    assert len(bridge.questions) == 2
    assert bridge.questions[0] == bridge.questions[1]
    assert len(signals) == 1
    assert signals[0].raw_ref == "workiq:email:mail-1"


def test_query_plans_email_subject_filters_coexist_with_keywords() -> None:
    program = _m365_query_plan_program()
    workstreams = (
        Workstream(
            id="ws1",
            name="Ramp",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(
                workiq_keywords=("SCHIE",),
                email_subject_filters=("Northwind ramp",),
            ),
        ),
    )
    plans = gather._build_workiq_query_plans(program=program, workstreams=workstreams)
    names = {p.query_name for p in plans}
    # Keywords drive a feedback search AND the explicit email source still fires.
    assert "subject_filter:ws1" in names
    assert any(n.startswith("feedback_search:ws1") for n in names)


def test_query_plans_email_threads_supersede_subject_filters() -> None:
    program = _m365_query_plan_program()
    workstreams = (
        Workstream(
            id="ws1",
            name="Ramp",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(
                email_subject_filters=("Northwind ramp",),
                email_threads=(EmailThreadSource(display_name="Ramp Thread", thread_id="thread-1"),),
            ),
        ),
    )
    plans = gather._build_workiq_query_plans(program=program, workstreams=workstreams)
    names = {p.query_name for p in plans}
    # Precise thread coverage suppresses the broad subject search (no double-count).
    assert "subject_filter:ws1" not in names
    assert "email_thread:ws1:thread-1" in names


def test_query_plans_blank_subject_filters_emit_no_email_plan() -> None:
    program = _m365_query_plan_program()
    workstreams = (
        Workstream(
            id="ws1",
            name="Ramp",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(email_subject_filters=("  ", "")),
        ),
    )
    plans = gather._build_workiq_query_plans(program=program, workstreams=workstreams)
    assert not [p for p in plans if p.query_name == "subject_filter:ws1"]


def test_query_plans_teams_chats_emit_targeted_message_searches() -> None:
    program = _m365_query_plan_program()
    workstreams = (
        Workstream(
            id="ws1",
            name="Ramp",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(
                workiq_keywords=("SCHIE",),
                teams_chats=(TeamsChat(display_name="Ramp Chat", thread_id="thread-1"),),
            ),
        ),
    )
    plans = gather._build_workiq_query_plans(program=program, workstreams=workstreams)
    chat_plans = [p for p in plans if p.query_name == "teams_chat:ws1:thread-1"]
    assert len(chat_plans) == 1
    assert chat_plans[0].mcp_tool == "teams_chat"
    assert chat_plans[0].tool_args == {"channel": "all", "query": '"Ramp Chat" OR thread-1 OR SCHIE', "limit": 50}
    assert chat_plans[0].allowed_thread_ids == ("thread-1",)


def test_query_plans_meeting_series_emit_targeted_calendar_searches() -> None:
    program = _m365_query_plan_program()
    workstreams = (
        Workstream(
            id="ws1",
            name="Ramp",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(
                workiq_keywords=("SCHIE",),
                teams_meeting_series=(TeamsMeetingSeries(display_name="Ramp Weekly", series_id="meeting-1"),),
            ),
        ),
    )
    plans = gather._build_workiq_query_plans(program=program, workstreams=workstreams)
    meeting_plans = [p for p in plans if p.query_name == "meeting_series:ws1:meeting-1"]
    assert len(meeting_plans) == 1
    assert meeting_plans[0].mcp_tool == "calendar_gather"
    assert meeting_plans[0].tool_args == {"query": '"Ramp Weekly" OR meeting-1 OR SCHIE', "limit": 50}
    assert meeting_plans[0].allowed_thread_ids == ("meeting-1",)
    assert meeting_plans[0].include_transcripts is True


def test_query_plans_meeting_series_preserve_transcript_opt_out() -> None:
    program = _m365_query_plan_program()
    workstreams = (
        Workstream(
            id="ws1",
            name="Ramp",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(
                workiq_keywords=("SCHIE",),
                teams_meeting_series=(
                    TeamsMeetingSeries(
                        display_name="Ramp Weekly",
                        series_id="meeting-1",
                        include_transcripts=False,
                    ),
                ),
            ),
        ),
    )
    plans = gather._build_workiq_query_plans(program=program, workstreams=workstreams)
    meeting_plan = next(p for p in plans if p.query_name == "meeting_series:ws1:meeting-1")
    assert meeting_plan.include_transcripts is False


def test_query_plans_registry_artifacts_emit_targeted_plans_when_unconfigured() -> None:
    program = _m365_query_plan_program()
    workstreams = (
        Workstream(
            id="ws1",
            name="Ramp",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(workiq_keywords=("SCHIE",)),
        ),
    )
    registry_artifacts = (
        M365RegistryArtifact(
            artifact_id="thread:named:acme-ramp-thread",
            artifact_type="email_thread",
            inferred_workstream="ws1",
            confidence=0.92,
            confidence_source="router",
            pm_confirmed=False,
            promoted_to_workstreams_yaml=False,
            first_seen=date(2026, 5, 1),
            last_seen=date(2026, 5, 10),
            high_confidence_streak=3,
            display_name="Ramp Thread",
            thread_id="thread-1",
            routing_reasoning="Previously routed to Ramp.",
        ),
        M365RegistryArtifact(
            artifact_id="chan:acme-ramp-chat",
            artifact_type="teams_channel",
            inferred_workstream="ws1",
            confidence=0.95,
            confidence_source="router",
            pm_confirmed=False,
            promoted_to_workstreams_yaml=False,
            first_seen=date(2026, 5, 1),
            last_seen=date(2026, 5, 10),
            high_confidence_streak=3,
            display_name="Ramp Chat",
            thread_id="chat-1",
            routing_reasoning="Previously routed to Ramp.",
        ),
        M365RegistryArtifact(
            artifact_id="meet:acme-ramp-weekly",
            artifact_type="meeting_series",
            inferred_workstream="ws1",
            confidence=0.9,
            confidence_source="router",
            pm_confirmed=False,
            promoted_to_workstreams_yaml=False,
            first_seen=date(2026, 5, 1),
            last_seen=date(2026, 5, 10),
            high_confidence_streak=3,
            display_name="Ramp Weekly",
            series_id="meeting-1",
            routing_reasoning="Previously routed to Ramp.",
        ),
    )

    plans = gather._build_workiq_query_plans(
        program=program,
        workstreams=workstreams,
        registry_artifacts=registry_artifacts,
    )

    assert "email_thread:ws1:thread-1" in {p.query_name for p in plans}
    assert "teams_chat:ws1:chat-1" in {p.query_name for p in plans}
    assert "meeting_series:ws1:meeting-1" in {p.query_name for p in plans}
    meeting_plan = next(p for p in plans if p.query_name == "meeting_series:ws1:meeting-1")
    assert meeting_plan.include_transcripts is True


def test_query_plans_registry_artifacts_skip_unsustained_high_confidence_candidates() -> None:
    program = _m365_query_plan_program()
    workstreams = (
        Workstream(
            id="ws1",
            name="Ramp",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(workiq_keywords=("SCHIE",)),
        ),
    )
    registry_artifacts = (
        M365RegistryArtifact(
            artifact_id="thread:named:acme-ramp-thread",
            artifact_type="email_thread",
            inferred_workstream="ws1",
            confidence=0.92,
            confidence_source="router",
            pm_confirmed=False,
            promoted_to_workstreams_yaml=False,
            first_seen=date(2026, 5, 1),
            last_seen=date(2026, 5, 10),
            high_confidence_streak=0,
            display_name="Ramp Thread",
            thread_id="thread-1",
            routing_reasoning="Previously routed to Ramp.",
        ),
    )

    plans = gather._build_workiq_query_plans(
        program=program,
        workstreams=workstreams,
        registry_artifacts=registry_artifacts,
    )

    assert "email_thread:ws1:thread-1" not in {p.query_name for p in plans}


def test_query_plans_registry_artifacts_skip_recently_rejected_candidates() -> None:
    program = _m365_query_plan_program()
    workstreams = (
        Workstream(
            id="ws1",
            name="Ramp",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(workiq_keywords=("SCHIE",)),
        ),
    )
    registry_artifacts = (
        M365RegistryArtifact(
            artifact_id="thread:named:acme-ramp-thread",
            artifact_type="email_thread",
            inferred_workstream="ws1",
            confidence=1.0,
            confidence_source="pm_confirmed",
            pm_confirmed=True,
            promoted_to_workstreams_yaml=False,
            first_seen=date(2026, 5, 1),
            last_seen=date(2026, 5, 10),
            display_name="Ramp Thread",
            thread_id="thread-1",
            routing_reasoning="Previously routed to Ramp.",
        ),
    )
    feedback_events = (
        M365RoutingFeedbackEvent(
            ts=datetime(2026, 5, 10, 7, 0, tzinfo=timezone.utc),
            artifact_id="thread:named:acme-ramp-thread",
            action="reject",
            pm_alias="operator",
        ),
    )

    plans = gather._build_workiq_query_plans(
        program=program,
        workstreams=workstreams,
        registry_artifacts=registry_artifacts,
        feedback_events=feedback_events,
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    assert "email_thread:ws1:thread-1" not in {p.query_name for p in plans}


def test_query_plans_feedback_search_excludes_recently_rejected_registry_keywords() -> None:
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True,
            prefer_agency=True,
            workiq_queries={"feedback_search": "Find current status"},
        ),
    )
    workstreams = (
        Workstream(
            id="ws1",
            name="Ramp",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(),
        ),
    )
    registry_artifacts = (
        M365RegistryArtifact(
            artifact_id="thread:named:acme-ramp-thread",
            artifact_type="email_thread",
            inferred_workstream="ws1",
            confidence=1.0,
            confidence_source="pm_confirmed",
            pm_confirmed=True,
            promoted_to_workstreams_yaml=False,
            first_seen=date(2026, 5, 1),
            last_seen=date(2026, 5, 10),
            display_name="Ramp Thread",
            thread_id="thread-1",
            topics=("SCHIE gaps",),
            routing_reasoning="Previously routed to Ramp.",
        ),
    )
    feedback_events = (
        M365RoutingFeedbackEvent(
            ts=datetime(2026, 5, 10, 7, 0, tzinfo=timezone.utc),
            artifact_id="thread:named:acme-ramp-thread",
            action="reject",
            pm_alias="operator",
        ),
    )

    plans = gather._build_workiq_query_plans(
        program=program,
        workstreams=workstreams,
        registry_artifacts=registry_artifacts,
        feedback_events=feedback_events,
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    assert "feedback_search:ws1" not in {p.query_name for p in plans}


def test_query_plans_feedback_search_learns_keywords_from_approved_signals() -> None:
    program = _m365_query_plan_program()
    workstreams = (
        Workstream(
            id="ws1",
            name="Ramp",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(),
        ),
    )

    plans = gather._build_workiq_query_plans(
        program=program,
        workstreams=workstreams,
        approved_signals_by_workstream={
            "ws1": (
                "Northwind launch blocker needs triage",
                "Northwind launch risk remains open",
            )
        },
    )

    feedback_plan = next(p for p in plans if p.query_name == "feedback_search:ws1")
    assert "northwind launch" in (feedback_plan.question or "").lower()


def test_query_plans_feedback_search_learns_exclusions_from_rejected_signals() -> None:
    program = _m365_query_plan_program()
    workstreams = (
        Workstream(
            id="ws1",
            name="Ramp",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(workiq_keywords=("SCHIE",)),
        ),
    )

    plans = gather._build_workiq_query_plans(
        program=program,
        workstreams=workstreams,
        rejected_signals_by_workstream={
            "ws1": (
                "Legacy deck chatter is noisy",
                "Legacy deck ask should be ignored",
            )
        },
    )

    feedback_plan = next(p for p in plans if p.query_name == "feedback_search:ws1")
    assert "legacy deck" in feedback_plan.exclude_keywords
    assert "Exclude:" in (feedback_plan.question or "")
    assert "legacy deck" in (feedback_plan.question or "").lower()


def test_query_plans_emit_exploration_plan_from_work_item_titles_when_keywords_missing() -> None:
    program = _m365_query_plan_program()
    workstreams = (
        Workstream(
            id="ws1",
            name="Ramp",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(),
        ),
    )
    items = (
        WorkItem(
            id=101,
            type="Feature",
            title="Northwind Launch Readiness",
            state="Active",
            assigned_to=None,
            assigned_to_email=None,
            area_path="One\\Adventure\\Acme",
            iteration_path="One\\Adventure\\Acme\\Sprint 1",
            target_date=None,
            risk_level=RiskLevel.UNKNOWN,
            tags=[],
            custom_fields={},
            fetched_at=datetime(2026, 6, 2, tzinfo=timezone.utc),
        ),
        WorkItem(
            id=102,
            type="Feature",
            title="Northwind Launch Checklist",
            state="Active",
            assigned_to=None,
            assigned_to_email=None,
            area_path="One\\Adventure\\Acme",
            iteration_path="One\\Adventure\\Acme\\Sprint 1",
            target_date=None,
            risk_level=RiskLevel.UNKNOWN,
            tags=[],
            custom_fields={},
            fetched_at=datetime(2026, 6, 2, tzinfo=timezone.utc),
        ),
    )

    plans = gather._build_workiq_query_plans(
        program=program,
        workstreams=workstreams,
        items=items,
    )

    names = {p.query_name for p in plans}
    assert "feedback_explore:ws1" in names
    assert "feedback_search:ws1" not in names
    explore_plan = next(p for p in plans if p.query_name == "feedback_explore:ws1")
    assert "northwind launch" in (explore_plan.question or "").lower()


def test_query_plans_emit_exploration_plan_from_milestone_names_when_item_keywords_missing() -> None:
    program = _m365_query_plan_program()
    workstreams = (
        Workstream(
            id="ws1",
            name="Ramp",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(),
        ),
    )
    milestones = (
        Milestone(
            id="ms1",
            program_id="acme",
            name="Northwind Launch GA",
            target_date=date(2026, 6, 20),
            owner_alias="operator",
            status=MilestoneStatus.ON_TRACK,
            exit_criteria=(),
            linked_workstream_ids=("ws1",),
            linked_work_item_ids=(),
        ),
    )

    plans = gather._build_workiq_query_plans(
        program=program,
        workstreams=workstreams,
        milestones=milestones,
    )

    explore_plan = next(p for p in plans if p.query_name == "feedback_explore:ws1")
    assert "northwind launch" in (explore_plan.question or "").lower()


def test_m365_discovery_query_excludes_recently_rejected_registry_keywords() -> None:
    workstreams = (
        Workstream(
            id="ws1",
            name="Ramp",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(),
        ),
    )
    registry_artifacts = (
        M365RegistryArtifact(
            artifact_id="thread:named:acme-ramp-thread",
            artifact_type="email_thread",
            inferred_workstream="ws1",
            confidence=1.0,
            confidence_source="pm_confirmed",
            pm_confirmed=True,
            promoted_to_workstreams_yaml=False,
            first_seen=date(2026, 5, 1),
            last_seen=date(2026, 5, 10),
            display_name="Ramp Thread",
            thread_id="thread-1",
            topics=("SCHIE gaps",),
            routing_reasoning="Previously routed to Ramp.",
        ),
    )
    feedback_events = (
        M365RoutingFeedbackEvent(
            ts=datetime(2026, 5, 10, 7, 0, tzinfo=timezone.utc),
            artifact_id="thread:named:acme-ramp-thread",
            action="reject",
            pm_alias="operator",
        ),
    )

    query = gather._build_m365_discovery_query(
        workstreams=workstreams,
        registry_artifacts=registry_artifacts,
        feedback_events=feedback_events,
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    assert query == "Ramp"


def test_build_m365_discovery_queries_preserves_workstream_specific_seeded_terms() -> None:
    workstreams = (
        Workstream(
            id="dd_on_pf",
            name="Direct Drive Northwind",
            area_paths=("One\\Devices\\Contoso",),
            signal_sources=WorkstreamSignalSources(
                workiq_keywords=(
                    "Contoso pilot",
                    "pilot readiness",
                    "Kiona",
                    "AutoTSG",
                    "GFU",
                    "GFU SSD",
                    "DD performance",
                    "firmware sign-off",
                ),
                teams_meeting_series=(TeamsMeetingSeries(display_name="Contoso Weekly Review", series_id=None),),
            ),
        ),
        Workstream(
            id="acme",
            name="Adventure on Northwind",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(
                teams_meeting_series=(
                    TeamsMeetingSeries(display_name="Acme Weekly Ops Review", series_id=None),
                    TeamsMeetingSeries(display_name="Adventure Ramp Weekly Sync", series_id=None),
                ),
                teams_chats=(TeamsChat(display_name="Acme Eng Core Chat", thread_id=None),),
            ),
        ),
    )

    queries = gather._build_m365_discovery_queries(
        workstreams=workstreams,
        registry_artifacts=(),
        feedback_events=(),
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    assert len(queries) == 2
    assert "Contoso Weekly Review" in queries[0]
    assert "Acme Weekly Ops Review" in queries[1]
    assert "Adventure Ramp Weekly Sync" in queries[1]
    assert "Acme Eng Core Chat" in queries[1]
    assert "Adventure on Northwind" in queries[1]


def test_query_plans_registry_artifacts_skip_ids_already_configured() -> None:
    program = _m365_query_plan_program()
    workstreams = (
        Workstream(
            id="ws1",
            name="Ramp",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(
                teams_chats=(TeamsChat(display_name="Configured Ramp Chat", thread_id="chat-1"),),
            ),
        ),
    )
    registry_artifacts = (
        M365RegistryArtifact(
            artifact_id="chan:acme-ramp-chat",
            artifact_type="teams_channel",
            inferred_workstream="ws1",
            confidence=1.0,
            confidence_source="pm_confirmed",
            pm_confirmed=True,
            promoted_to_workstreams_yaml=False,
            first_seen=date(2026, 5, 1),
            last_seen=date(2026, 5, 10),
            display_name="Configured Ramp Chat",
            thread_id="chat-1",
            routing_reasoning="Seeded from prior review.",
        ),
    )

    plans = gather._build_workiq_query_plans(
        program=program,
        workstreams=workstreams,
        registry_artifacts=registry_artifacts,
    )

    assert len([p for p in plans if p.query_name == "teams_chat:ws1:chat-1"]) == 1


def _all_progress_flags() -> dict[str, bool]:
    return {
        "include_workiq": True,
        "include_kusto": True,
        "include_analytics": True,
        "include_sprints": True,
        "include_pipelines": True,
        "include_icm": True,
        "include_dependency_scout": True,
        "include_background_synthesis": True,
        "include_engms": True,
    }


def test_progress_steps_register_teams_uil_when_channel_enabled(monkeypatch) -> None:
    monkeypatch.setenv("VERTEX_UIL_TEAMS", "1")
    steps = gather._build_gather_progress_steps(**_all_progress_flags())
    assert "teams_uil" in steps
    # Registered position mirrors execution order: right after the workiq step.
    assert steps.index("teams_uil") == steps.index("workiq") + 1
    assert "engms" in steps


def test_progress_steps_omit_teams_uil_when_channel_disabled(monkeypatch) -> None:
    monkeypatch.setenv("VERTEX_UIL_TEAMS", "0")
    steps = gather._build_gather_progress_steps(**_all_progress_flags())
    assert "teams_uil" not in steps


def test_progress_steps_omit_engms_when_flag_off(monkeypatch) -> None:
    monkeypatch.setenv("VERTEX_UIL_TEAMS", "0")
    flags = _all_progress_flags()
    flags["include_engms"] = False
    steps = gather._build_gather_progress_steps(**flags)
    assert "engms" not in steps
