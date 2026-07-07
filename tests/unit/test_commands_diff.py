from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import json
from pathlib import Path
import shutil

import pytest
import typer
import yaml
from typer.testing import CliRunner

from cli import app
from src.commands.diff import build_offline_diff_summary
from src.commands.report import generate_report_draft
from src.core.jinja_filters import build_anchor
from src.core.narrative_store import get_narratives_dir
from src.core.overrides_store import get_overrides_path
from src.core.archive_store import write_confirmed_issue
from src.core.journal import append_review_decision, append_signal
from src.core.models import Comment, Confidence, Revision, RiskLevel, WorkItem
from src.core.models_v2 import Signal, SignalReviewDecision, TrajectoryPoint
from src.core.trajectory import append_trajectory_point
from tests.support.report_test_setup import disable_kusto_in_report_copy, stage_v2_report_workspace


runner = CliRunner()


def test_diff_cli_since_last_confirmed_renders_semantic_v2_sections(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root, archive_root, output_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    as_of_issue_1 = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    as_of_issue_2 = datetime(2026, 5, 12, 18, 0, tzinfo=timezone.utc)

    baseline = generate_report_draft(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of_issue_1,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    baseline_exec_summary = baseline.narratives_dir / "exec_summary.md"
    baseline_exec_summary.write_text("Baseline weekly summary.\n", encoding="utf-8")
    workstream_path = baseline.narratives_dir / "ws_deployment_readiness.md"
    workstream_path.write_text("Baseline deployment workstream summary.\n", encoding="utf-8")

    current = generate_report_draft(
        edition_name="acme_weekly",
        issue_number=2,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of_issue_2,
        work_item_loader=lambda bundle, timestamp: (_sample_items_with_diff(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    current_exec_summary = current.narratives_dir / "exec_summary.md"
    current_exec_summary.write_text("Updated weekly summary with rollout risk.\n", encoding="utf-8")
    current_workstream_path = current.narratives_dir / "ws_deployment_readiness.md"
    current_workstream_path.write_text("Deployment workstream updated with new safeguard.\n", encoding="utf-8")

    adjusted_snapshot = replace(
        baseline.snapshot,
        items=tuple(
            replace(item, risk_level=RiskLevel.LOW) if item.id == 900001 else item
            for item in baseline.snapshot.items
        ),
        scorecards=tuple(
            replace(scorecard, risk=RiskLevel.LOW)
            for scorecard in baseline.snapshot.scorecards
        ),
    )
    write_confirmed_issue(
        edition="acme_weekly",
        issue_number=1,
        snapshot=adjusted_snapshot,
        html_body=baseline.html_body,
        markdown_body=baseline.markdown_body,
        manifest=baseline.manifest,
        overrides_source=baseline.overrides_path,
        review_status_source=baseline.review_status_path,
        narratives_source_dir=baseline.narratives_dir,
        archive_root=archive_root,
    )

    programs_root = reports_root.parent / "programs"
    _append_approved_signal(
        programs_root=programs_root,
        signal=Signal(
            id="sig-ado-1",
            timestamp=datetime(2026, 5, 6, 9, 0, tzinfo=timezone.utc),
            source="ado/revision",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:900001",),
            text="Deployment target moved out one sprint.",
            raw_ref=None,
            confidence=Confidence.HIGH,
        ),
    )
    _append_approved_signal(
        programs_root=programs_root,
        signal=Signal(
            id="sig-kusto-1",
            timestamp=datetime(2026, 5, 7, 11, 0, tzinfo=timezone.utc),
            source="kusto",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:900004",),
            text="Telemetry shows new failure spike.",
            raw_ref=None,
            confidence=Confidence.MEDIUM,
        ),
    )

    for point in (
        TrajectoryPoint(date=date(2026, 5, 6), state="Active", assigned_to="owner-a", target_date=date(2026, 5, 10), risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme\\Deployment"),
        TrajectoryPoint(date=date(2026, 5, 7), state="Active", assigned_to="owner-b", target_date=date(2026, 5, 12), risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme\\Deployment"),
        TrajectoryPoint(date=date(2026, 5, 8), state="Active", assigned_to="owner-c", target_date=date(2026, 5, 14), risk_level=RiskLevel.HIGH, area_path="One\\Adventure\\Acme\\Deployment"),
        TrajectoryPoint(date=date(2026, 5, 9), state="Active", assigned_to="owner-d", target_date=date(2026, 5, 16), risk_level=RiskLevel.HIGH, area_path="One\\Adventure\\Acme\\Deployment"),
    ):
        append_trajectory_point("acme", 900001, point, programs_root=programs_root)

    monkeypatch.setattr("src.commands.diff.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.diff.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.diff.ARCHIVE_ROOT", archive_root)

    result = runner.invoke(app, ["diff", "--edition", "acme_weekly", "--since", "last-confirmed"])
    explicit_issue_result = runner.invoke(app, ["diff", "--edition", "acme_weekly", "--since", "issue-1"])

    assert result.exit_code == 0
    assert explicit_issue_result.exit_code == 0
    assert "VERTEX DIFF - Changes since Issue #001" in result.stdout
    assert "VERTEX DIFF - Changes since Issue #001" in explicit_issue_result.stdout
    assert "Items:" in result.stdout
    assert '+ NEW: WI:900004 "New cache warmup safeguard"' in result.stdout
    assert 'RISK UP: WI:900001 "Deployment velocity telemetry stabilization"' in result.stdout
    assert "Scorecards:" in result.stdout
    assert "Narratives:" in result.stdout
    assert f"{current_workstream_path.name}:" in result.stdout
    assert "Signals:" in result.stdout
    assert "2 new approved signals since Issue #001 (1 ADO, 1 Kusto)." in result.stdout
    assert "new drift pattern" in result.stdout


def _seed_v2_report_layout(repo_root: Path, tmp_path: Path) -> tuple[Path, Path, Path]:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    return reports_root, archive_root, (tmp_path / "programs" / "acme" / "publications")


def _sample_items(as_of: datetime) -> tuple[WorkItem, ...]:
    return (
        WorkItem(
            id=900001,
            type="Feature",
            title="Deployment velocity telemetry stabilization",
            state="Active",
            assigned_to="Vertex Maintainer",
            assigned_to_email="maintainer@example.com",
            area_path="One\\Adventure\\Acme\\Deployment",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 5, 10),
            risk_level=RiskLevel.MEDIUM,
            tags=["Safety"],
            custom_fields={},
            revisions=[
                Revision(
                    work_item_id=900001,
                    rev_number=7,
                    changed_by="Vertex Maintainer",
                    changed_by_email="maintainer@example.com",
                    changed_date=as_of,
                    fields_changed={"State": ("Proposed", "Active")},
                )
            ],
            comments=[],
            fetched_at=as_of,
        ),
        WorkItem(
            id=900002,
            type="Risk",
            title="Fleet pilot dependency on capacity allocation",
            state="At Risk",
            assigned_to="Vertex Maintainer",
            assigned_to_email="maintainer@example.com",
            area_path="One\\Adventure\\Contoso\\Networking",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 5, 8),
            risk_level=RiskLevel.HIGH,
            tags=["SCHIE"],
            custom_fields={},
            revisions=[
                Revision(
                    work_item_id=900002,
                    rev_number=3,
                    changed_by="Vertex Maintainer",
                    changed_by_email="maintainer@example.com",
                    changed_date=as_of,
                    fields_changed={"State": ("Active", "At Risk")},
                )
            ],
            comments=[
                Comment(
                    work_item_id=900002,
                    comment_id=1,
                    created_by="Vertex Maintainer",
                    created_by_email="maintainer@example.com",
                    created_date=as_of,
                    text="Capacity allocation follow-up is in progress.",
                )
            ],
            fetched_at=as_of,
        ),
        WorkItem(
            id=900003,
            type="Scenario",
            title="Pilot rollout path validation",
            state="Proposed",
            assigned_to="Vertex Maintainer",
            assigned_to_email="maintainer@example.com",
            area_path="One\\Adventure\\Fabrikam\\Acme\\Scenarios",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 5, 12),
            risk_level=RiskLevel.LOW,
            tags=[],
            custom_fields={},
            revisions=[
                Revision(
                    work_item_id=900003,
                    rev_number=2,
                    changed_by="Vertex Maintainer",
                    changed_by_email="maintainer@example.com",
                    changed_date=as_of,
                    fields_changed={"AreaPath": (None, "One\\Adventure\\Fabrikam\\Acme\\Scenarios")},
                )
            ],
            comments=[],
            fetched_at=as_of,
        ),
    )


def _sample_items_with_diff(as_of: datetime) -> tuple[WorkItem, ...]:
    baseline = _sample_items(as_of)
    return (
        replace(baseline[0], target_date=date(2026, 5, 14)),
        baseline[1],
        baseline[2],
        WorkItem(
            id=900004,
            type="Bug",
            title="New cache warmup safeguard",
            state="Active",
            assigned_to="Vertex Maintainer",
            assigned_to_email="maintainer@example.com",
            area_path="One\\Adventure\\Acme\\Deployment",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 5, 16),
            risk_level=RiskLevel.MEDIUM,
            tags=["Hotfix"],
            custom_fields={},
            revisions=[
                Revision(
                    work_item_id=900004,
                    rev_number=1,
                    changed_by="Vertex Maintainer",
                    changed_by_email="maintainer@example.com",
                    changed_date=as_of,
                    fields_changed={"State": (None, "Active")},
                )
            ],
            comments=[],
            fetched_at=as_of,
        ),
    )


def _append_approved_signal(*, programs_root: Path, signal: Signal) -> None:
    append_signal(signal, programs_root=programs_root, partition_at=signal.timestamp)
    append_review_decision(
        signal.program_id,
        SignalReviewDecision(
            signal_id=signal.id,
            decision="approved",
            reviewed_at=signal.timestamp,
            reviewed_by="test-author",
        ),
        programs_root=programs_root,
    )


def test_build_offline_diff_summary_detects_narrative_and_override_changes(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, output_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = tmp_path / "programs"

    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    generate_report_draft(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    section_id = build_anchor("Acme Adventure/XIO 100% Ramp Readiness-Deployment Velocity")
    narrative_path = get_narratives_dir("acme_weekly", 1, reports_root) / f"ws_{section_id}.md"
    narrative_path.write_text("Deployment velocity regressed after the rollout gate slipped.\n", encoding="utf-8")

    overrides_path = get_overrides_path("acme_weekly", reports_root, issue_number=1)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    overrides_payload["scorecards"]["Acme Adventure/XIO 100% Ramp Readiness"]["Deployment Velocity"]["risk"] = "high"
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    summary = build_offline_diff_summary(
        edition_name="acme_weekly",
        issue_number=1,
        section=None,
        reports_root=reports_root,
        programs_root=programs_root,
    )

    assert "VERTEX DIFF - Issue 001 vs last dry-run" in summary
    assert "Deployment Velocity (narrative changed):" in summary
    assert "Deployment Velocity (risk level changed via override):" in summary


def test_diff_cli_supports_json_and_csv(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, output_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = tmp_path / "programs"

    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    generate_report_draft(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    section_id = build_anchor("Acme Adventure/XIO 100% Ramp Readiness-Deployment Velocity")
    narrative_path = get_narratives_dir("acme_weekly", 1, reports_root) / f"ws_{section_id}.md"
    narrative_path.write_text("Deployment velocity regressed after the rollout gate slipped.\n", encoding="utf-8")

    monkeypatch.setattr("src.commands.diff.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.diff.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.diff.ARCHIVE_ROOT", archive_root)

    json_result = runner.invoke(app, ["diff", "--edition", "acme_weekly", "--format", "json"])

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["edition"] == "acme_weekly"
    assert payload["issue_number"] == 1
    assert payload["mode"] == "last-draft"
    assert payload["since"] == "last-draft"
    assert "VERTEX DIFF - Issue 001 vs last dry-run" in payload["summary"]

    csv_result = runner.invoke(app, ["diff", "--edition", "acme_weekly", "--format", "csv"])

    assert csv_result.exit_code == 0
    lines = csv_result.stdout.strip().splitlines()
    assert lines[0] == "edition,issue_number,since,section,mode,summary"
    assert "acme_weekly,1,last-draft,-,last-draft," in lines[1]
    assert "VERTEX DIFF - Issue 001 vs last dry-run" in lines[1]


def test_build_offline_diff_summary_surfaces_invalid_current_draft_state(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, output_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = tmp_path / "programs"

    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    generate_report_draft(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    draft_state_path = programs_root / "acme" / "publications" / "acme_weekly" / "issue_001" / "issue_001.draft.json"
    draft_state_path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(typer.BadParameter, match=r"Draft state at .*issue_001\.draft\.json is invalid\."):
        build_offline_diff_summary(
            edition_name="acme_weekly",
            issue_number=1,
            section=None,
            reports_root=reports_root,
            programs_root=programs_root,
        )
