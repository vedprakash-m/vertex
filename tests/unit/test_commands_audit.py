from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from cli import app
from src.ai.edit_learner import append_edit_patterns, build_edit_patterns
from src.commands.audit import build_audit_timeline
from src.core.ado_proposal import ADOUpdateEntry, ADOUpdateProposal, write_proposal_manifest
from src.core.analytics_store import AutonomyAuditRecord, append_autonomy_audit_record, get_program_autonomy_audit_archive_path, get_program_autonomy_audit_path
from src.core.feedback.signal_approval_learner import load_promoted_signal_approval_rules
from src.core.journal import append_review_decision, append_signal, append_usage_marker
from src.core.models import Confidence
from src.core.models_v2 import Signal, SignalReviewDecision, SignalUsageMarker
from src.core.sqlite_stores import SQLiteSignalStore


runner = CliRunner()
FROZEN_SIGNAL_AT = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)
FROZEN_REVIEW_AT = datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)
FROZEN_EDIT_PATTERN_AT = datetime(2026, 5, 1, 10, 3, tzinfo=timezone.utc)
FROZEN_USAGE_AT = datetime(2026, 5, 1, 10, 5, tzinfo=timezone.utc)
FROZEN_ARCHIVE_AT = datetime(2026, 5, 1, 11, 0, tzinfo=timezone.utc)
FROZEN_TRACE_ISSUE_007_AT = datetime(2026, 5, 1, 10, 4, tzinfo=timezone.utc)
FROZEN_TRACE_ISSUE_008_STALE_AT = datetime(2026, 5, 2, 10, 4, tzinfo=timezone.utc)
FROZEN_TRACE_ISSUE_008_AT = datetime(2026, 5, 2, 10, 6, tzinfo=timezone.utc)
FROZEN_TRACE_ISSUE_009_MATCH_AT = datetime(2026, 5, 3, 10, 4, tzinfo=timezone.utc)
FROZEN_TRACE_ISSUE_009_NEWER_AT = datetime(2026, 5, 3, 10, 6, tzinfo=timezone.utc)
FROZEN_TRACE_RUN_ID_ISSUE_007 = "acme_weekly:issue-007:20260501T100000Z"


def test_build_audit_timeline_orders_journal_and_archive_events(tmp_path: Path) -> None:
    programs_root = _seed_audit_workspace(tmp_path)
    _append_trace_records(
        programs_root,
        "acme_weekly",
        (
            {
                "timestamp": FROZEN_TRACE_ISSUE_007_AT.isoformat(),
                "edition": "acme_weekly",
                "run_id": FROZEN_TRACE_RUN_ID_ISSUE_007,
                "caller": "src.commands.report._synthesize_v2_ai_content",
                "model": "gpt-4o-mini",
                "deployment": "fake-exec-v1",
                "prompt_version": "exec_summary_drafter.v1",
                "latency_ms": 140.0,
                "cost_usd": 0.03,
                "metadata": {
                    "issue_number": 7,
                    "task_type": "exec_summary",
                    "section_id": "exec_summary",
                },
            },
        ),
    )

    events = build_audit_timeline("acme", programs_root=programs_root)

    assert [event.category for event in events] == ["signal", "review", "edit_pattern", "usage", "archive"]
    assert events[0].reference == "signal-1"
    assert events[1].summary == "approved review: Looks good"
    assert events[2].edition_id == "acme_weekly"
    assert events[2].reference == "exec_summary"
    assert events[2].source == "exec_summary_drafter.v1"
    assert events[2].trace_run_id == FROZEN_TRACE_RUN_ID_ISSUE_007
    assert events[2].model == "gpt-4o-mini"
    assert events[2].deployment == "fake-exec-v1"
    assert events[2].prompt_version == "exec_summary_drafter.v1"
    assert events[2].task_type == "exec_summary"
    assert events[2].ai_confidence == Confidence.MEDIUM.value
    assert events[2].author_override_magnitude is not None
    assert "task=exec_summary" in events[2].summary
    assert "override=" in events[2].summary
    assert events[3].edition_id == "acme_weekly"
    assert events[4].summary == "Confirmed publish | QG 2/3 passing | freshness b0/w1/i2"


def test_build_audit_timeline_reads_sqlite_backed_reviews_and_usage_markers(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        "\n".join(
            (
                "schema_version: '2.0'",
                "id: acme",
                "name: Acme",
                "storage_backend: sqlite",
                "",
            )
        ),
        encoding="utf-8",
    )

    store = SQLiteSignalStore(programs_root=programs_root)
    store.append(
        Signal(
            id="signal-1",
            timestamp=FROZEN_SIGNAL_AT,
            source="ado/revision",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:1001",),
            text="Deployment update received",
            raw_ref="WI:1001",
            confidence=Confidence.HIGH,
            metadata=None,
            thread_id=None,
        )
    )
    store.append_review(
        "acme",
        SignalReviewDecision(
            signal_id="signal-1",
            decision="deferred",
            reviewed_at=FROZEN_REVIEW_AT,
            reviewed_by="owner",
            note="Need more context",
        ),
    )
    store.append_review(
        "acme",
        SignalReviewDecision(
            signal_id="signal-1",
            decision="approved",
            reviewed_at=FROZEN_REVIEW_AT.replace(minute=5),
            reviewed_by="owner",
            note="Looks good",
        ),
    )
    store.append_usage_marker(
        "acme",
        SignalUsageMarker(
            signal_id="signal-1",
            issue_number=7,
            edition_id="acme_weekly",
            manifest_id="manifest-007",
            used_at=FROZEN_USAGE_AT,
        ),
    )

    events = build_audit_timeline("acme", programs_root=programs_root)

    assert [event.category for event in events] == ["signal", "review", "review", "usage"]
    assert events[1].summary == "deferred review: Need more context"
    assert events[2].summary == "approved review: Looks good"
    assert events[3].summary == "Used in issue 007"


def test_audit_cli_supports_human_json_and_csv(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_audit_workspace(tmp_path)
    monkeypatch.setattr("src.commands.audit.PROGRAMS_ROOT", programs_root)
    _append_trace_records(
        programs_root,
        "acme_weekly",
        (
            {
                "timestamp": FROZEN_TRACE_ISSUE_007_AT.isoformat(),
                "edition": "acme_weekly",
                "run_id": FROZEN_TRACE_RUN_ID_ISSUE_007,
                "caller": "src.commands.report._synthesize_v2_ai_content",
                "model": "gpt-4o-mini",
                "deployment": "fake-exec-v1",
                "prompt_version": "exec_summary_drafter.v1",
                "latency_ms": 140.0,
                "cost_usd": 0.03,
                "metadata": {
                    "issue_number": 7,
                    "task_type": "exec_summary",
                    "section_id": "exec_summary",
                },
            },
        ),
    )

    human_result = runner.invoke(app, ["audit", "--program", "acme"])
    json_result = runner.invoke(app, ["audit", "--program", "acme", "--format", "json"])
    csv_result = runner.invoke(app, ["audit", "--program", "acme", "--format", "csv"])

    assert human_result.exit_code == 0
    assert "Audit Timeline: acme" in human_result.stdout
    assert "signal | signal-1 | ado/revision | Deployment update received" in human_result.stdout
    assert (
        f"edit_pattern | acme_weekly | exec_summary | exec_summary_drafter.v1 | trace_run_id={FROZEN_TRACE_RUN_ID_ISSUE_007} | model=gpt-4o-mini | deployment=fake-exec-v1 | task=exec_summary"
        in human_result.stdout
    )
    assert "archive | acme_weekly | issue_007 | confirmed" in human_result.stdout

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["program_id"] == "acme"
    assert [event["category"] for event in payload["events"]] == ["signal", "review", "edit_pattern", "usage", "archive"]
    assert payload["events"][0]["timestamp"] == FROZEN_SIGNAL_AT.isoformat()
    assert payload["events"][2]["source"] == "exec_summary_drafter.v1"
    assert payload["events"][2]["trace_run_id"] == FROZEN_TRACE_RUN_ID_ISSUE_007
    assert payload["events"][2]["model"] == "gpt-4o-mini"
    assert payload["events"][2]["deployment"] == "fake-exec-v1"
    assert payload["events"][2]["prompt_version"] == "exec_summary_drafter.v1"
    assert payload["events"][2]["task_type"] == "exec_summary"
    assert payload["events"][2]["ai_confidence"] == Confidence.MEDIUM.value
    assert payload["events"][2]["author_override_magnitude"] is not None
    assert "override=" in payload["events"][2]["summary"]

    assert csv_result.exit_code == 0
    rows = csv_result.stdout.strip().splitlines()
    assert rows[0] == "timestamp,category,edition_id,reference,source,trace_run_id,model,deployment,prompt_version,task_type,ai_confidence,author_override_magnitude,summary"
    assert any(
        f"edit_pattern,acme_weekly,exec_summary,exec_summary_drafter.v1,{FROZEN_TRACE_RUN_ID_ISSUE_007},gpt-4o-mini,fake-exec-v1,exec_summary_drafter.v1,exec_summary,{Confidence.MEDIUM.value}," in row
        for row in rows[1:]
    )
    assert any(
        row.startswith("2026-05-01T11:00:00+00:00,archive,acme_weekly,issue_007,confirmed")
        and row.endswith("Confirmed publish | QG 2/3 passing | freshness b0/w1/i2")
        for row in rows[1:]
    )


def test_build_audit_timeline_includes_incident_linked_autonomy_records(tmp_path: Path) -> None:
    programs_root = _seed_audit_workspace(tmp_path)
    append_autonomy_audit_record(
        AutonomyAuditRecord(
            program_id="acme",
            action_id="action-incident-1",
            level="l2",
            author_alias="operator",
            subject_alias="operator",
            action_type="decision_ask_nudge",
            evidence_refs=("WI:123", "ICM:22001", "decision_ask:d-incident"),
            policy_rule="decision_ask_nudge",
            accepted=True,
            applied_at=FROZEN_ARCHIVE_AT + timedelta(minutes=30),
            blast_radius="1 local EML draft",
            rollback_mechanism="Delete the draft EML before send.",
            prior_acceptance_rate=0.75,
        ),
        programs_root=programs_root,
    )

    events = build_audit_timeline("acme", programs_root=programs_root)

    assert [event.category for event in events] == ["signal", "review", "edit_pattern", "usage", "archive", "autonomy"]
    assert events[-1].reference == "action-incident-1"
    assert events[-1].source == "decision_ask_nudge"
    assert events[-1].summary == "approved decision_ask_nudge for operator | incident-linked via ICM:22001"


def test_audit_cli_renders_incident_linked_autonomy_summary(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_audit_workspace(tmp_path)
    monkeypatch.setattr("src.commands.audit.PROGRAMS_ROOT", programs_root)
    append_autonomy_audit_record(
        AutonomyAuditRecord(
            program_id="acme",
            action_id="action-incident-1",
            level="l2",
            author_alias="operator",
            subject_alias="operator",
            action_type="decision_ask_nudge",
            evidence_refs=("WI:123", "ICM:22001", "decision_ask:d-incident"),
            policy_rule="decision_ask_nudge",
            accepted=True,
            applied_at=FROZEN_ARCHIVE_AT + timedelta(minutes=30),
            blast_radius="1 local EML draft",
            rollback_mechanism="Delete the draft EML before send.",
            prior_acceptance_rate=0.75,
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(app, ["audit", "--program", "acme"])

    assert result.exit_code == 0
    assert "autonomy | action-incident-1 | decision_ask_nudge | approved decision_ask_nudge for operator | incident-linked via ICM:22001" in result.stdout


def test_audit_cli_prompt_learning_summary_supports_human_json_and_csv(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_audit_workspace(tmp_path)
    monkeypatch.setattr("src.commands.audit.PROGRAMS_ROOT", programs_root)

    append_edit_patterns(
        "acme",
        build_edit_patterns(
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=8,
            recorded_at=FROZEN_EDIT_PATTERN_AT.replace(day=2),
            draft_exec_summary_text="AI exec summary draft is concise and nearly final.",
            confirmed_exec_summary_text="AI exec summary draft is concise and nearly final with one added fact.",
            draft_workstream_blurbs={},
            confirmed_workstream_blurbs={},
            draft_prompt_versions={"exec_summary": "exec_summary_drafter.v2"},
            draft_ai_confidences={"exec_summary": Confidence.HIGH.value},
        ),
        programs_root=programs_root,
    )
    _append_trace_records(
        programs_root,
        "acme_weekly",
        (
            {
                "timestamp": FROZEN_TRACE_ISSUE_007_AT.isoformat(),
                "edition": "acme_weekly",
                "run_id": "acme_weekly:issue-007:20260501T100000Z",
                "caller": "src.commands.report._synthesize_v2_ai_content",
                "model": "gpt-4o-mini",
                "deployment": "fake-exec-v1",
                "prompt_version": "exec_summary_drafter.v1",
                "latency_ms": 140.0,
                "cost_usd": 0.03,
                "metadata": {
                    "issue_number": 7,
                    "task_type": "exec_summary",
                    "section_id": "exec_summary",
                },
            },
            {
                "timestamp": FROZEN_TRACE_ISSUE_008_STALE_AT.isoformat(),
                "edition": "acme_weekly",
                "run_id": "acme_weekly:issue-008:20260502T100400Z",
                "caller": "src.commands.report._synthesize_v2_ai_content",
                "model": "gpt-4o-stale",
                "deployment": "fake-exec-stale",
                "prompt_version": "exec_summary_drafter.v2",
                "latency_ms": 999.0,
                "cost_usd": 0.99,
                "metadata": {
                    "issue_number": 8,
                    "task_type": "exec_summary",
                    "section_id": "exec_summary",
                },
            },
            {
                "timestamp": FROZEN_TRACE_ISSUE_008_AT.isoformat(),
                "edition": "acme_weekly",
                "run_id": "acme_weekly:issue-008:20260502T100600Z",
                "caller": "src.commands.report._synthesize_v2_ai_content",
                "model": "gpt-4o",
                "deployment": "fake-exec-v2",
                "prompt_version": "exec_summary_drafter.v2",
                "latency_ms": 120.0,
                "cost_usd": 0.015,
                "metadata": {
                    "issue_number": 8,
                    "task_type": "exec_summary",
                    "section_id": "exec_summary",
                },
            },
        ),
    )

    human_result = runner.invoke(app, ["audit", "--program", "acme", "--prompt-learning-summary"])
    json_result = runner.invoke(
        app,
        ["audit", "--program", "acme", "--prompt-learning-summary", "--format", "json"],
    )
    csv_result = runner.invoke(
        app,
        ["audit", "--program", "acme", "--prompt-learning-summary", "--format", "csv"],
    )

    assert human_result.exit_code == 0
    assert "Prompt Learning Summary: acme (last 10 issues)" in human_result.stdout
    assert "Calibration" in human_result.stdout
    assert "- exec_summary | samples=2 | avg_override=" in human_result.stdout
    assert "Confidence Bands" in human_result.stdout
    assert "- exec_summary | high | samples=1 | avg_override=" in human_result.stdout
    assert "- exec_summary | medium | samples=1 | avg_override=" in human_result.stdout
    assert "Prompt Versions" in human_result.stdout
    assert "- exec_summary | exec_summary_drafter.v2 | samples=1 | avg_override=" in human_result.stdout
    assert "- exec_summary | exec_summary_drafter.v1 | samples=1 | avg_override=" in human_result.stdout
    assert "Prompt Version Confidence Bands" in human_result.stdout
    assert "- exec_summary | exec_summary_drafter.v2 | high | samples=1 | avg_override=" in human_result.stdout
    assert "- exec_summary | exec_summary_drafter.v1 | medium | samples=1 | avg_override=" in human_result.stdout
    assert "Leaderboard" in human_result.stdout
    assert "1. exec_summary | exec_summary_drafter.v2 | samples=1 | avg_override=" in human_result.stdout
    assert "2. exec_summary | exec_summary_drafter.v1 | samples=1 | avg_override=" in human_result.stdout
    assert "Prompt Version Model Leaderboard" in human_result.stdout
    assert "1. exec_summary | prompt=exec_summary_drafter.v2 | model=gpt-4o | deployments=1 | samples=1 | avg_override=" in human_result.stdout
    assert "2. exec_summary | prompt=exec_summary_drafter.v1 | model=gpt-4o-mini | deployments=1 | samples=1 | avg_override=" in human_result.stdout
    assert "Model/Deployment Leaderboard" in human_result.stdout
    assert "1. exec_summary | model=gpt-4o | deployment=fake-exec-v2 | samples=1 | avg_override=" in human_result.stdout
    assert "2. exec_summary | model=gpt-4o-mini | deployment=fake-exec-v1 | samples=1 | avg_override=" in human_result.stdout
    assert "Prompt Version Model/Deployment Leaderboard" in human_result.stdout
    assert "1. exec_summary | prompt=exec_summary_drafter.v2 | model=gpt-4o | deployment=fake-exec-v2 | samples=1 | avg_override=" in human_result.stdout
    assert "2. exec_summary | prompt=exec_summary_drafter.v1 | model=gpt-4o-mini | deployment=fake-exec-v1 | samples=1 | avg_override=" in human_result.stdout
    assert "Confidence Model/Deployment Leaderboard" in human_result.stdout
    assert "1. exec_summary | high | model=gpt-4o | deployment=fake-exec-v2 | samples=1 | avg_override=" in human_result.stdout
    assert "1. exec_summary | medium | model=gpt-4o-mini | deployment=fake-exec-v1 | samples=1 | avg_override=" in human_result.stdout
    assert "fake-exec-stale" not in human_result.stdout

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["prompt_learning"]["window_issues"] == 10
    assert payload["prompt_learning"]["calibration"][0]["task_type"] == "exec_summary"
    assert payload["prompt_learning"]["calibration"][0]["sample_count"] == 2
    assert [row["ai_confidence"] for row in payload["prompt_learning"]["confidence_bands"]] == ["high", "medium"]
    assert payload["prompt_learning"]["confidence_bands"][0]["sample_count"] == 1
    assert payload["prompt_learning"]["confidence_bands"][1]["sample_count"] == 1
    assert payload["prompt_learning"]["prompt_versions"][0]["prompt_version"] == "exec_summary_drafter.v2"
    assert payload["prompt_learning"]["prompt_versions"][1]["prompt_version"] == "exec_summary_drafter.v1"
    assert {
        (row["prompt_version"], row["ai_confidence"], row["sample_count"])
        for row in payload["prompt_learning"]["prompt_version_confidence_bands"]
    } == {
        ("exec_summary_drafter.v1", Confidence.MEDIUM.value, 1),
        ("exec_summary_drafter.v2", Confidence.HIGH.value, 1),
    }
    assert payload["prompt_learning"]["leaderboard"][0]["rank"] == 1
    assert payload["prompt_learning"]["leaderboard"][0]["prompt_version"] == "exec_summary_drafter.v2"
    assert payload["prompt_learning"]["leaderboard"][1]["rank"] == 2
    assert payload["prompt_learning"]["leaderboard"][1]["prompt_version"] == "exec_summary_drafter.v1"
    assert [
        (row["rank"], row["prompt_version"], row["model"], row["deployment_count"])
        for row in payload["prompt_learning"]["prompt_version_model_leaderboard"]
    ] == [
        (1, "exec_summary_drafter.v2", "gpt-4o", 1),
        (2, "exec_summary_drafter.v1", "gpt-4o-mini", 1),
    ]
    assert payload["prompt_learning"]["prompt_version_model_leaderboard"][0]["average_latency_ms"] == 120.0
    assert payload["prompt_learning"]["prompt_version_model_leaderboard"][0]["average_cost_usd"] == 0.015
    assert [
        row["deployment"] for row in payload["prompt_learning"]["model_deployment_leaderboard"]
    ] == ["fake-exec-v2", "fake-exec-v1"]
    assert payload["prompt_learning"]["model_deployment_leaderboard"][0]["model"] == "gpt-4o"
    assert payload["prompt_learning"]["model_deployment_leaderboard"][0]["average_latency_ms"] == 120.0
    assert payload["prompt_learning"]["model_deployment_leaderboard"][0]["average_cost_usd"] == 0.015
    assert [
        (row["rank"], row["prompt_version"], row["model"], row["deployment"])
        for row in payload["prompt_learning"]["prompt_version_model_deployment_leaderboard"]
    ] == [
        (1, "exec_summary_drafter.v2", "gpt-4o", "fake-exec-v2"),
        (2, "exec_summary_drafter.v1", "gpt-4o-mini", "fake-exec-v1"),
    ]
    assert payload["prompt_learning"]["prompt_version_model_deployment_leaderboard"][0]["average_latency_ms"] == 120.0
    assert payload["prompt_learning"]["prompt_version_model_deployment_leaderboard"][0]["average_cost_usd"] == 0.015
    assert [
        (row["rank"], row["ai_confidence"], row["model"], row["deployment"])
        for row in payload["prompt_learning"]["confidence_model_deployment_leaderboard"]
    ] == [
        (1, Confidence.HIGH.value, "gpt-4o", "fake-exec-v2"),
        (1, Confidence.MEDIUM.value, "gpt-4o-mini", "fake-exec-v1"),
    ]
    assert payload["prompt_learning"]["confidence_model_deployment_leaderboard"][0]["average_latency_ms"] == 120.0
    assert payload["prompt_learning"]["confidence_model_deployment_leaderboard"][0]["average_cost_usd"] == 0.015

    assert csv_result.exit_code == 0
    rows = csv_result.stdout.strip().splitlines()
    assert rows[0] == "timestamp,category,edition_id,reference,source,trace_run_id,model,deployment,prompt_version,task_type,ai_confidence,author_override_magnitude,summary"
    assert any(
        row == "record_type,task_type,rank,prompt_version,ai_confidence,window_issues,sample_count,average_override_magnitude,calibration_score"
        for row in rows
    )
    assert any(row.startswith("calibration_summary,exec_summary,,,,10,2,") for row in rows)
    assert any(row.startswith("confidence_band_summary,exec_summary,,,high,10,1,") for row in rows)
    assert any(row.startswith("confidence_band_summary,exec_summary,,,medium,10,1,") for row in rows)
    assert any(row.startswith("prompt_version_summary,exec_summary,,exec_summary_drafter.v2,,10,1,") for row in rows)
    assert any(row.startswith("prompt_version_summary,exec_summary,,exec_summary_drafter.v1,,10,1,") for row in rows)
    assert any(
        row.startswith("prompt_version_confidence_summary,exec_summary,,exec_summary_drafter.v2,high,10,1,")
        for row in rows
    )
    assert any(
        row.startswith("prompt_version_confidence_summary,exec_summary,,exec_summary_drafter.v1,medium,10,1,")
        for row in rows
    )
    assert any(row.startswith("prompt_version_leaderboard,exec_summary,1,exec_summary_drafter.v2,,10,1,") for row in rows)
    assert any(row.startswith("prompt_version_leaderboard,exec_summary,2,exec_summary_drafter.v1,,10,1,") for row in rows)
    assert any(
        row
        == "record_type,task_type,rank,prompt_version,model,deployment_count,window_issues,sample_count,average_override_magnitude,calibration_score,average_latency_ms,average_cost_usd"
        for row in rows
    )
    assert any(
        row.startswith(
            "prompt_version_model_leaderboard,exec_summary,1,exec_summary_drafter.v2,gpt-4o,1,10,1,"
        )
        for row in rows
    )
    assert any(
        row.startswith(
            "prompt_version_model_leaderboard,exec_summary,2,exec_summary_drafter.v1,gpt-4o-mini,1,10,1,"
        )
        for row in rows
    )
    assert any(
        row == "record_type,task_type,rank,model,deployment,window_issues,sample_count,average_override_magnitude,calibration_score,average_latency_ms,average_cost_usd"
        for row in rows
    )
    assert any(row.startswith("model_deployment_leaderboard,exec_summary,1,gpt-4o,fake-exec-v2,10,1,") for row in rows)
    assert any(row.startswith("model_deployment_leaderboard,exec_summary,2,gpt-4o-mini,fake-exec-v1,10,1,") for row in rows)
    assert any(
        row
        == "record_type,task_type,rank,prompt_version,model,deployment,window_issues,sample_count,average_override_magnitude,calibration_score,average_latency_ms,average_cost_usd"
        for row in rows
    )
    assert any(
        row.startswith(
            "prompt_version_model_deployment_leaderboard,exec_summary,1,exec_summary_drafter.v2,gpt-4o,fake-exec-v2,10,1,"
        )
        for row in rows
    )
    assert any(
        row.startswith(
            "prompt_version_model_deployment_leaderboard,exec_summary,2,exec_summary_drafter.v1,gpt-4o-mini,fake-exec-v1,10,1,"
        )
        for row in rows
    )
    assert any(
        row
        == "record_type,task_type,rank,ai_confidence,model,deployment,window_issues,sample_count,average_override_magnitude,calibration_score,average_latency_ms,average_cost_usd"
        for row in rows
    )
    assert any(
        row.startswith(
            "confidence_model_deployment_leaderboard,exec_summary,1,high,gpt-4o,fake-exec-v2,10,1,"
        )
        for row in rows
    )
    assert any(
        row.startswith(
            "confidence_model_deployment_leaderboard,exec_summary,1,medium,gpt-4o-mini,fake-exec-v1,10,1,"
        )
        for row in rows
    )


def test_audit_cli_prompt_learning_summary_prefers_exact_trace_run_id(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_audit_workspace(tmp_path)
    monkeypatch.setattr("src.commands.audit.PROGRAMS_ROOT", programs_root)

    append_edit_patterns(
        "acme",
        build_edit_patterns(
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=9,
            recorded_at=FROZEN_EDIT_PATTERN_AT.replace(day=3),
            draft_exec_summary_text="AI exec summary draft needs one factual correction.",
            confirmed_exec_summary_text="AI exec summary draft needs one factual correction plus the corrected milestone.",
            draft_workstream_blurbs={},
            confirmed_workstream_blurbs={},
            draft_prompt_versions={"exec_summary": "exec_summary_drafter.v3"},
            draft_ai_confidences={"exec_summary": Confidence.MEDIUM.value},
            draft_trace_run_id="acme_weekly:issue-009:20260503T100400Z",
        ),
        programs_root=programs_root,
    )
    _append_trace_records(
        programs_root,
        "acme_weekly",
        (
            {
                "timestamp": FROZEN_TRACE_ISSUE_009_MATCH_AT.isoformat(),
                "edition": "acme_weekly",
                "run_id": "acme_weekly:issue-009:20260503T100400Z",
                "caller": "src.commands.report._synthesize_v2_ai_content",
                "model": "gpt-4o-mini",
                "deployment": "fake-exec-match",
                "prompt_version": "exec_summary_drafter.v3",
                "latency_ms": 111.0,
                "cost_usd": 0.021,
                "metadata": {
                    "issue_number": 9,
                    "task_type": "exec_summary",
                    "section_id": "exec_summary",
                },
            },
            {
                "timestamp": FROZEN_TRACE_ISSUE_009_NEWER_AT.isoformat(),
                "edition": "acme_weekly",
                "run_id": "acme_weekly:issue-009:20260503T100600Z",
                "caller": "src.commands.report._synthesize_v2_ai_content",
                "model": "gpt-4o",
                "deployment": "fake-exec-newer",
                "prompt_version": "exec_summary_drafter.v3",
                "latency_ms": 90.0,
                "cost_usd": 0.019,
                "metadata": {
                    "issue_number": 9,
                    "task_type": "exec_summary",
                    "section_id": "exec_summary",
                },
            },
        ),
    )

    json_result = runner.invoke(
        app,
        ["audit", "--program", "acme", "--prompt-learning-summary", "--format", "json"],
    )

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert [
        row["deployment"] for row in payload["prompt_learning"]["model_deployment_leaderboard"]
    ] == ["fake-exec-match"]
    assert payload["prompt_learning"]["model_deployment_leaderboard"][0]["model"] == "gpt-4o-mini"
    assert payload["prompt_learning"]["model_deployment_leaderboard"][0]["average_latency_ms"] == 111.0
    assert payload["prompt_learning"]["model_deployment_leaderboard"][0]["average_cost_usd"] == 0.021
    assert [
        (row["prompt_version"], row["model"], row["deployment"])
        for row in payload["prompt_learning"]["prompt_version_model_deployment_leaderboard"]
    ] == [("exec_summary_drafter.v3", "gpt-4o-mini", "fake-exec-match")]
    assert [
        (row["ai_confidence"], row["model"], row["deployment"])
        for row in payload["prompt_learning"]["confidence_model_deployment_leaderboard"]
    ] == [(Confidence.MEDIUM.value, "gpt-4o-mini", "fake-exec-match")]


def test_audit_cli_prompt_learning_summary_aggregates_model_leaderboard_across_deployments(
    monkeypatch,
    tmp_path: Path,
) -> None:
    programs_root = _seed_audit_workspace(tmp_path)
    monkeypatch.setattr("src.commands.audit.PROGRAMS_ROOT", programs_root)

    append_edit_patterns(
        "acme",
        build_edit_patterns(
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=8,
            recorded_at=FROZEN_EDIT_PATTERN_AT.replace(day=2),
            draft_exec_summary_text="AI exec summary draft is concise and nearly final.",
            confirmed_exec_summary_text="AI exec summary draft is concise and nearly final with one detail.",
            draft_workstream_blurbs={},
            confirmed_workstream_blurbs={},
            draft_prompt_versions={"exec_summary": "exec_summary_drafter.v2"},
            draft_ai_confidences={"exec_summary": Confidence.HIGH.value},
            draft_trace_run_id="acme_weekly:issue-008:20260502T100600Z",
        ),
        programs_root=programs_root,
    )
    append_edit_patterns(
        "acme",
        build_edit_patterns(
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=9,
            recorded_at=FROZEN_EDIT_PATTERN_AT.replace(day=3),
            draft_exec_summary_text="AI exec summary draft keeps the same direction.",
            confirmed_exec_summary_text="AI exec summary draft keeps the same direction with one update.",
            draft_workstream_blurbs={},
            confirmed_workstream_blurbs={},
            draft_prompt_versions={"exec_summary": "exec_summary_drafter.v3"},
            draft_ai_confidences={"exec_summary": Confidence.HIGH.value},
            draft_trace_run_id="acme_weekly:issue-009:20260503T100400Z",
        ),
        programs_root=programs_root,
    )
    _append_trace_records(
        programs_root,
        "acme_weekly",
        (
            {
                "timestamp": FROZEN_TRACE_ISSUE_007_AT.isoformat(),
                "edition": "acme_weekly",
                "run_id": FROZEN_TRACE_RUN_ID_ISSUE_007,
                "caller": "src.commands.report._synthesize_v2_ai_content",
                "model": "gpt-4o-mini",
                "deployment": "fake-exec-v1",
                "prompt_version": "exec_summary_drafter.v1",
                "latency_ms": 140.0,
                "cost_usd": 0.03,
                "metadata": {
                    "issue_number": 7,
                    "task_type": "exec_summary",
                    "section_id": "exec_summary",
                },
            },
            {
                "timestamp": FROZEN_TRACE_ISSUE_008_AT.isoformat(),
                "edition": "acme_weekly",
                "run_id": "acme_weekly:issue-008:20260502T100600Z",
                "caller": "src.commands.report._synthesize_v2_ai_content",
                "model": "gpt-4o",
                "deployment": "fake-exec-v2",
                "prompt_version": "exec_summary_drafter.v2",
                "latency_ms": 120.0,
                "cost_usd": 0.015,
                "metadata": {
                    "issue_number": 8,
                    "task_type": "exec_summary",
                    "section_id": "exec_summary",
                },
            },
            {
                "timestamp": FROZEN_TRACE_ISSUE_009_MATCH_AT.isoformat(),
                "edition": "acme_weekly",
                "run_id": "acme_weekly:issue-009:20260503T100400Z",
                "caller": "src.commands.report._synthesize_v2_ai_content",
                "model": "gpt-4o",
                "deployment": "fake-exec-canary",
                "prompt_version": "exec_summary_drafter.v3",
                "latency_ms": 180.0,
                "cost_usd": 0.045,
                "metadata": {
                    "issue_number": 9,
                    "task_type": "exec_summary",
                    "section_id": "exec_summary",
                },
            },
        ),
    )

    human_result = runner.invoke(app, ["audit", "--program", "acme", "--prompt-learning-summary"])
    json_result = runner.invoke(
        app,
        ["audit", "--program", "acme", "--prompt-learning-summary", "--format", "json"],
    )
    csv_result = runner.invoke(
        app,
        ["audit", "--program", "acme", "--prompt-learning-summary", "--format", "csv"],
    )

    assert human_result.exit_code == 0
    assert "Model Leaderboard" in human_result.stdout
    assert "1. exec_summary | model=gpt-4o | deployments=2 | samples=2 | avg_override=" in human_result.stdout

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert [
        (
            row["rank"],
            row["model"],
            row["deployment_count"],
            row["sample_count"],
            row["average_latency_ms"],
            row["average_cost_usd"],
        )
        for row in payload["prompt_learning"]["model_leaderboard"]
    ] == [
        (1, "gpt-4o", 2, 2, 150.0, 0.03),
        (2, "gpt-4o-mini", 1, 1, 140.0, 0.03),
    ]
    assert {
        (row["model"], row["deployment"])
        for row in payload["prompt_learning"]["model_deployment_leaderboard"]
    } == {
        ("gpt-4o", "fake-exec-v2"),
        ("gpt-4o", "fake-exec-canary"),
        ("gpt-4o-mini", "fake-exec-v1"),
    }

    assert csv_result.exit_code == 0
    rows = csv_result.stdout.strip().splitlines()
    assert any(
        row
        == "record_type,task_type,rank,model,deployment_count,window_issues,sample_count,average_override_magnitude,calibration_score,average_latency_ms,average_cost_usd"
        for row in rows
    )
    assert any(row.startswith("model_leaderboard,exec_summary,1,gpt-4o,2,10,2,") for row in rows)
    assert any(row.startswith("model_leaderboard,exec_summary,2,gpt-4o-mini,1,10,1,") for row in rows)


def test_audit_cli_prompt_learning_summary_aggregates_prompt_version_model_leaderboard_across_deployments(
    monkeypatch,
    tmp_path: Path,
) -> None:
    programs_root = _seed_audit_workspace(tmp_path)
    monkeypatch.setattr("src.commands.audit.PROGRAMS_ROOT", programs_root)

    append_edit_patterns(
        "acme",
        build_edit_patterns(
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=8,
            recorded_at=FROZEN_EDIT_PATTERN_AT.replace(day=2),
            draft_exec_summary_text="AI exec summary draft is concise and nearly final.",
            confirmed_exec_summary_text="AI exec summary draft is concise and nearly final with one detail.",
            draft_workstream_blurbs={},
            confirmed_workstream_blurbs={},
            draft_prompt_versions={"exec_summary": "exec_summary_drafter.v2"},
            draft_ai_confidences={"exec_summary": Confidence.HIGH.value},
            draft_trace_run_id="acme_weekly:issue-008:20260502T100600Z",
        ),
        programs_root=programs_root,
    )
    append_edit_patterns(
        "acme",
        build_edit_patterns(
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=9,
            recorded_at=FROZEN_EDIT_PATTERN_AT.replace(day=3),
            draft_exec_summary_text="AI exec summary draft is concise and nearly final.",
            confirmed_exec_summary_text="AI exec summary draft is concise and nearly final with a second detail.",
            draft_workstream_blurbs={},
            confirmed_workstream_blurbs={},
            draft_prompt_versions={"exec_summary": "exec_summary_drafter.v2"},
            draft_ai_confidences={"exec_summary": Confidence.HIGH.value},
            draft_trace_run_id="acme_weekly:issue-009:20260503T100400Z",
        ),
        programs_root=programs_root,
    )
    _append_trace_records(
        programs_root,
        "acme_weekly",
        (
            {
                "timestamp": FROZEN_TRACE_ISSUE_007_AT.isoformat(),
                "edition": "acme_weekly",
                "run_id": FROZEN_TRACE_RUN_ID_ISSUE_007,
                "caller": "src.commands.report._synthesize_v2_ai_content",
                "model": "gpt-4o-mini",
                "deployment": "fake-exec-v1",
                "prompt_version": "exec_summary_drafter.v1",
                "latency_ms": 140.0,
                "cost_usd": 0.03,
                "metadata": {
                    "issue_number": 7,
                    "task_type": "exec_summary",
                    "section_id": "exec_summary",
                },
            },
            {
                "timestamp": FROZEN_TRACE_ISSUE_008_AT.isoformat(),
                "edition": "acme_weekly",
                "run_id": "acme_weekly:issue-008:20260502T100600Z",
                "caller": "src.commands.report._synthesize_v2_ai_content",
                "model": "gpt-4o",
                "deployment": "fake-exec-v2",
                "prompt_version": "exec_summary_drafter.v2",
                "latency_ms": 120.0,
                "cost_usd": 0.015,
                "metadata": {
                    "issue_number": 8,
                    "task_type": "exec_summary",
                    "section_id": "exec_summary",
                },
            },
            {
                "timestamp": FROZEN_TRACE_ISSUE_009_MATCH_AT.isoformat(),
                "edition": "acme_weekly",
                "run_id": "acme_weekly:issue-009:20260503T100400Z",
                "caller": "src.commands.report._synthesize_v2_ai_content",
                "model": "gpt-4o",
                "deployment": "fake-exec-canary",
                "prompt_version": "exec_summary_drafter.v2",
                "latency_ms": 180.0,
                "cost_usd": 0.045,
                "metadata": {
                    "issue_number": 9,
                    "task_type": "exec_summary",
                    "section_id": "exec_summary",
                },
            },
        ),
    )

    human_result = runner.invoke(app, ["audit", "--program", "acme", "--prompt-learning-summary"])
    json_result = runner.invoke(
        app,
        ["audit", "--program", "acme", "--prompt-learning-summary", "--format", "json"],
    )
    csv_result = runner.invoke(
        app,
        ["audit", "--program", "acme", "--prompt-learning-summary", "--format", "csv"],
    )

    assert human_result.exit_code == 0
    assert "Prompt Version Model Leaderboard" in human_result.stdout
    assert "1. exec_summary | prompt=exec_summary_drafter.v2 | model=gpt-4o | deployments=2 | samples=2 | avg_override=" in human_result.stdout

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert [
        (
            row["rank"],
            row["prompt_version"],
            row["model"],
            row["deployment_count"],
            row["sample_count"],
            row["average_latency_ms"],
            row["average_cost_usd"],
        )
        for row in payload["prompt_learning"]["prompt_version_model_leaderboard"]
    ] == [
        (1, "exec_summary_drafter.v2", "gpt-4o", 2, 2, 150.0, 0.03),
        (2, "exec_summary_drafter.v1", "gpt-4o-mini", 1, 1, 140.0, 0.03),
    ]
    assert {
        (row["prompt_version"], row["model"], row["deployment"])
        for row in payload["prompt_learning"]["prompt_version_model_deployment_leaderboard"]
    } == {
        ("exec_summary_drafter.v1", "gpt-4o-mini", "fake-exec-v1"),
        ("exec_summary_drafter.v2", "gpt-4o", "fake-exec-v2"),
        ("exec_summary_drafter.v2", "gpt-4o", "fake-exec-canary"),
    }

    assert csv_result.exit_code == 0
    rows = csv_result.stdout.strip().splitlines()
    assert any(
        row
        == "record_type,task_type,rank,prompt_version,model,deployment_count,window_issues,sample_count,average_override_magnitude,calibration_score,average_latency_ms,average_cost_usd"
        for row in rows
    )
    assert any(
        row.startswith(
            "prompt_version_model_leaderboard,exec_summary,1,exec_summary_drafter.v2,gpt-4o,2,10,2,"
        )
        for row in rows
    )
    assert any(
        row.startswith(
            "prompt_version_model_leaderboard,exec_summary,2,exec_summary_drafter.v1,gpt-4o-mini,1,10,1,"
        )
        for row in rows
    )


def test_build_audit_timeline_respects_date_filters(tmp_path: Path) -> None:
    programs_root = _seed_audit_workspace(tmp_path)

    events = build_audit_timeline(
        "acme",
        programs_root=programs_root,
        start_date=FROZEN_REVIEW_AT.date(),
        end_date=FROZEN_REVIEW_AT.date(),
    )

    assert len(events) == 5
    assert [event.category for event in events] == ["signal", "review", "edit_pattern", "usage", "archive"]


def test_build_audit_timeline_tolerates_malformed_archived_manifest(tmp_path: Path) -> None:
    programs_root = _seed_audit_workspace(tmp_path)
    manifest_path = programs_root / "acme" / "archive" / "acme_weekly" / "manifests" / "issue_007.json"
    manifest_path.write_text("{malformed", encoding="utf-8")

    events = build_audit_timeline("acme", programs_root=programs_root)

    archive_events = [event for event in events if event.category == "archive"]

    assert len(archive_events) == 1
    assert archive_events[0].reference == "issue_007"
    assert archive_events[0].summary == "Archived issue 007"


def test_audit_archive_cli_supports_human_json_and_csv(monkeypatch, tmp_path: Path) -> None:
    def _seed_archive_case(case_root: Path) -> Path:
        programs_root = _seed_audit_workspace(case_root)
        monkeypatch.setattr("src.commands.audit.PROGRAMS_ROOT", programs_root)
        append_autonomy_audit_record(
            AutonomyAuditRecord(
                program_id="acme",
                action_id="action-older",
                level="l2",
                author_alias="operator",
                subject_alias="alex",
                evidence_refs=("WI:1001",),
                policy_rule="vitality_nudge",
                accepted=True,
                applied_at=datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc),
            ),
            programs_root=programs_root,
        )
        append_autonomy_audit_record(
            AutonomyAuditRecord(
                program_id="acme",
                action_id="action-current",
                level="l2",
                author_alias="operator",
                subject_alias="alex",
                evidence_refs=("WI:1002",),
                policy_rule="vitality_nudge",
                accepted=False,
                applied_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            ),
            programs_root=programs_root,
        )
        return programs_root

    human_programs_root = _seed_archive_case(tmp_path / "human")
    human_result = runner.invoke(app, ["audit", "archive", "--program", "acme", "--before", "2026-05-01"])
    human_archive_path = get_program_autonomy_audit_archive_path("acme", 2026, programs_root=human_programs_root)

    assert human_result.exit_code == 0
    assert "Archived 1 autonomy audit row(s) for acme before 2026-05-01." in human_result.stdout
    assert "Remaining active autonomy audit rows: 1" in human_result.stdout
    assert str(human_archive_path) in human_result.stdout

    json_programs_root = _seed_archive_case(tmp_path / "json")
    json_result = runner.invoke(app, ["audit", "archive", "--program", "acme", "--before", "2026-05-01", "--format", "json"])
    json_archive_path = get_program_autonomy_audit_archive_path("acme", 2026, programs_root=json_programs_root)

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload == {
        "archive_paths": [str(json_archive_path)],
        "archived_count": 1,
        "before_date": "2026-05-01",
        "program_id": "acme",
        "remaining_count": 1,
    }

    csv_programs_root = _seed_archive_case(tmp_path / "csv")
    csv_result = runner.invoke(app, ["audit", "archive", "--program", "acme", "--before", "2026-05-01", "--format", "csv"])
    csv_archive_path = get_program_autonomy_audit_archive_path("acme", 2026, programs_root=csv_programs_root)

    assert csv_result.exit_code == 0
    rows = csv_result.stdout.strip().splitlines()
    assert rows[0] == "row_type,program_id,before_date,archived_count,remaining_count,path"
    assert rows[1] == "summary,acme,2026-05-01,1,1,"
    assert rows[2] == f"path,acme,2026-05-01,,,{csv_archive_path}"

    archived_payloads = [
        json.loads(line) for line in csv_archive_path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert [payload["action_id"] for payload in archived_payloads] == ["action-older"]

    active_payloads = [
        json.loads(line)
        for line in get_program_autonomy_audit_path("acme", programs_root=csv_programs_root).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [payload["action_id"] for payload in active_payloads] == ["action-current"]


def test_audit_archive_cli_retention_uses_program_config(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_audit_workspace(tmp_path)
    monkeypatch.setattr("src.commands.audit.PROGRAMS_ROOT", programs_root)
    (programs_root / "acme" / "program.yaml").write_text(
        "\n".join(
            (
                "schema_version: '3.0'",
                "id: acme",
                "name: Acme",
                "audit:",
                "  retention_days: 30",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    now = datetime.now(timezone.utc)
    older_at = now - timedelta(days=31)
    newer_at = now - timedelta(days=5)
    append_autonomy_audit_record(
        AutonomyAuditRecord(
            program_id="acme",
            action_id="action-older",
            level="l2",
            author_alias="operator",
            subject_alias="alex",
            evidence_refs=("WI:1001",),
            policy_rule="vitality_nudge",
            accepted=True,
            applied_at=older_at,
        ),
        programs_root=programs_root,
    )
    append_autonomy_audit_record(
        AutonomyAuditRecord(
            program_id="acme",
            action_id="action-current",
            level="l2",
            author_alias="operator",
            subject_alias="alex",
            evidence_refs=("WI:1002",),
            policy_rule="vitality_nudge",
            accepted=False,
            applied_at=newer_at,
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(app, ["audit", "archive", "--program", "acme", "--retention"])

    archive_path = get_program_autonomy_audit_archive_path("acme", older_at.year, programs_root=programs_root)
    assert result.exit_code == 0
    assert "Archived 1 autonomy audit row(s) for acme using configured retention 30 day(s)." in result.stdout
    assert "Remaining active autonomy audit rows: 1" in result.stdout
    assert str(archive_path) in result.stdout

    archived_payloads = [json.loads(line) for line in archive_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [payload["action_id"] for payload in archived_payloads] == ["action-older"]

    active_payloads = [
        json.loads(line)
        for line in get_program_autonomy_audit_path("acme", programs_root=programs_root).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [payload["action_id"] for payload in active_payloads] == ["action-current"]


def test_audit_pause_cli_disables_batch_rule_and_records_audit(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_audit_workspace(tmp_path)
    monkeypatch.setattr("src.commands.audit.PROGRAMS_ROOT", programs_root)
    append_autonomy_audit_record(
        AutonomyAuditRecord(
            program_id="acme",
            action_id="prior-vitality-accept",
            level="l2",
            author_alias="operator",
            subject_alias=None,
            evidence_refs=("WI:1001",),
            policy_rule="approval:vitality_nudge",
            accepted=True,
            applied_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            action_type="vitality_nudge",
        ),
        programs_root=programs_root,
    )
    _write_signal_approval_rules(
        programs_root,
        rules=(
            {
                "rule_id": "approval:vitality_nudge",
                "action_type": "vitality_nudge",
                "recommended_mode": "batch_approval",
                "promoted_by": "tester",
            },
            {
                "rule_id": "approval:comment",
                "action_type": "comment",
                "recommended_mode": "proposal_staging",
                "promoted_by": "reviewer",
            },
        ),
    )

    result = runner.invoke(app, ["audit", "pause", "--program", "acme", "--action-type", "vitality_nudge", "--updated-by", "owner"])

    assert result.exit_code == 0
    assert "Paused batch approval for vitality_nudge in acme." in result.stdout
    assert "Paused rule(s): approval:vitality_nudge" in result.stdout

    remaining_rules = load_promoted_signal_approval_rules("acme", programs_root=programs_root)
    assert [rule.proposal.rule_id for rule in remaining_rules] == ["approval:comment"]

    payloads = [
        json.loads(line)
        for line in get_program_autonomy_audit_path("acme", programs_root=programs_root).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    pause_payload = payloads[-1]
    assert pause_payload["action_type"] == "policy_paused"
    assert pause_payload["author_alias"] == "owner"
    assert pause_payload["policy_rule"] == "approval:vitality_nudge"
    assert pause_payload["prior_acceptance_rate"] == 1.0
    assert pause_payload["evidence_refs"] == [
        "action_type:vitality_nudge",
        "signal_approval_rule:approval:vitality_nudge",
    ]
    assert "future auto-apply triggers halted" in pause_payload["blast_radius"]
    assert "vertex policy promote --rule <id>" in pause_payload["rollback_mechanism"]


def test_audit_rollback_cli_rolls_back_proposal_backed_action_and_records_governance(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_audit_workspace(tmp_path)
    monkeypatch.setattr("src.commands.audit.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.audit.PROGRAMS_ROOT", programs_root)
    (programs_root / "acme" / "program.yaml").write_text(
        "\n".join(
            (
                "schema_version: '2.0'",
                "id: acme",
                "name: Acme",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    append_autonomy_audit_record(
        AutonomyAuditRecord(
            program_id="acme",
            action_id="action-apply",
            level="l2",
            author_alias="operator",
            subject_alias=None,
            evidence_refs=("ado_proposal:prop-demo", "WI:1001", "WI:1002"),
            policy_rule="approval:vitality_nudge",
            accepted=True,
            applied_at=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
            action_type="vitality_nudge",
        ),
        programs_root=programs_root,
    )
    write_proposal_manifest(
        ADOUpdateProposal(
            id="prop-demo",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=7,
            update_type="vitality_nudge",
            created_at=datetime(2026, 5, 2, 10, 0, tzinfo=timezone.utc),
            expires_at=datetime(2026, 5, 5, 10, 0, tzinfo=timezone.utc),
            entries=(
                ADOUpdateEntry(
                    work_item_id=1001,
                    action="add_comment",
                    field_or_tag="comment",
                    current_value=None,
                    proposed_value="Vertex demo_weekly issue #007\nRisk: high.",
                    reason="Cited in confirmed issue #007.",
                    revision_id=7,
                    entry_status="applied",
                ),
                ADOUpdateEntry(
                    work_item_id=1002,
                    action="set_field",
                    field_or_tag="Custom.RiskLevel",
                    current_value="high",
                    proposed_value="medium",
                    reason="Sync Vertex override.",
                    revision_id=8,
                    entry_status="applied",
                ),
            ),
        ),
        programs_root=programs_root,
    )

    fake_client = _AuditRollbackFakeADOClient(
        rows_by_id={
            1002: {"rev": 10, "fields": {"System.Id": 1002, "System.Rev": 10, "Custom.RiskLevel": "medium"}},
        },
        comments_by_id={1001: [{"id": 901, "text": "Vertex demo_weekly issue #007\nRisk: high."}]},
    )
    monkeypatch.setattr(
        "src.commands.audit.gather_command_helpers._load_program_context",
        lambda program_id, programs_root: (
            SimpleNamespace(
                id=program_id,
                ado=SimpleNamespace(organization="your-org", project="One", api_timeout_seconds=30),
            ),
            (),
        ),
    )
    monkeypatch.setattr("src.commands.audit._build_ado_client_for_program", lambda program: fake_client)

    result = runner.invoke(app, ["audit", "rollback", "--action", "action-apply", "--updated-by", "owner"])

    assert result.exit_code == 0
    assert "Rolled back action action-apply for acme: 2 rolled back, 0 skipped, 0 conflict, 0 failed." in result.stdout
    assert fake_client.rows_by_id[1002]["fields"]["Custom.RiskLevel"] == "high"
    assert fake_client.comments_by_id[1001][-1]["text"].startswith("Vertex rollback action-apply")

    payloads = [
        json.loads(line)
        for line in get_program_autonomy_audit_path("acme", programs_root=programs_root).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rollback_payload = payloads[-1]
    assert rollback_payload["action_type"] == "rollback"
    assert rollback_payload["author_alias"] == "owner"
    assert rollback_payload["policy_rule"] == "approval:vitality_nudge"
    assert rollback_payload["prior_acceptance_rate"] == 1.0
    assert rollback_payload["evidence_refs"][:2] == ["original_action:action-apply", "ado_proposal:prop-demo"]
    assert "Rolled back 2 ADO update(s) from action action-apply" in rollback_payload["blast_radius"]


def test_audit_rollback_cli_dry_run_previews_without_external_write(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_audit_workspace(tmp_path)
    monkeypatch.setattr("src.commands.audit.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.audit.PROGRAMS_ROOT", programs_root)
    (programs_root / "acme" / "program.yaml").write_text("schema_version: '2.0'\nid: acme\nname: Acme\n", encoding="utf-8")
    append_autonomy_audit_record(
        AutonomyAuditRecord(
            program_id="acme",
            action_id="action-preview",
            level="l2",
            author_alias="operator",
            subject_alias=None,
            evidence_refs=("ado_proposal:prop-preview",),
            policy_rule=None,
            accepted=True,
            applied_at=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
            action_type="comment",
        ),
        programs_root=programs_root,
    )
    write_proposal_manifest(
        ADOUpdateProposal(
            id="prop-preview",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=7,
            update_type="comment",
            created_at=datetime(2026, 5, 2, 10, 0, tzinfo=timezone.utc),
            expires_at=datetime(2026, 5, 5, 10, 0, tzinfo=timezone.utc),
            entries=(
                ADOUpdateEntry(
                    work_item_id=1001,
                    action="add_comment",
                    field_or_tag="comment",
                    current_value=None,
                    proposed_value="Vertex demo_weekly issue #007\nRisk: high.",
                    reason="Cited in confirmed issue #007.",
                    revision_id=7,
                    entry_status="applied",
                ),
            ),
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(app, ["audit", "rollback", "--action", "action-preview", "--dry-run"])

    assert result.exit_code == 0
    assert "Dry-run: would roll back 1 applied ADO update(s) from proposal prop-preview for action action-preview." in result.stdout
    payloads = [
        json.loads(line)
        for line in get_program_autonomy_audit_path("acme", programs_root=programs_root).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [payload["action_id"] for payload in payloads] == ["action-preview"]


def test_audit_rollback_cli_batch_rolls_back_same_day_actions_and_records_each_governance_row(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_audit_workspace(tmp_path)
    monkeypatch.setattr("src.commands.audit.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.audit.PROGRAMS_ROOT", programs_root)
    (programs_root / "acme" / "program.yaml").write_text("schema_version: '2.0'\nid: acme\nname: Acme\n", encoding="utf-8")
    for action_id, proposal_id, applied_at in (
        ("action-batch-a", "prop-batch-a", datetime(2026, 5, 2, 9, 0, tzinfo=timezone.utc)),
        ("action-batch-b", "prop-batch-b", datetime(2026, 5, 2, 11, 0, tzinfo=timezone.utc)),
        ("action-other-day", "prop-other-day", datetime(2026, 5, 3, 11, 0, tzinfo=timezone.utc)),
    ):
        append_autonomy_audit_record(
            AutonomyAuditRecord(
                program_id="acme",
                action_id=action_id,
                level="l2",
                author_alias="operator",
                subject_alias=None,
                evidence_refs=(f"ado_proposal:{proposal_id}",),
                policy_rule="approval:vitality_nudge",
                accepted=True,
                applied_at=applied_at,
                action_type="vitality_nudge",
            ),
            programs_root=programs_root,
        )
    for proposal_id, work_item_id, current_value, proposed_value in (
        ("prop-batch-a", 2001, "high", "medium"),
        ("prop-batch-b", 2002, "medium", "low"),
        ("prop-other-day", 2003, "low", "high"),
    ):
        write_proposal_manifest(
            ADOUpdateProposal(
                id=proposal_id,
                program_id="acme",
                edition_id="acme_weekly",
                issue_number=7,
                update_type="vitality_nudge",
                created_at=datetime(2026, 5, 2, 8, 0, tzinfo=timezone.utc),
                expires_at=datetime(2026, 5, 5, 8, 0, tzinfo=timezone.utc),
                entries=(
                    ADOUpdateEntry(
                        work_item_id=work_item_id,
                        action="set_field",
                        field_or_tag="Custom.RiskLevel",
                        current_value=current_value,
                        proposed_value=proposed_value,
                        reason="Sync Vertex override.",
                        revision_id=7,
                        entry_status="applied",
                    ),
                ),
            ),
            programs_root=programs_root,
        )

    fake_client = _AuditRollbackFakeADOClient(
        rows_by_id={
            2001: {"rev": 10, "fields": {"System.Id": 2001, "System.Rev": 10, "Custom.RiskLevel": "medium"}},
            2002: {"rev": 11, "fields": {"System.Id": 2002, "System.Rev": 11, "Custom.RiskLevel": "low"}},
            2003: {"rev": 12, "fields": {"System.Id": 2003, "System.Rev": 12, "Custom.RiskLevel": "high"}},
        },
        comments_by_id={},
    )
    monkeypatch.setattr(
        "src.commands.audit.gather_command_helpers._load_program_context",
        lambda program_id, programs_root: (
            SimpleNamespace(
                id=program_id,
                ado=SimpleNamespace(organization="your-org", project="One", api_timeout_seconds=30),
            ),
            (),
        ),
    )
    monkeypatch.setattr("src.commands.audit._build_ado_client_for_program", lambda program: fake_client)

    result = runner.invoke(app, ["audit", "rollback", "--program", "acme", "--batch", "2026-05-02", "--updated-by", "owner"])

    assert result.exit_code == 0
    assert "Rolled back batch 2026-05-02 for acme: 2 action(s), 2 rolled back, 0 skipped, 0 conflict, 0 failed." in result.stdout
    assert fake_client.rows_by_id[2001]["fields"]["Custom.RiskLevel"] == "high"
    assert fake_client.rows_by_id[2002]["fields"]["Custom.RiskLevel"] == "medium"
    assert fake_client.rows_by_id[2003]["fields"]["Custom.RiskLevel"] == "high"

    payloads = [
        json.loads(line)
        for line in get_program_autonomy_audit_path("acme", programs_root=programs_root).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rollback_payloads = [payload for payload in payloads if payload["action_type"] == "rollback"]
    assert len(rollback_payloads) == 2
    assert {payload["evidence_refs"][0] for payload in rollback_payloads} == {
        "original_action:action-batch-a",
        "original_action:action-batch-b",
    }


def test_audit_rollback_cli_batch_dry_run_previews_same_day_actions(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_audit_workspace(tmp_path)
    monkeypatch.setattr("src.commands.audit.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.audit.PROGRAMS_ROOT", programs_root)
    (programs_root / "acme" / "program.yaml").write_text("schema_version: '2.0'\nid: acme\nname: Acme\n", encoding="utf-8")
    append_autonomy_audit_record(
        AutonomyAuditRecord(
            program_id="acme",
            action_id="action-batch-preview",
            level="l2",
            author_alias="operator",
            subject_alias=None,
            evidence_refs=("ado_proposal:prop-batch-preview",),
            policy_rule=None,
            accepted=True,
            applied_at=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
            action_type="comment",
        ),
        programs_root=programs_root,
    )
    write_proposal_manifest(
        ADOUpdateProposal(
            id="prop-batch-preview",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=7,
            update_type="comment",
            created_at=datetime(2026, 5, 2, 10, 0, tzinfo=timezone.utc),
            expires_at=datetime(2026, 5, 5, 10, 0, tzinfo=timezone.utc),
            entries=(
                ADOUpdateEntry(
                    work_item_id=1001,
                    action="add_comment",
                    field_or_tag="comment",
                    current_value=None,
                    proposed_value="Vertex demo_weekly issue #007\nRisk: high.",
                    reason="Cited in confirmed issue #007.",
                    revision_id=7,
                    entry_status="applied",
                ),
            ),
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(app, ["audit", "rollback", "--program", "acme", "--batch", "2026-05-02", "--dry-run"])

    assert result.exit_code == 0
    assert "Dry-run: would roll back 1 autonomy action(s) applied on 2026-05-02 in acme." in result.stdout
    assert "- action-batch-preview | proposal prop-batch-preview | 1 applied update(s)" in result.stdout
    payloads = [
        json.loads(line)
        for line in get_program_autonomy_audit_path("acme", programs_root=programs_root).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [payload["action_id"] for payload in payloads] == ["action-batch-preview"]


def _seed_audit_workspace(tmp_path: Path) -> Path:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    (program_dir / "archive" / "acme_weekly" / "manifests").mkdir(parents=True, exist_ok=True)

    append_signal(
        Signal(
            id="signal-1",
            timestamp=FROZEN_SIGNAL_AT,
            source="ado/revision",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:1001",),
            text="Deployment update received",
            raw_ref="WI:1001",
            confidence=Confidence.HIGH,
            metadata=None,
            thread_id=None,
        ),
        programs_root=programs_root,
        partition_at=FROZEN_SIGNAL_AT,
    )
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="signal-1",
            decision="approved",
            reviewed_at=FROZEN_REVIEW_AT,
            reviewed_by="owner",
            note="Looks good",
        ),
        programs_root=programs_root,
    )
    append_usage_marker(
        "acme",
        SignalUsageMarker(
            signal_id="signal-1",
            issue_number=7,
            edition_id="acme_weekly",
            manifest_id="manifest-007",
            used_at=FROZEN_USAGE_AT,
        ),
        programs_root=programs_root,
    )
    append_edit_patterns(
        "acme",
        build_edit_patterns(
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=7,
            recorded_at=FROZEN_EDIT_PATTERN_AT,
            draft_exec_summary_text="AI exec summary draft remains broad.",
            confirmed_exec_summary_text="Confirmed exec summary is tighter and action-oriented.",
            draft_workstream_blurbs={},
            confirmed_workstream_blurbs={},
            draft_prompt_versions={"exec_summary": "exec_summary_drafter.v1"},
            draft_ai_confidences={"exec_summary": Confidence.MEDIUM.value},
            draft_trace_run_id=FROZEN_TRACE_RUN_ID_ISSUE_007,
        ),
        programs_root=programs_root,
    )

    manifest_path = program_dir / "archive" / "acme_weekly" / "manifests" / "issue_007.json"
    manifest_path.write_text(
        json.dumps(
            {
                "manifest_id": "manifest-007",
                "freshness_summary": {"blocks": 0, "warns": 1, "infos": 2},
                "qg_results": {"QG-1": True, "QG-4": True, "QG-8": False},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (program_dir / "archive" / "acme_weekly" / "index.json").write_text(
        json.dumps(
            {
                "edition": "acme_weekly",
                "issues": [
                    {
                        "issue_number": 7,
                        "generated_at": FROZEN_ARCHIVE_AT.isoformat(),
                        "kind": "confirmed",
                        "manifest_path": str(manifest_path),
                        "html_path": None,
                        "md_path": None,
                        "snapshot_path": None,
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return programs_root


def _write_signal_approval_rules(programs_root: Path, *, rules: tuple[dict[str, str], ...]) -> None:
    path = programs_root / "acme" / "_feedback" / "signal_approval_rules.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        'schema_version: "1.0"',
        'updated_at: "2026-05-20T00:00:00+00:00"',
        "proposals: []",
        "rules:",
    ]
    for rule in rules:
        lines.extend(
            (
                f"  - rule_id: {rule['rule_id']}",
                f"    action_type: {rule['action_type']}",
                f"    label: {rule['action_type']}",
                "    sample_count: 12",
                "    accepted_count: 11",
                "    acceptance_rate: 0.9167",
                "    average_prior_acceptance_rate: 0.9",
                "    bootstrap: false",
                "    recommended_level: l2",
                f"    recommended_mode: {rule['recommended_mode']}",
                '    rationale: "Eligible for batch approval."',
                "    required_acceptance_rate: 0.7",
                '    promoted_at: "2026-05-20T00:00:00+00:00"',
                f"    promoted_by: {rule['promoted_by']}",
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class _AuditRollbackFakeADOClient:
    def __init__(self, *, rows_by_id: dict[int, dict[str, object]], comments_by_id: dict[int, list[dict[str, object]]]) -> None:
        self.rows_by_id = rows_by_id
        self.comments_by_id = comments_by_id
        self.timeout = 30
        self._rest_base_url = "https://dev.azure.com/your-org/One/_apis/wit/"
        self._session = _AuditRollbackFakeSession(self)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer fake", "Accept": "application/json", "Content-Type": "application/json"}

    def query_work_items_batch(self, work_item_ids: list[int], fields: tuple[str, ...]) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for work_item_id in work_item_ids:
            stored = self.rows_by_id.get(work_item_id)
            if stored is None:
                continue
            all_fields = dict(stored.get("fields", {}))
            rows.append(
                {
                    "id": work_item_id,
                    "rev": stored.get("rev"),
                    "fields": {field: all_fields[field] for field in fields if field in all_fields},
                }
            )
        return rows

    def list_work_item_comments(self, work_item_id: int) -> list[dict[str, object]]:
        return list(self.comments_by_id.get(work_item_id, ()))


class _AuditRollbackFakeSession:
    def __init__(self, client: _AuditRollbackFakeADOClient) -> None:
        self.client = client
        self.calls: list[dict[str, object]] = []
        self._next_comment_id = 20000

    def request(self, method: str, url: str, **kwargs: object) -> _AuditRollbackFakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        work_item_id = int(url.split("/workItems/")[1].split("/")[0].split("?")[0])
        if method == "POST" and url.endswith("comments?api-version=7.1-preview.4"):
            text = str(kwargs["json"]["text"])
            comment_id = self._next_comment_id
            self._next_comment_id += 1
            self.client.comments_by_id.setdefault(work_item_id, []).append({"id": comment_id, "text": text})
            return _AuditRollbackFakeResponse({"id": comment_id, "text": text})
        if method == "PATCH" and url.endswith("?api-version=7.1"):
            for op in kwargs["json"]:
                if op["op"] == "test":
                    continue
                field_name = str(op["path"]).removeprefix("/fields/")
                if op["op"] == "remove":
                    self.client.rows_by_id[work_item_id].setdefault("fields", {}).pop(field_name, None)
                else:
                    self.client.rows_by_id[work_item_id].setdefault("fields", {})[field_name] = op.get("value")
            return _AuditRollbackFakeResponse({"id": work_item_id})
        raise AssertionError(f"Unexpected fake request: {method} {url}")


class _AuditRollbackFakeResponse:
    def __init__(self, payload: dict[str, object], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self) -> dict[str, object]:
        return self._payload


def _append_trace_records(programs_root: Path, edition_id: str, records: tuple[dict[str, object], ...]) -> None:
    trace_path = programs_root / edition_id / "publications" / edition_id / "ai" / "llm_trace.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    with trace_path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
