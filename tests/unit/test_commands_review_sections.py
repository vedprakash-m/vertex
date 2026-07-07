from __future__ import annotations

from datetime import date
from datetime import datetime, timezone
import json
from pathlib import Path

from typer.testing import CliRunner

from cli import app
from src.commands.review_full import prepare_review_full_context
from src.commands.review_sections import _sanitize_section_filename_component
from src.core.action_tracker import append_action, build_action_id
from src.core.journal import append_review_decision, append_signal
from src.core.models_v2 import ActionItem, ActionSourceType, ActionStatus
from src.core.models_v2 import Confidence, Signal, SignalReviewDecision
import src.core.archive_store as archive_store
from src.commands.report import generate_report_draft
from src.core.review_status_store import load_review_status
from tests.unit.test_commands_report import _lookback_snapshot, _manifest, _sample_items, _seed_v2_report_layout, _snapshot_item_from_work_item, _stable_low_risk_items


runner = CliRunner()
EDITION_NAME = "acme_weekly"


def test_review_sections_set_updates_active_review_status(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    monkeypatch.setattr("src.commands.review_sections.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.review_sections.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.review_sections.PROGRAMS_ROOT", tmp_path / "programs")

    result = runner.invoke(
        app,
        [
            "review-sections",
            "set",
            "--edition",
            EDITION_NAME,
            "--section",
            "exec_summary",
            "--state",
            "approved",
            "--note",
            "LGTM",
        ],
    )

    assert result.exit_code == 0
    review_status = load_review_status(EDITION_NAME, reports_root=reports_root)
    assert review_status is not None
    exec_summary = next(section for section in review_status.sections if section.section_id == "exec_summary")
    assert exec_summary.state.value == "approved"
    assert exec_summary.note == "LGTM"
    assert exec_summary.manifest_id is not None


def test_review_sections_show_requires_seeded_review_status(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    monkeypatch.setattr("src.commands.review_sections.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.review_sections.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.review_sections.PROGRAMS_ROOT", tmp_path / "programs")

    result = runner.invoke(app, ["review-sections", "show", "--edition", EDITION_NAME])

    assert result.exit_code == 2


def test_review_sections_show_keeps_continuity_chapter_pending_without_delta(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    previous_as_of = datetime(2026, 5, 4, 18, 0, tzinfo=timezone.utc)
    current_as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    stable_previous = _stable_low_risk_items(previous_as_of)[0]
    archive_store.write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=_lookback_snapshot(
            issue_number=1,
            as_of=previous_as_of,
            items=(
                _snapshot_item_from_work_item(stable_previous, risk_level=stable_previous.risk_level),
            ),
            scorecard_risks={"Deployment Velocity": stable_previous.risk_level},
        ),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001\n",
        manifest=_manifest(issue_number=1, as_of=previous_as_of),
        archive_root=archive_root,
    )

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=current_as_of,
        work_item_loader=lambda bundle, timestamp: (_stable_low_risk_items(timestamp), 0),
        open_browser=False,
    )

    monkeypatch.setattr("src.commands.review_sections.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.review_sections.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.review_sections.PROGRAMS_ROOT", tmp_path / "programs")

    result = runner.invoke(app, ["review-sections", "show", "--edition", EDITION_NAME])

    assert result.exit_code == 0
    assert "ws:deployment_readiness: pending" in result.stdout


def test_review_sections_set_surfaces_malformed_active_draft_manifest(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )
    artifacts.manifest_path.write_text("{", encoding="utf-8")

    monkeypatch.setattr("src.commands.review_sections.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.review_sections.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.review_sections.PROGRAMS_ROOT", tmp_path / "programs")

    result = runner.invoke(
        app,
        [
            "review-sections",
            "set",
            "--edition",
            EDITION_NAME,
            "--section",
            "exec_summary",
            "--state",
            "approved",
        ],
    )

    assert result.exit_code == 2
    assert f"Manifest at {artifacts.manifest_path} is invalid." in result.stdout


def test_review_sections_export_writes_section_only_html(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    detail_section_id = next(
        section.section_id
        for section in prepare_review_full_context(
            edition_name=EDITION_NAME,
            issue_number=1,
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=programs_root,
        ).reviewer_context.sections
        if section.section_id.startswith("ws:")
    )

    append_action(
        "acme",
        ActionItem(
            id=build_action_id(
                "acme",
                text="Clarify deployment mitigation with engineering.",
                owner_alias="operator",
                due_date=date(2026, 5, 8),
                source_signal_id=None,
                workstream_id=detail_section_id.removeprefix("ws:"),
                linked_work_item_ids=(101,),
            ),
            program_id="acme",
            text="Clarify deployment mitigation with engineering.",
            owner_alias="operator",
            due_date=date(2026, 5, 8),
            status=ActionStatus.OPEN,
            source_signal_id=None,
            source_type=ActionSourceType.MANUAL,
            linked_work_item_ids=(101,),
            linked_claim_id=None,
            linked_risk_id=None,
            workstream_id=detail_section_id.removeprefix("ws:"),
            created_at=datetime(2026, 5, 5, 18, 10, tzinfo=timezone.utc),
            resolved_at=None,
            resolution_note=None,
        ),
        programs_root=reports_root.parent / "programs",
    )
    for signal in (
        Signal(
            id="telemetry-analytics",
            timestamp=datetime(2026, 5, 5, 17, 0, tzinfo=timezone.utc),
            source="ado/analytics",
            program_id="acme",
            workstream_id=detail_section_id.removeprefix("ws:"),
            entity_refs=("WI:101",),
            text="Analytics snapshot for review section telemetry.",
            raw_ref="ado-analytics:telemetry-analytics",
            confidence=Confidence.HIGH,
            metadata={
                "snapshot_item_count": 5,
                "completed_item_count": 2,
                "scope_delta_count": 2,
                "open_delta_count": -1,
                "average_cycle_time_days": 5.0,
                "average_lead_time_days": 8.0,
            },
            thread_id=None,
        ),
        Signal(
            id="telemetry-sprint",
            timestamp=datetime(2026, 5, 5, 17, 15, tzinfo=timezone.utc),
            source="ado/sprint",
            program_id="acme",
            workstream_id=detail_section_id.removeprefix("ws:"),
            entity_refs=("WI:101",),
            text="Sprint snapshot for review section telemetry.",
            raw_ref="ado-sprint:telemetry-sprint",
            confidence=Confidence.HIGH,
            metadata={
                "iteration_name": "Sprint 24",
                "completion_pct": 50,
                "open_item_count": 1,
                "team_member_count": 3,
                "total_capacity_per_day": 24.0,
            },
            thread_id=None,
        ),
        Signal(
            id="context-signal",
            timestamp=datetime(2026, 5, 5, 17, 20, tzinfo=timezone.utc),
            source="ado/revision",
            program_id="acme",
            workstream_id=detail_section_id.removeprefix("ws:"),
            entity_refs=("WI:900002",),
            text="Context signal for review section portal.",
            raw_ref="ado:900002:revision-3",
            confidence=Confidence.HIGH,
            metadata=None,
            thread_id=None,
        ),
    ):
        append_signal(signal, programs_root=reports_root.parent / "programs", partition_at=signal.timestamp)
        append_review_decision(
            "acme",
            SignalReviewDecision(
                signal_id=signal.id,
                decision="approved",
                reviewed_at=signal.timestamp,
                reviewed_by="system",
                note=None,
            ),
            programs_root=reports_root.parent / "programs",
        )

    monkeypatch.setattr("src.commands.review_sections.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.review_sections.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.review_sections.PROGRAMS_ROOT", tmp_path / "programs")

    result = runner.invoke(
        app,
        [
            "review-sections",
            "export",
            "--edition",
            EDITION_NAME,
            "--section",
            detail_section_id,
            "--no-open",
        ],
    )

    exported_path = programs_root / "acme" / "publications" / EDITION_NAME / "review" / "sections" / f"issue_001.{_sanitize_section_filename_component(detail_section_id)}.html"
    assert result.exit_code == 0
    assert exported_path.exists()
    html = exported_path.read_text(encoding="utf-8")
    assert "DRI Self-Service" in html
    assert "Remediation Actions" in html
    assert "Vitality Scores" in html
    assert "Telemetry" in html
    assert "Latest approved telemetry" in html
    assert "analytics, 5 scope, 2 completed, scope up 2, open down 1, cycle 5.0d / lead 8.0d; sprint, Sprint 24, 50% complete, 1 open, team cap 24.0h/day across 3 members" in html
    assert "Context" in html
    assert "Why This Section Matters" in html
    assert 'href="https://dev.azure.com/your-org/One/_workitems/edit/101"' in html
    assert 'href="https://dev.azure.com/your-org/One/_workitems/edit/900002"' in html
    assert '<a href="https://dev.azure.com/your-org/One/_workitems/edit/900002">Signal · ado/revision · High</a>' in html
    assert 'href="https://dev.azure.com/your-org/One/_queries/query' in html
    assert 'ADO query</a>' in html
    assert 'View evidence in ADO' in html
    assert "Clarify deployment mitigation with engineering." in html
    assert "Executive Summary" not in html


def test_review_sections_export_rejects_non_workstream_section(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    monkeypatch.setattr("src.commands.review_sections.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.review_sections.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.review_sections.PROGRAMS_ROOT", tmp_path / "programs")

    result = runner.invoke(
        app,
        [
            "review-sections",
            "export",
            "--edition",
            EDITION_NAME,
            "--section",
            "exec_summary",
            "--no-open",
        ],
    )

    assert result.exit_code == 2
    assert "only supports workstream section ids" in result.stdout


def test_review_sections_export_surfaces_snapshot_backed_broader_historical_sprint_window(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    detail_section_id = next(
        section.section_id
        for section in prepare_review_full_context(
            edition_name=EDITION_NAME,
            issue_number=1,
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=programs_root,
        ).reviewer_context.sections
        if section.section_id.startswith("ws:")
    )

    append_signal(
        Signal(
            id="telemetry-sprint",
            timestamp=datetime(2026, 5, 5, 17, 15, tzinfo=timezone.utc),
            source="ado/sprint",
            program_id="acme",
            workstream_id=detail_section_id.removeprefix("ws:"),
            entity_refs=("WI:101",),
            text="Sprint snapshot for review section telemetry.",
            raw_ref="ado-sprint:telemetry-sprint",
            confidence=Confidence.HIGH,
            metadata={
                "iteration_name": "Sprint 24",
                "completion_pct": 100,
                "open_item_count": 0,
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
        ),
        programs_root=reports_root.parent / "programs",
        partition_at=datetime(2026, 5, 5, 17, 15, tzinfo=timezone.utc),
    )
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="telemetry-sprint",
            decision="approved",
            reviewed_at=datetime(2026, 5, 5, 17, 15, tzinfo=timezone.utc),
            reviewed_by="system",
            note=None,
        ),
        programs_root=reports_root.parent / "programs",
    )

    monkeypatch.setattr("src.commands.review_sections.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.review_sections.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.review_sections.PROGRAMS_ROOT", tmp_path / "programs")

    result = runner.invoke(
        app,
        [
            "review-sections",
            "export",
            "--edition",
            EDITION_NAME,
            "--section",
            detail_section_id,
            "--no-open",
        ],
    )

    exported_path = programs_root / "acme" / "publications" / EDITION_NAME / "review" / "sections" / f"issue_001.{_sanitize_section_filename_component(detail_section_id)}.html"
    assert result.exit_code == 0
    assert exported_path.exists()
    html = exported_path.read_text(encoding="utf-8")
    assert "Latest approved telemetry" in html
    assert (
        "sprint, Sprint 24, 100% complete, 0 open, 4-sprint throughput 1.0->0.5->1.0->1.5/day, 4-sprint open 1->2->1->0, 4-sprint burndown 3->2->1 | 3->2->2 | 3->1->1 | 3->1->0 open, 4-sprint completion 0->1->2 | 0->1->1 | 0->2->2 | 0->2->3 done"
        in html
    )


def test_review_sections_show_supports_json_and_csv(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    monkeypatch.setattr("src.commands.review_sections.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.review_sections.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.review_sections.PROGRAMS_ROOT", tmp_path / "programs")

    json_result = runner.invoke(app, ["review-sections", "show", "--edition", EDITION_NAME, "--format", "json"])

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["edition_name"] == EDITION_NAME
    assert payload["issue_number"] == 1
    assert payload["source_path"].endswith(f"{EDITION_NAME}\\review_status.yaml")
    assert any(section["section_id"] == "exec_summary" for section in payload["sections"])

    csv_result = runner.invoke(app, ["review-sections", "show", "--edition", EDITION_NAME, "--format", "csv"])

    assert csv_result.exit_code == 0
    lines = csv_result.stdout.strip().splitlines()
    assert lines[0] == "edition_name,issue_number,source_path,section_id,state,display_state,reviewer,updated_at,note"
    assert any(",exec_summary," in line for line in lines[1:])

