from __future__ import annotations

from dataclasses import replace
import json
import pytest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import typer
from typer.testing import CliRunner
import yaml

from cli import app
from src.ai.ai_mode import AIMode, set_ai_mode
from src.ai.edit_learner import EditPattern, append_edit_patterns
from src.ai.client import AIClientError
from src.ai.llm_trace import AITraceContext
from src.commands import review_full as review_full_module
from src.commands.report import generate_report_draft
from src.commands.review_full import generate_review_full
from src.core.analytics_store import AutonomyAuditRecord, append_autonomy_audit_record
from src.core.claim_extraction_calibration_store import ClaimExtractionCalibrationRecord, append_claim_extraction_calibration_record
from src.core.coverage_gap import CoverageGap
from src.core.forecast_engine import ETAForecast
from src.core.issue_projection import IssueProjection
from src.core.action_tracker import append_action
from src.core.assumption_tracker import save_assumptions
import src.core.archive_store as archive_store
from src.core.claim_tracker import append_claim_entry, append_decision_ask
from src.core.decision_register import save_decisions
from src.core.journal import append_review_decision, append_signal, append_signal_thread_link
from src.core.models import Confidence, ConfirmedDimension, EditionType, Enrichment, ReviewSection, ReviewStatus, RiskLevel, RunManifest, Snapshot, SnapshotItem
from src.core.models_v2 import ActionItem, ActionSourceType, ActionStatus, Assumption, AssumptionStatus, ClaimEntry, DecisionAsk, DecisionEntry, DecisionStatus, Milestone, MilestoneAssessment, MilestoneStatus, PersonDirectory, RiskCategory, RiskEntry, RiskImpact, RiskProbability, RiskStatus, Signal, SignalReviewDecision, SignalThreadLink, TrajectoryPoint
from src.core.risk_register_engine import save_risk_register
from src.core.review_status_store import load_review_status, save_review_status
from src.core.sqlite_stores import SQLiteSignalStore, SQLiteTrajectoryStore
from src.core.summary_store import RollingSummary, save_summary
from src.core.trajectory import backfill_trajectory_points
from tests.unit.test_commands_report import _append_approved_v2_signal, _forecast_items, _lookback_snapshot, _manifest, _sample_items, _seed_v2_report_layout, _set_v2_program_artifact_base_url, _snapshot_item_from_work_item


runner = CliRunner()
EDITION_NAME = "acme_weekly"


def test_format_issue_row_title_includes_confidence() -> None:
    title = review_full_module._format_issue_row_title(
        IssueProjection(
            work_item_id=900001,
            source_type="icm_incident",
            severity="block",
            summary="IcM 12345 active.",
            owner_alias=None,
            workstream_id="deployment_readiness",
            ado_url=None,
            linked_entity_ids=(),
            confidence=Confidence.HIGH,
        )
    )

    assert title == "WI:900001 · icm incident · block · high confidence"


def test_format_coverage_gap_row_title_includes_confidence() -> None:
    title = review_full_module._format_coverage_gap_row_title(
        CoverageGap(
            work_item_id=900001,
            title="Missing narrative",
            state="Active",
            assigned_to=None,
            confidence=Confidence.HIGH,
        )
    )

    assert title == "WI:900001 · Active · high confidence"


def test_format_milestone_row_title_includes_confidence() -> None:
    title = review_full_module._format_milestone_row_title(
        Milestone(
            id="m3-code-complete",
            program_id="acme",
            name="M3 - Code Complete",
            target_date=date(2026, 5, 25),
            owner_alias="maintainer",
            status=MilestoneStatus.ON_TRACK,
            exit_criteria=("Code complete",),
            linked_workstream_ids=("deployment_readiness",),
            linked_work_item_ids=(900001,),
        ),
        MilestoneAssessment(
            milestone_id="m3-code-complete",
            computed_health=MilestoneStatus.AT_RISK,
            blocked_criteria=(),
            slip_probability=0.6,
            critical_path=True,
            confidence=Confidence.HIGH,
            reasoning="At risk.",
        ),
        critical_path=True,
    )

    assert title == "m3-code-complete · declared on_track · computed at_risk · high confidence · critical path"


def test_format_milestone_row_detail_includes_confidence() -> None:
    detail = review_full_module._format_milestone_row_detail(
        Milestone(
            id="m3-code-complete",
            program_id="acme",
            name="M3 - Code Complete",
            target_date=date(2026, 5, 25),
            owner_alias="maintainer",
            status=MilestoneStatus.ON_TRACK,
            exit_criteria=("Code complete",),
            linked_workstream_ids=("deployment_readiness",),
            linked_work_item_ids=(900001,),
        ),
        MilestoneAssessment(
            milestone_id="m3-code-complete",
            computed_health=MilestoneStatus.AT_RISK,
            blocked_criteria=(),
            slip_probability=0.6,
            critical_path=True,
            confidence=Confidence.HIGH,
            reasoning="At risk.",
        ),
        schedule_summary="Tracking 2026-05-28 (3 days late vs target)",
        target_history_summary="Target History 2026-05-20 -> 2026-05-25",
    )

    assert detail == (
        "Target 2026-05-25 | Owner maintainer | High confidence | Linked WI:900001 | "
        "Workstream deployment_readiness | Tracking 2026-05-28 (3 days late vs target) | "
        "Target History 2026-05-20 -> 2026-05-25"
    )


def test_build_context_rows_prefers_higher_signal_class_over_newer_status() -> None:
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    newer_status = Signal(
        id="status",
        timestamp=datetime(2026, 5, 10, 17, 30, tzinfo=timezone.utc),
        source="workiq/email",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("WI:900001",),
        text="Status update: rollout remains on track.",
        raw_ref="raw:status",
        confidence=Confidence.MEDIUM,
        metadata={"sender_alias": "owner"},
    )
    older_decision = Signal(
        id="decision",
        timestamp=datetime(2026, 5, 10, 16, 45, tzinfo=timezone.utc),
        source="workiq/email",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("WI:900001",),
        text="Decision: leadership approved the rollout.",
        raw_ref="raw:decision",
        confidence=Confidence.MEDIUM,
        metadata={"sender_alias": "owner"},
    )

    rows = review_full_module._build_context_rows(
        approved_signals=(newer_status, older_decision),
        drift_patterns=(),
        item_ids={900001},
        item_urls={900001: "https://example.invalid/wi/900001"},
        as_of=as_of,
        people_directory=(PersonDirectory(alias="owner", title="Director"),),
        source_confidence_order=("workiq",),
        max_rows=1,
    )

    assert len(rows) == 1
    assert rows[0].summary == "Decision: leadership approved the rollout."


def _write_confirmed_issue(archive_root: Path, *, issue_number: int, markdown_body: str) -> None:
    as_of = datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc)
    archive_store.write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=issue_number,
        snapshot=Snapshot(
            issue_number=issue_number,
            generated_at=as_of,
            ado_data_as_of=as_of,
            edition_type=EditionType.DETAILED,
            items=(
                SnapshotItem(
                    id=issue_number,
                    type="Feature",
                    title=f"Issue {issue_number}",
                    state="Active",
                    assigned_to="Vertex Maintainer",
                    area_path="One\\Adventure\\Acme",
                    target_date=date(2026, 5, 12),
                    risk_level=RiskLevel.MEDIUM,
                    tags=["acme"],
                ),
            ),
            scorecards=(
                ConfirmedDimension(
                    scorecard_name="Acme Readiness",
                    name="Deployment Velocity",
                    risk=RiskLevel.MEDIUM,
                    prior_risk=RiskLevel.LOW,
                    item_count=1,
                    ado_query_url="https://dev.azure.com/your-org/One/_queries/query-id",
                ),
            ),
        ),
        html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
        markdown_body=markdown_body,
        manifest=RunManifest(
            manifest_id=f"manifest-{issue_number}",
            issue_number=issue_number,
            edition=EDITION_NAME,
            started_at=as_of,
            ended_at=as_of,
            config_hash="config",
            snapshot_hash="snapshot",
            html_hash="html",
            md_hash="md",
            ado_calls=1,
            ai_calls=0,
            ai_cost_usd=0.0,
            freshness_summary={"blocks": 0, "warns": 0, "infos": 0},
            qg_results={"QG-4": True, "QG-5": True, "QG-6": True, "QG-8": True},
            git_sha=None,
        ),
        archive_root=archive_root,
    )


def test_generate_review_full_renders_two_pane_html(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=tmp_path / "programs",
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    review_status = load_review_status(EDITION_NAME, reports_root=reports_root)
    assert review_status is not None
    detail_section_id = next(
        section.section_id
        for section in review_status.sections
        if section.section_id == "ws:acme-adventure-xio-100-ramp-readiness-deployment-safety"
    )
    updated_sections = []
    for section in review_status.sections:
        if section.section_id == "exec_summary":
            updated_sections.append(
                ReviewSection(
                    section_id=section.section_id,
                    state=section.state.from_string("approved"),
                    reviewer="Lead PM",
                    note="Ready for LT review.",
                    updated_at=datetime(2026, 5, 5, 18, 15, tzinfo=timezone.utc),
                )
            )
        elif section.section_id == detail_section_id:
            updated_sections.append(
                ReviewSection(
                    section_id=section.section_id,
                    state=section.state.from_string("changes_requested"),
                    reviewer="Vertex Maintainer",
                    note="Need ETA clarification before send.",
                    updated_at=datetime(2026, 5, 5, 18, 20, tzinfo=timezone.utc),
                )
            )
        else:
            updated_sections.append(section)
    save_review_status(
        EDITION_NAME,
        ReviewStatus(issue_number=review_status.issue_number, sections=tuple(updated_sections)),
        reports_root=reports_root,
    )

    artifacts = generate_review_full(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        open_browser=False,
    )

    reviewer_html = artifacts.html_path.read_text(encoding="utf-8")

    assert artifacts.html_path == programs_root / "acme" / "publications" / EDITION_NAME / "review" / "issue_001.html"
    assert "Review Status" in reviewer_html
    assert "Published View" in reviewer_html
    assert "Evidence" in reviewer_html
    assert "Why Drawer" in reviewer_html
    assert "Vertex Review" in reviewer_html


def test_generate_review_full_renders_engms_reference_docs(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    knowledge_dir = programs_root / "acme" / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    (knowledge_dir / "engms_pages.yaml").write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "pages:",
                "  - id: acme-readiness-spec",
                "    title: Acme Readiness Spec",
                "    url: https://eng.ms/acme-readiness",
                "    program_ids: [acme]",
                "    workstream_ids: [acme]",
                "    description: Canonical readiness design notes.",
            )
        ),
        encoding="utf-8",
    )

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    artifacts = generate_review_full(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        open_browser=False,
    )

    reviewer_html = artifacts.html_path.read_text(encoding="utf-8")

    assert "Reference Docs" in reviewer_html
    assert "Acme Readiness Spec" in reviewer_html
    assert "Canonical readiness design notes." in reviewer_html
    assert "https://eng.ms/acme-readiness" in reviewer_html
    assert "Executive Summary" in reviewer_html
    assert "Open document" in reviewer_html


def test_generate_review_full_renders_fetched_engms_reference_docs(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    knowledge_dir = programs_root / "acme" / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    (knowledge_dir / "engms_pages.yaml").write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "pages:",
                "  - id: acme-readiness-spec",
                "    title: Acme Readiness Spec",
                "    url: https://eng.ms/acme-readiness",
                "    program_ids: [acme]",
                "    workstream_ids: [acme]",
                "    description: Canonical readiness design notes.",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(review_full_module, "summarize_engms_page", lambda page: "Canonical readiness design notes. Fetched reviewer summary.")

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    artifacts = generate_review_full(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        open_browser=False,
    )

    reviewer_html = artifacts.html_path.read_text(encoding="utf-8")

    assert "Canonical readiness design notes. Fetched reviewer summary." in reviewer_html


def test_load_reviewer_summaries_appends_engms_references(repo_root: Path, tmp_path: Path) -> None:
    reports_root, _ = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    knowledge_dir = programs_root / "acme" / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    (knowledge_dir / "engms_pages.yaml").write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "pages:",
                "  - id: acme-readiness-spec",
                "    title: Acme Readiness Spec",
                "    url: https://eng.ms/acme-readiness",
                "    program_ids: [acme]",
                "    workstream_ids: [acme]",
            )
        ),
        encoding="utf-8",
    )
    save_summary(
        "acme",
        RollingSummary(
            workstream_id="acme",
            generated_at=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
            prompt_version=None,
            source_mode="manual",
            signal_count=1,
            text="Ramp timeline remains conditional.",
        ),
        programs_root=programs_root,
    )

    resolved_v2 = review_full_module.resolve_edition(
        EDITION_NAME,
        editions_root=reports_root.parent / "programs" / "acme" / "editions",
        programs_root=programs_root,
    )

    summaries = review_full_module._load_reviewer_summaries(
        resolved_v2=resolved_v2,
        programs_root=programs_root,
    )

    assert "acme" in summaries
    assert "Ramp timeline remains conditional." in summaries["acme"]
    assert "Reference doc: Acme Readiness Spec (https://eng.ms/acme-readiness)." in summaries["acme"]


def test_load_reviewer_summaries_appends_fetched_engms_summary(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, _ = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    knowledge_dir = programs_root / "acme" / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    (knowledge_dir / "engms_pages.yaml").write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "pages:",
                "  - id: acme-readiness-spec",
                "    title: Acme Readiness Spec",
                "    url: https://eng.ms/acme-readiness",
                "    program_ids: [acme]",
                "    workstream_ids: [acme]",
            )
        ),
        encoding="utf-8",
    )
    save_summary(
        "acme",
        RollingSummary(
            workstream_id="acme",
            generated_at=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
            prompt_version=None,
            source_mode="manual",
            signal_count=1,
            text="Ramp timeline remains conditional.",
        ),
        programs_root=programs_root,
    )
    monkeypatch.setattr(review_full_module, "summarize_engms_page", lambda page: "Fetched reviewer anticipation context.")

    resolved_v2 = review_full_module.resolve_edition(
        EDITION_NAME,
        editions_root=reports_root.parent / "programs" / "acme" / "editions",
        programs_root=programs_root,
    )

    summaries = review_full_module._load_reviewer_summaries(
        resolved_v2=resolved_v2,
        programs_root=programs_root,
    )

    assert "Fetched reviewer anticipation context." in summaries["acme"]


def test_generate_review_full_renders_semantic_similarity_badge_when_enabled(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    program_path = programs_root / "acme" / "program.yaml"
    program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))

    assert isinstance(program_document, dict)
    ai_document = program_document.get("ai")
    if not isinstance(ai_document, dict):
        ai_document = {}
    ai_document["semantic_index"] = True
    program_document["ai"] = ai_document
    m365_document = program_document.get("m365")
    if not isinstance(m365_document, dict):
        m365_document = {}
    m365_document["enabled"] = False
    program_document["m365"] = m365_document
    program_path.write_text(yaml.safe_dump(program_document, sort_keys=False), encoding="utf-8")

    draft_artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    _write_confirmed_issue(
        archive_root,
        issue_number=7,
        markdown_body=f"# Issue 007\n{draft_artifacts.report.exec_summary_text}\n",
    )

    artifacts = generate_review_full(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        open_browser=False,
    )

    reviewer_html = artifacts.html_path.read_text(encoding="utf-8")

    assert "Similar prior narrative" in reviewer_html
    assert "Issue 007" in reviewer_html
    assert "overlap" in reviewer_html


def test_generate_review_full_renders_approved_telemetry_summary(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    _append_approved_v2_signal(
        programs_root,
        signal=Signal(
            id="analytics-1",
            timestamp=datetime(2026, 5, 5, 10, 0, tzinfo=timezone.utc),
            source="ado/analytics",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=(),
            text="Deployment Readiness: analytics summary",
            raw_ref="ado-analytics:deployment_readiness:20260505:20260421:20260505",
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
    )
    _append_approved_v2_signal(
        programs_root,
        signal=Signal(
            id="sprint-1",
            timestamp=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
            source="ado/sprint",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=(),
            text="Deployment Readiness: sprint summary",
            raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-05",
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
    )

    artifacts = generate_review_full(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        open_browser=False,
    )

    reviewer_html = artifacts.html_path.read_text(encoding="utf-8")

    assert "<h3>Telemetry</h3>" in reviewer_html
    assert "ADO analytics and sprint signals from the local journal | high confidence" in reviewer_html
    assert "analytics, 5 scope, 2 completed, scope up 2, open down 1, cycle 5.0d / lead 8.0d; sprint, Sprint 24, 50% complete, 1 open, team cap 24.0h/day across 3 members" in reviewer_html


def test_generate_review_full_renders_snapshot_backed_three_sprint_history_summaries(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    _append_approved_v2_signal(
        programs_root,
        signal=Signal(
            id="sprint-1",
            timestamp=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
            source="ado/sprint",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=(),
            text="Deployment Readiness: sprint summary",
            raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-05",
            confidence=Confidence.HIGH,
            metadata={
                "iteration_name": "Sprint 24",
                "completion_pct": 100,
                "open_item_count": 0,
                "three_iteration_average_completion_per_business_day": 1.0,
                "three_iteration_completion_per_business_day_history": (0.5, 1.0, 1.5),
                "three_iteration_completed_history_series": ((0, 1, 1), (0, 2, 2), (0, 2, 3)),
                "three_iteration_throughput_trend_direction": "up",
                "three_iteration_throughput_trend_delta_per_business_day": 1.0,
                "three_iteration_average_open_item_count": 1,
                "three_iteration_open_item_count_history": (2, 1, 0),
                "three_iteration_open_history_series": ((3, 2, 2), (3, 1, 1), (3, 1, 0)),
                "three_iteration_open_trend_direction": "down",
                "three_iteration_open_trend_delta_count": -2,
            },
            thread_id=None,
        ),
    )

    artifacts = generate_review_full(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        open_browser=False,
    )

    reviewer_html = artifacts.html_path.read_text(encoding="utf-8")

    assert "<h3>Telemetry</h3>" in reviewer_html
    assert (
        "sprint, Sprint 24, 100% complete, 0 open, 3-sprint avg 1.0/day, 3-sprint throughput 0.5->1.0->1.5/day, throughput trend up 1.0/day over 3 sprints, 3-sprint open avg 1, 3-sprint open 2->1->0, 3-sprint burndown 3->2->2 | 3->1->1 | 3->1->0 open, 3-sprint completion 0->1->1 | 0->2->2 | 0->2->3 done, open trend down 2 over 3 sprints"
        in reviewer_html
    )


def test_generate_review_full_uses_trusted_baseline_for_previous_snapshot(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=tmp_path / "programs",
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    baseline_call: dict[str, int | None] = {}
    snapshot_call: dict[str, int | None] = {}
    original_load_previous_snapshot = review_full_module._load_previous_snapshot

    def _fake_load_trusted_baseline_issue(*args, **kwargs):
        del args
        baseline_call["before_issue_number"] = kwargs.get("before_issue_number")
        return 77

    def _capturing_load_previous_snapshot(*args, **kwargs):
        snapshot_call["trusted_issue_number"] = kwargs.get("trusted_issue_number")
        return original_load_previous_snapshot(*args, **kwargs)

    monkeypatch.setattr(review_full_module, "load_trusted_baseline_issue", _fake_load_trusted_baseline_issue)
    monkeypatch.setattr(review_full_module, "_load_previous_snapshot", _capturing_load_previous_snapshot)

    generate_review_full(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        open_browser=False,
    )

    assert baseline_call["before_issue_number"] == 1
    assert snapshot_call["trusted_issue_number"] == 77


def test_generate_review_full_renders_snapshot_backed_broader_historical_sprint_window(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    _append_approved_v2_signal(
        programs_root,
        signal=Signal(
            id="sprint-1",
            timestamp=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
            source="ado/sprint",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=(),
            text="Deployment Readiness: sprint summary",
            raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-05",
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
    )

    artifacts = generate_review_full(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        open_browser=False,
    )

    reviewer_html = artifacts.html_path.read_text(encoding="utf-8")

    assert "<h3>Telemetry</h3>" in reviewer_html
    assert (
        "sprint, Sprint 24, 100% complete, 0 open, 4-sprint throughput 1.0->0.5->1.0->1.5/day, 4-sprint open 1->2->1->0, 4-sprint burndown 3->2->1 | 3->2->2 | 3->1->1 | 3->1->0 open, 4-sprint completion 0->1->2 | 0->1->1 | 0->2->2 | 0->2->3 done"
        in reviewer_html
    )


def test_generate_review_full_reads_sqlite_backed_signals_and_trajectories(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    report_as_of = datetime(2026, 5, 20, 18, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(review_full_module, "_build_default_anticipation_client", lambda bundle, trace_context=None: None)
    monkeypatch.setattr(
        review_full_module,
        "M365Enricher",
        lambda *args, **kwargs: SimpleNamespace(enrich_items=lambda **inner_kwargs: {}),
    )

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=report_as_of,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    _set_v2_program_storage_backend(programs_root, program_id="acme", storage_backend="sqlite")
    signal_store = SQLiteSignalStore(programs_root=programs_root)
    trajectory_store = SQLiteTrajectoryStore(programs_root=programs_root)
    signal_store.append(
        Signal(
            id="analytics-1",
            timestamp=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            source="ado/analytics",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=(),
            text="Deployment Readiness: analytics summary",
            raw_ref="ado-analytics:deployment_readiness:20260520:20260506:20260520",
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
        )
    )
    signal_store.append_review(
        "acme",
        SignalReviewDecision(
            signal_id="analytics-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
    )
    signal_store.append(
        Signal(
            id="icm-1",
            timestamp=datetime(2026, 5, 20, 11, 0, tzinfo=timezone.utc),
            source="icm/incident",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:900001",),
            text="IcM 12345: Sev2 incident active for deployment readiness.",
            raw_ref="icm:12345",
            confidence=Confidence.HIGH,
            metadata={"severity": 2},
            thread_id=None,
        )
    )
    trajectory_store.append(
        "acme",
        900001,
        TrajectoryPoint(
            date=date(2026, 5, 19),
            state="Active",
            assigned_to="Vertex Maintainer",
            target_date=date(2026, 5, 17),
            risk_level=RiskLevel.MEDIUM,
            area_path="One\\Adventure\\Acme\\Deployment",
        ),
    )
    (programs_root / "acme" / "milestones.yaml").write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "milestones:",
                "  - id: m3-code-complete",
                "    name: M3 - Code Complete",
                "    target_date: 2026-05-12",
                "    owner_alias: maintainer",
                "    status: on_track",
                "    exit_criteria:",
                "      - Code complete",
                "    linked_workstream_ids:",
                "      - deployment_readiness",
                "    linked_work_item_ids:",
                "      - 900001",
            )
        ),
        encoding="utf-8",
    )

    artifacts = generate_review_full(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        open_browser=False,
    )

    reviewer_html = artifacts.html_path.read_text(encoding="utf-8")

    assert "<h3>Telemetry</h3>" in reviewer_html
    assert "analytics, 5 scope, 2 completed, scope up 2, open down 1, cycle 5.0d / lead 8.0d" in reviewer_html
    assert "Active Issues" in reviewer_html
    assert "WI:900001 · icm incident · block · high confidence" in reviewer_html
    assert "IcM 12345: Sev2 incident active for deployment readiness." in reviewer_html
    assert "Milestone Health" in reviewer_html
    assert "m3-code-complete · declared on_track · computed missed" in reviewer_html
    assert "Target 2026-05-12 | Owner maintainer | High confidence | Linked WI:900001 | Workstream deployment_readiness | Tracking 2026-05-17 (5 days late vs target)" in reviewer_html


def test_generate_review_full_writes_section_review_adaptive_cards(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    _set_v2_program_artifact_base_url(reports_root.parent / "programs", artifact_base_url="https://contoso.example/vertex-output")

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=tmp_path / "programs",
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    review_status = load_review_status(EDITION_NAME, reports_root=reports_root)
    assert review_status is not None
    detail_section_id = next(
        section.section_id
        for section in review_status.sections
        if section.section_id == "ws:acme-adventure-xio-100-ramp-readiness-deployment-safety"
    )
    updated_sections = []
    for section in review_status.sections:
        if section.section_id == detail_section_id:
            updated_sections.append(
                ReviewSection(
                    section_id=section.section_id,
                    state=section.state.from_string("changes_requested"),
                    reviewer="Vertex Maintainer",
                    note="Need ETA clarification before send.",
                    updated_at=datetime(2026, 5, 5, 18, 20, tzinfo=timezone.utc),
                )
            )
        else:
            updated_sections.append(section)
    save_review_status(
        EDITION_NAME,
        ReviewStatus(issue_number=review_status.issue_number, sections=tuple(updated_sections)),
        reports_root=reports_root,
    )

    artifacts = generate_review_full(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        open_browser=False,
    )
    reviewer_html = artifacts.html_path.read_text(encoding="utf-8")

    assert len(artifacts.adaptive_card_paths) >= 2
    assert all(path.parent == programs_root / "acme" / "publications" / EDITION_NAME / "review" / "adaptive_cards" for path in artifacts.adaptive_card_paths)
    assert all(":" not in path.name for path in artifacts.adaptive_card_paths)
    published_html_path = programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.html"
    assert f"file:///{published_html_path.as_posix()}" in reviewer_html

    exec_summary_path = next(path for path in artifacts.adaptive_card_paths if ".exec_summary." in path.name)
    exec_summary_payload = json.loads(exec_summary_path.read_text(encoding="utf-8"))
    assert exec_summary_payload["type"] == "AdaptiveCard"
    assert exec_summary_payload["body"][0]["text"] == "acme_weekly section review"
    assert any(block.get("type") == "ActionSet" for block in exec_summary_payload["body"])

    detail_payload = next(
        json.loads(path.read_text(encoding="utf-8"))
        for path in artifacts.adaptive_card_paths
        if "Need ETA clarification before send." in path.read_text(encoding="utf-8")
    )
    detail_action = next(block for block in detail_payload["body"] if block.get("type") == "ActionSet")
    assert detail_action["actions"][0]["url"] == "https://contoso.example/vertex-output/review/issue_001.html"


def test_generate_review_full_posts_section_review_adaptive_cards_when_requested(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    _set_v2_program_artifact_base_url(reports_root.parent / "programs", artifact_base_url="https://contoso.example/vertex-output")
    _set_v2_program_teams_webhook_url(reports_root.parent / "programs", webhook_url="https://contoso.example/webhook")

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=tmp_path / "programs",
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    sent_cards: list[str] = []

    def _build_fake_sender(webhook_url: str):
        assert webhook_url == "https://contoso.example/webhook"

        def _sender(section_id: str, payload: dict[str, object]) -> None:
            sent_cards.append(section_id)

        return _sender

    monkeypatch.setattr("src.commands.review_full._build_section_review_teams_sender", _build_fake_sender)

    artifacts = generate_review_full(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        open_browser=False,
        post_adaptive_cards=True,
    )

    assert artifacts.posted_card_count == len(artifacts.adaptive_card_paths)
    assert len(sent_cards) == len(artifacts.adaptive_card_paths)
    assert all(path.exists() for path in artifacts.adaptive_card_paths)


def test_generate_review_full_rejects_post_adaptive_cards_without_webhook(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=tmp_path / "programs",
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    with pytest.raises(typer.BadParameter, match="teams_incoming_webhook_url"):
        generate_review_full(
            edition_name=EDITION_NAME,
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=programs_root,
            open_browser=False,
            post_adaptive_cards=True,
        )


def test_review_full_cli_writes_reviewer_output(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = tmp_path / "programs"

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=tmp_path / "programs",
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    monkeypatch.setattr("src.commands.review_full.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.review_full.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.review_full.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["review-full", "--edition", EDITION_NAME, "--no-open"])

    assert result.exit_code == 0
    assert "Leadership review view generated for Issue 001." in result.stdout
    assert "Reviewer HTML:" in result.stdout
    assert (programs_root / "acme" / "publications" / EDITION_NAME / "review" / "issue_001.html").exists()


def test_review_full_cli_supports_json_and_csv(monkeypatch, tmp_path: Path) -> None:
    html_path = tmp_path / "issue_077.html"
    card_path = tmp_path / "issue_077.exec_summary.json"

    def _fake_generate_review_full(
        edition_name: str,
        issue_number: int | None = None,
        reports_root: Path | None = None,
        archive_root: Path | None = None,
        output_root: Path | None = None,
        m365_enricher=None,
        open_browser: bool = False,
        post_adaptive_cards: bool = False,
    ):
        del edition_name, reports_root, archive_root, output_root, m365_enricher, open_browser, post_adaptive_cards
        return review_full_module.ReviewFullArtifacts(
            issue_number=issue_number or 77,
            html_path=html_path,
            adaptive_card_paths=(card_path,),
            posted_card_count=1,
        )

    monkeypatch.setattr("src.commands.review_full.generate_review_full", _fake_generate_review_full)

    json_result = runner.invoke(app, ["review-full", "--edition", EDITION_NAME, "--issue", "77", "--no-open", "--format", "json"])

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["edition_name"] == EDITION_NAME
    assert payload["issue_number"] == 77
    assert payload["html_path"] == str(html_path)
    assert payload["adaptive_card_count"] == 1
    assert payload["posted_card_count"] == 1
    assert payload["adaptive_card_paths"] == [str(card_path)]

    csv_result = runner.invoke(app, ["review-full", "--edition", EDITION_NAME, "--issue", "77", "--no-open", "--format", "csv"])

    assert csv_result.exit_code == 0
    lines = csv_result.stdout.strip().splitlines()
    assert lines[0] == "entry_type,edition_name,issue_number,html_path,adaptive_card_count,posted_card_count,adaptive_card_path"
    assert lines[1] == f"summary,{EDITION_NAME},77,{html_path},1,1,"
    assert lines[2] == f"adaptive_card,{EDITION_NAME},77,{html_path},1,1,{card_path}"


def test_review_full_cli_posts_adaptive_cards_when_requested(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    _set_v2_program_teams_webhook_url(reports_root.parent / "programs", webhook_url="https://contoso.example/webhook")

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=tmp_path / "programs",
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    sent_cards: list[str] = []

    def _build_fake_sender(webhook_url: str):
        assert webhook_url == "https://contoso.example/webhook"

        def _sender(section_id: str, payload: dict[str, object]) -> None:
            sent_cards.append(section_id)

        return _sender

    monkeypatch.setattr("src.commands.review_full.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.review_full.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.review_full.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.review_full._build_section_review_teams_sender", _build_fake_sender)

    result = runner.invoke(app, ["review-full", "--edition", EDITION_NAME, "--no-open", "--post-adaptive-cards"])

    assert result.exit_code == 0
    assert sent_cards
    assert "Section review cards posted:" in result.stdout


def test_generate_review_full_keeps_m365_metadata_in_reviewer_pane_only(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    _set_v2_program_m365(reports_root.parent / "programs", enabled=True)

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=tmp_path / "programs",
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    fake_enricher = _FakeEnricher()
    artifacts = generate_review_full(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        m365_enricher=fake_enricher,
        open_browser=False,
    )

    reviewer_html = artifacts.html_path.read_text(encoding="utf-8")
    published_html = (programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.html").read_text(encoding="utf-8")

    assert fake_enricher.calls == [(True, True, 3)]
    assert "Enrichments (1):" in reviewer_html
    assert "Subject: Leadership feedback digest" in reviewer_html
    assert "Subject: Leadership feedback digest" not in published_html


def test_generate_review_full_renders_workstream_raci_in_status_chips(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    _set_v2_workstream_raci(
        reports_root.parent / "programs",
        workstream_id="acme",
        raci={
            "accountable": "alex_owner",
            "responsible": ["isaiah_owner", "priya_owner"],
            "consulted": ["sam_partner"],
            "informed": ["lt_staff"],
        },
    )

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=tmp_path / "programs",
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    artifacts = generate_review_full(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        open_browser=False,
    )

    reviewer_html = artifacts.html_path.read_text(encoding="utf-8")

    assert "RACI: A alex_owner | R isaiah_owner, priya_owner | C sam_partner | I lt_staff" in reviewer_html


def test_generate_review_full_renders_forecast_why_lines(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    _set_v2_edition_forecast(reports_root.parent / "programs" / "acme" / "editions", edition_id=EDITION_NAME, forecast_enabled=True)

    for issue_number in range(1, 5):
        as_of = datetime(2026, 4, issue_number, 18, 0, tzinfo=timezone.utc)
        archive_store.write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=as_of,
                items=(
                    _snapshot_item_from_work_item(_forecast_items(as_of)[0], risk_level=_forecast_items(as_of)[0].risk_level),
                ),
                scorecard_risks={"Deployment Velocity": _forecast_items(as_of)[0].risk_level},
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}\n",
            manifest=_manifest(issue_number=issue_number, as_of=as_of),
            archive_root=archive_root,
        )

        draft = generate_report_draft(
            edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=tmp_path / "programs",
        as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_forecast_items(timestamp), 0),
        open_browser=False,
    )

    if draft.manifest.metadata.get("forecast_summary") is None:
        pytest.skip("Current continuity forecast fixture did not produce a forecast candidate.")

    artifacts = generate_review_full(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        open_browser=False,
    )

    reviewer_html = artifacts.html_path.read_text(encoding="utf-8")

    assert "Forecast formula" in reviewer_html
    assert "Predicted slip +" in reviewer_html


def test_generate_review_full_v2_renders_drift_patterns_in_reviewer_pane(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    _seed_high_drift_pattern(programs_root, work_item_id=900001)

    generate_report_draft(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    artifacts = generate_review_full(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        open_browser=False,
    )

    reviewer_html = artifacts.html_path.read_text(encoding="utf-8")

    assert "Signals &amp; Patterns" in reviewer_html
    assert "Drift · High · Eta Drift" in reviewer_html
    assert "Target date slipped 3 times in the last 90 days." in reviewer_html


def test_generate_review_full_v2_renders_anticipated_questions(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    _seed_high_drift_pattern(programs_root, work_item_id=900001)

    generate_report_draft(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    artifacts = generate_review_full(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        open_browser=False,
    )

    reviewer_html = artifacts.html_path.read_text(encoding="utf-8")

    assert "Anticipated Questions" in reviewer_html
    assert "Jordan Lee" in reviewer_html
    assert "Why has Deployment velocity telemetry stabilization slipped 3 times?" in reviewer_html
    assert "the next checkpoint" in reviewer_html


def test_build_anticipation_client_falls_back_to_backup_deployment(monkeypatch) -> None:
    attempts: list[str] = []

    class _RuntimeAIClient:
        def __init__(self, *, deployment: str, temperature: float, budget_usd: float, trace_context=None) -> None:
            del temperature, budget_usd
            self.deployment = deployment

        def structured(self, system: str, user: str, *, parser, max_tokens: int = 800, prompt_version: str | None = None):
            del system, user, max_tokens, prompt_version
            attempts.append(self.deployment)
            if self.deployment == "review-primary":
                raise AIClientError("primary deployment failed")
            return parser(
                {
                    "question": "What moved the ramp timeline again?",
                    "suggested_response": "Name the blocker, owner, checkpoint, and consequence.",
                }
            )

    monkeypatch.setattr("src.ai.deployment_fallback.AIClient", _RuntimeAIClient)

    bundle = SimpleNamespace(
        config=SimpleNamespace(
            ai=SimpleNamespace(
                enabled=True,
                exec_summary_deployment="review-primary",
                exec_summary_backup_deployment="review-backup",
                blurb_deployment=None,
                blurb_backup_deployment=None,
                temperature=0.2,
                budget_usd_per_run=0.5,
            )
        )
    )

    client = review_full_module._build_anticipation_client(bundle)

    assert client is not None
    rendered = client.structured(
        "system",
        "user",
        parser=lambda payload: (payload["question"], payload["suggested_response"]),
    )

    assert rendered == (
        "What moved the ramp timeline again?",
        "Name the blocker, owner, checkpoint, and consequence.",
    )
    assert attempts == ["review-primary", "review-backup"]


def test_build_anticipation_client_logs_vertex_first_guidance_when_no_deployment_is_configured(caplog, monkeypatch) -> None:
    monkeypatch.delenv("VERTEX_EXEC_DEPLOYMENT", raising=False)
    monkeypatch.delenv("VERTEX_AI_DEPLOYMENT", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT", raising=False)
    monkeypatch.delenv("VERTEX_EXEC_BACKUP_DEPLOYMENT", raising=False)
    monkeypatch.delenv("VERTEX_AI_BACKUP_DEPLOYMENT", raising=False)
    bundle = SimpleNamespace(
        config=SimpleNamespace(
            ai=SimpleNamespace(
                enabled=True,
                exec_summary_deployment=None,
                exec_summary_backup_deployment=None,
                blurb_deployment=None,
                blurb_backup_deployment=None,
                temperature=0.2,
                budget_usd_per_run=0.5,
            )
        )
    )

    with caplog.at_level("WARNING"):
        client = review_full_module._build_anticipation_client(bundle)

    assert client is None
    assert "VERTEX_EXEC_DEPLOYMENT, VERTEX_AI_DEPLOYMENT, or AZURE_OPENAI_DEPLOYMENT" in caplog.text
    assert "Configure one of the supported vertex deployment aliases" in caplog.text


def test_build_anticipation_client_passes_trace_context_to_runtime_clients(monkeypatch) -> None:
    seen_trace_contexts: list[object] = []

    class _RuntimeAIClient:
        def __init__(self, *, deployment: str, temperature: float, budget_usd: float, trace_context=None) -> None:
            del deployment, temperature, budget_usd
            seen_trace_contexts.append(trace_context)

        def structured(self, system: str, user: str, *, parser, max_tokens: int = 800, prompt_version: str | None = None):
            del system, user, max_tokens, prompt_version
            return parser(
                {
                    "question": "What moved the ramp timeline again?",
                    "suggested_response": "Name the blocker, owner, checkpoint, and consequence.",
                }
            )

    monkeypatch.setattr("src.ai.deployment_fallback.AIClient", _RuntimeAIClient)

    bundle = SimpleNamespace(
        config=SimpleNamespace(
            ai=SimpleNamespace(
                enabled=True,
                exec_summary_deployment="review-primary",
                exec_summary_backup_deployment=None,
                blurb_deployment=None,
                blurb_backup_deployment=None,
                temperature=0.2,
                budget_usd_per_run=0.5,
            )
        )
    )
    trace_context = review_full_module._build_review_full_trace_context(
        edition_name=EDITION_NAME,
        issue_number=78,
        output_root=Path("output"),
        budget_usd=0.5,
    )

    client = review_full_module._build_anticipation_client(bundle, trace_context=trace_context)

    assert client is not None
    rendered = client.structured(
        "system",
        "user",
        parser=lambda payload: (payload["question"], payload["suggested_response"]),
    )

    assert rendered == (
        "What moved the ramp timeline again?",
        "Name the blocker, owner, checkpoint, and consequence.",
    )


def test_build_anticipation_client_returns_none_when_ai_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.commands.review_full.FallbackStructuredClient",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("FallbackStructuredClient should not be constructed")),
    )
    bundle = SimpleNamespace(
        config=SimpleNamespace(
            ai=SimpleNamespace(
                enabled=True,
                exec_summary_deployment="review-primary",
                exec_summary_backup_deployment=None,
                blurb_deployment=None,
                blurb_backup_deployment=None,
                temperature=0.2,
                budget_usd_per_run=0.5,
            )
        )
    )
    set_ai_mode(AIMode.DISABLED)
    try:
        client = review_full_module._build_anticipation_client(bundle)
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert client is None


def test_build_default_anticipation_client_passes_trace_context_to_builder(monkeypatch) -> None:
    seen_trace_contexts: list[object] = []

    def _fake_build_anticipation_client(bundle, *, trace_context=None):
        del bundle
        seen_trace_contexts.append(trace_context)
        return object()

    monkeypatch.setattr(review_full_module, "_build_anticipation_client", _fake_build_anticipation_client)

    bundle = SimpleNamespace(config=SimpleNamespace(ai=SimpleNamespace(enabled=True)))
    trace_context = review_full_module._build_review_full_trace_context(
        edition_name=EDITION_NAME,
        issue_number=78,
        output_root=Path("output"),
        budget_usd=0.5,
    )
    client = review_full_module._build_default_anticipation_client(bundle=bundle, trace_context=trace_context)

    assert client is not None
    assert seen_trace_contexts == [trace_context]
    assert isinstance(trace_context, AITraceContext)
    assert trace_context.caller == "src.commands.review_full.prepare_review_full_context"
    assert trace_context.metadata["run_budget_usd"] == 0.5
    assert trace_context.metadata["task_type"] == "reviewer_anticipation"


def test_build_default_anticipation_client_returns_none_when_invocation_ai_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        review_full_module,
        "_build_anticipation_client",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("_build_anticipation_client should not be called")),
    )
    bundle = SimpleNamespace(config=SimpleNamespace(ai=SimpleNamespace(enabled=True)))
    trace_context = review_full_module._build_review_full_trace_context(
        edition_name=EDITION_NAME,
        issue_number=78,
        output_root=Path("output"),
        budget_usd=0.5,
    )

    set_ai_mode(AIMode.DISABLED)
    try:
        client = review_full_module._build_default_anticipation_client(bundle=bundle, trace_context=trace_context)
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert client is None


def test_generate_review_full_v2_renders_owner_vitality_bars(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    _set_v2_program_vitality_surface(programs_root, surface="reviewer_pane", enabled=True)

    generate_report_draft(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    artifacts = generate_review_full(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        open_browser=False,
    )

    reviewer_html = artifacts.html_path.read_text(encoding="utf-8")

    assert "Owner Vitality" in reviewer_html
    assert "Last 14 days. Reviewer-only coaching view" in reviewer_html
    assert "maintainer" in reviewer_html
    assert "100%" in reviewer_html
    assert "2/2 items fresh, 0 leakage" in reviewer_html


def test_generate_review_full_renders_trust_calibration_card(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    monkeypatch.setattr(review_full_module, "_build_default_anticipation_client", lambda bundle, trace_context=None: None)
    monkeypatch.setattr(
        review_full_module,
        "M365Enricher",
        lambda *args, **kwargs: SimpleNamespace(enrich_items=lambda **inner_kwargs: {}),
    )

    generate_report_draft(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 20, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    _seed_review_full_trust_state(programs_root)

    artifacts = generate_review_full(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        open_browser=False,
    )

    reviewer_html = artifacts.html_path.read_text(encoding="utf-8")

    assert "Autonomy Trust Calibration" in reviewer_html
    assert "Blurb generation" in reviewer_html
    assert "override=0.1000 | calibration=0.9000 | samples=3" in reviewer_html
    assert "Claim extraction" in reviewer_html
    assert "agreement=" in reviewer_html
    assert "avg_difference=" in reviewer_html
    assert "calibration_samples=" in reviewer_html
    assert "Decision ask escalation" in reviewer_html
    assert "accepted=10/10 (100%) | level=l3" in reviewer_html
    assert "Confidence 90%" in reviewer_html
    assert "repair" in reviewer_html
    assert "Salience-calibration bridge" in reviewer_html
    assert "slip_modifier=+0.18 | attention_weight=0.31" in reviewer_html
    assert "Forecast slip pressure is high while editorial attention remains low." in reviewer_html


def test_generate_review_full_v2_hides_owner_vitality_when_surface_disabled(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    generate_report_draft(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=tmp_path / "programs",
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    artifacts = generate_review_full(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        open_browser=False,
    )

    reviewer_html = artifacts.html_path.read_text(encoding="utf-8")

    assert "Owner Vitality" not in reviewer_html


def test_generate_review_full_v2_renders_coverage_gaps_warning(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    generate_report_draft(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=tmp_path / "programs",
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    artifacts = generate_review_full(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        open_browser=False,
    )

    reviewer_html = artifacts.html_path.read_text(encoding="utf-8")

    assert "Coverage Gaps" in reviewer_html
    assert "2 active items with no approved signals or narrative mention in 14 days." in reviewer_html
    assert "WI:900001 · Active · high confidence" in reviewer_html
    assert "WI:900002 · At Risk · high confidence" in reviewer_html


def test_generate_review_full_v2_renders_open_claims_and_asks(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    report_as_of = datetime(2026, 5, 20, 18, 0, tzinfo=timezone.utc)
    completed_items = list(_sample_items(report_as_of))
    completed_items[2] = replace(completed_items[2], state="Resolved")

    generate_report_draft(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=report_as_of,
        work_item_loader=lambda bundle, timestamp: (tuple(completed_items), 0),
        open_browser=False,
    )

    append_claim_entry(
        ClaimEntry(
            id="claim-1",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=1,
            workstream_id="deployment_readiness",
            text="WI:900001 expected by 2026-05-10",
            entity_refs=("WI:900001",),
            claim_date=date(2026, 5, 5),
            owner_alias="maintainer",
            due_date=date(2026, 5, 10),
        ),
        programs_root=programs_root,
    )
    append_decision_ask(
        DecisionAsk(
            id="ask-1",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=1,
            text="Need LT decision on SCHIE timeline",
            entity_refs=("WI:900002",),
            ask_date=date(2026, 5, 5),
            owner_alias="lt",
        ),
        programs_root=programs_root,
    )
    save_decisions(
        "acme",
        (
            DecisionEntry(
                id="decision-1",
                program_id="acme",
                title="SCHIE timeline approval",
                context="Timeline needs leadership alignment before partner commit.",
                decision="Await LT approval before locking external target.",
                rationale=None,
                alternatives_considered=(),
                decided_by="lt",
                decision_date=date(2026, 4, 15),
                status=DecisionStatus.PROPOSED,
                superseded_by=None,
                linked_claim_id=None,
                linked_risk_id=None,
                linked_action_ids=(),
                workstream_id="deployment_readiness",
                entity_refs=("WI:900001",),
            ),
        ),
        programs_root=programs_root,
    )
    save_assumptions(
        "acme",
        (
            Assumption(
                id="assumption-1",
                program_id="acme",
                text="Partner schema contract stays stable through Q4.",
                validation_method="Validate in monthly partner review",
                validation_due=date(2026, 5, 1),
                status=AssumptionStatus.UNVALIDATED,
                linked_risk_id=None,
                linked_milestone_id="m3-code-complete",
                owner_alias="operator",
                identified_date=date(2026, 4, 10),
                entity_refs=("WI:900001",),
            ),
        ),
        programs_root=programs_root,
    )
    append_action(
        "acme",
        ActionItem(
            id="action-1",
            program_id="acme",
            text="Follow up with partner team on schema freeze.",
            owner_alias="operator",
            due_date=date(2026, 5, 8),
            status=ActionStatus.OPEN,
            source_signal_id=None,
            source_type=ActionSourceType.MANUAL,
            linked_work_item_ids=(900001,),
            linked_claim_id=None,
            linked_risk_id=None,
            workstream_id="deployment_readiness",
            created_at=datetime(2026, 5, 5, 10, 0, tzinfo=timezone.utc),
            resolved_at=None,
            resolution_note=None,
        ),
        programs_root=programs_root,
    )
    save_risk_register(
        "acme",
        (
            RiskEntry(
                id="risk-1",
                program_id="acme",
                title="Partner schema freeze may slip",
                description="Partner schema contract still lacks an approved freeze date.",
                probability=RiskProbability.LIKELY,
                impact=RiskImpact.HIGH,
                category=RiskCategory.DEPENDENCY,
                owner_alias="operator",
                mitigation_plan="Escalate in partner sync and keep contingency path open.",
                mitigation_due_date=date(2026, 5, 12),
                linked_workstream_ids=("deployment_readiness",),
                linked_work_item_ids=(900001,),
                linked_milestone_ids=("m3-code-complete",),
                linked_claim_ids=("claim-1",),
                linked_action_ids=("action-1",),
                status=RiskStatus.OPEN,
                identified_date=date(2026, 4, 5),
                identified_in_vertex_issue=1,
                last_reviewed_date=date(2026, 4, 10),
                entity_refs=("WI:900001",),
            ),
        ),
        programs_root=programs_root,
    )
    (programs_root / "acme" / "milestones.yaml").write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "milestones:",
                "  - id: m3-code-complete",
                "    name: M3 - Code Complete",
                "    target_date: 2026-05-25",
                "    owner_alias: maintainer",
                "    status: on_track",
                "    exit_criteria:",
                "      - Code complete",
                "    linked_workstream_ids:",
                "      - deployment_readiness",
                "    linked_work_item_ids:",
                "      - 900001",
                "  - id: m4-pilot-rollout-validation",
                "    name: M4 - Pilot Rollout Validation",
                "    target_date: 2026-05-17",
                "    owner_alias: maintainer",
                "    status: on_track",
                "    exit_criteria:",
                "      - Pilot rollout validated",
                "    linked_workstream_ids:",
                "      - deployment_readiness",
                "    linked_work_item_ids:",
                "      - 900003",
            )
        ),
        encoding="utf-8",
    )
    (programs_root / "acme" / "dependencies.yaml").write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "dependencies:",
                "  - id: acme-fabrikam-buildouts",
                "    from_item_id: 900001",
                "    to_workstream_id: fabrikam:buildouts",
                "    dependency_type: informs",
                "    risk_if_broken: Fabrikam buildout planning remains provisional until Acme lands the freeze date.",
                "    mitigation: Review the dependency in the weekly cross-program checkpoint.",
                "    status: active",
                "    owner_alias: maintainer",
            )
        ),
        encoding="utf-8",
    )
    archive_store.write_confirmed_issue(
        edition="acme_weekly",
        issue_number=1,
        snapshot=_lookback_snapshot(
            issue_number=1,
            as_of=datetime(2026, 5, 18, 18, 0, tzinfo=timezone.utc),
            items=(
                _snapshot_item_from_work_item(_sample_items(datetime(2026, 5, 20, 18, 0, tzinfo=timezone.utc))[0], risk_level=RiskLevel.HIGH),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.HIGH},
        ),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001",
        manifest=replace(
            _manifest(issue_number=1, as_of=datetime(2026, 5, 18, 18, 0, tzinfo=timezone.utc)),
            metadata={
                "milestone_assessments": [
                    {
                        "milestone_id": "m3-code-complete",
                        "target_date": "2026-05-20",
                    },
                    {
                        "milestone_id": "m4-pilot-rollout-validation",
                        "completion_date": "2026-05-16",
                    }
                ]
            },
        ),
        archive_root=archive_root,
    )
    backfill_trajectory_points(
        "acme",
        900001,
        (
            TrajectoryPoint(
                date=date(2026, 5, 15),
                state="Active",
                assigned_to="Vertex Maintainer",
                target_date=date(2026, 5, 28),
                risk_level=RiskLevel.HIGH,
                area_path="One\\Adventure\\Acme",
            ),
        ),
        programs_root=programs_root,
    )
    backfill_trajectory_points(
        "acme",
        900003,
        (
            TrajectoryPoint(
                date=date(2026, 5, 18),
                state="Resolved",
                assigned_to="Vertex Maintainer",
                target_date=date(2026, 5, 12),
                risk_level=RiskLevel.LOW,
                area_path="One\\Adventure\\Fabrikam\\Acme\\Scenarios",
            ),
        ),
        programs_root=programs_root,
    )
    approved_signal_at = datetime(2026, 5, 19, 9, 0, tzinfo=timezone.utc)
    append_signal(
        Signal(
            id="signal-1",
            timestamp=approved_signal_at,
            source="manual",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:900001",),
            text="Partner freeze remains provisional and keeps Fabrikam buildout timing tentative.",
            raw_ref="WI:900001",
            confidence=Confidence.HIGH,
        ),
        programs_root=programs_root,
        partition_at=approved_signal_at,
    )
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="signal-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 19, 9, 15, tzinfo=timezone.utc),
            reviewed_by="operator",
        ),
        programs_root=programs_root,
    )
    threaded_signal_at = datetime(2026, 5, 19, 10, 0, tzinfo=timezone.utc)
    append_signal(
        Signal(
            id="signal-2",
            timestamp=threaded_signal_at,
            source="workiq/email",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:900001",),
            text="Partner email says the freeze date is still not committed.",
            raw_ref="msg-100",
            confidence=Confidence.MEDIUM,
        ),
        programs_root=programs_root,
        partition_at=threaded_signal_at,
    )
    append_signal(
        Signal(
            id="signal-3",
            timestamp=datetime(2026, 5, 19, 11, 0, tzinfo=timezone.utc),
            source="manual",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:900001",),
            text="Manual follow-up confirmed the same dependency concern.",
            raw_ref=None,
            confidence=Confidence.HIGH,
        ),
        programs_root=programs_root,
        partition_at=threaded_signal_at,
    )
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="signal-2",
            decision="approved",
            reviewed_at=datetime(2026, 5, 19, 10, 5, tzinfo=timezone.utc),
            reviewed_by="operator",
        ),
        programs_root=programs_root,
    )
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="signal-3",
            decision="approved",
            reviewed_at=datetime(2026, 5, 19, 11, 5, tzinfo=timezone.utc),
            reviewed_by="operator",
        ),
        programs_root=programs_root,
    )
    append_signal_thread_link(
        "acme",
        SignalThreadLink(
            signal_id="signal-2",
            thread_id="partner-freeze-risk",
            linked_at=datetime(2026, 5, 19, 11, 10, tzinfo=timezone.utc),
            linked_by="operator",
        ),
        programs_root=programs_root,
    )
    append_signal_thread_link(
        "acme",
        SignalThreadLink(
            signal_id="signal-3",
            thread_id="partner-freeze-risk",
            linked_at=datetime(2026, 5, 19, 11, 10, tzinfo=timezone.utc),
            linked_by="operator",
        ),
        programs_root=programs_root,
    )

    artifacts = generate_review_full(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        open_browser=False,
    )

    reviewer_html = artifacts.html_path.read_text(encoding="utf-8")

    assert "Open Claims" in reviewer_html
    assert "claim-1 · issue #1 · stale" in reviewer_html
    assert "Claim due 2026-05-10 has passed." in reviewer_html
    assert "Risk Register" in reviewer_html
    assert "risk-1 · open · stale" in reviewer_html
    assert "Owner operator | dependency | likely x high | Next review 2026-05-10 | Mitigation due 2026-05-12" in reviewer_html
    assert "Linked WI:900001; milestone m3-code-complete; claim claim-1; action action-1; workstream deployment_readiness" in reviewer_html
    assert "Partner schema freeze may slip: Partner schema contract still lacks an approved freeze date." in reviewer_html
    assert "Milestone Health" in reviewer_html
    assert "Milestone Timeline" in reviewer_html
    assert "<div class=\"milestone-timeline\">" in reviewer_html
    assert "Tracking 2026-05-28 (3 days late vs target)" in reviewer_html
    assert "Completed 2026-05-18 (1 day late vs target)" in reviewer_html
    assert "m3-code-complete · declared on_track · computed at_risk · high confidence · critical path" in reviewer_html
    assert "m4-pilot-rollout-validation · declared on_track · computed completed · high confidence" in reviewer_html
    assert "Target 2026-05-25 | Owner maintainer | High confidence | Linked WI:900001 | Workstream deployment_readiness" in reviewer_html
    assert "Target 2026-05-17 | Owner maintainer | High confidence | Linked WI:900003 | Workstream deployment_readiness" in reviewer_html
    assert "computed at_risk" in reviewer_html
    assert "Cross-Program Cascades" in reviewer_html
    assert "WI:900001 impacts fabrikam:buildouts" in reviewer_html
    assert "Impact Fabrikam buildout planning remains provisional until Acme lands the freeze date. | Trigger signal on WI:900001 | Confidence high" in reviewer_html
    assert "Partner freeze remains provisional and keeps Fabrikam buildout timing tentative." in reviewer_html
    assert "Signal Threads" in reviewer_html
    assert "partner-freeze-risk · 2 signals" in reviewer_html
    assert "<div class=\"signal-thread-list\">" in reviewer_html
    assert "workiq/email" in reviewer_html and "high confidence" in reviewer_html
    assert "Window 2026-05-19 to 2026-05-19 | Refs WI:900001 | Sources workiq/email, manual" in reviewer_html
    assert "<span class=\"signal-thread-source\">manual</span>" in reviewer_html
    assert "Manual follow-up confirmed the same dependency concern." in reviewer_html
    assert "<span class=\"signal-thread-source\">workiq/email</span>" in reviewer_html
    assert "Partner email says the freeze date is still not committed." in reviewer_html
    assert "Decision Register" in reviewer_html
    assert "decision-1 · proposed · stale" in reviewer_html
    assert "SCHIE timeline approval: Await LT approval before locking external target." in reviewer_html
    assert "Assumptions" in reviewer_html
    assert "assumption-1 · unvalidated · overdue" in reviewer_html
    assert "Partner schema contract stays stable through Q4." in reviewer_html
    assert "Action Items" in reviewer_html
    assert "operator · action-1 · open · overdue · candidate for resolution" in reviewer_html
    assert "Follow up with partner team on schema freeze." in reviewer_html
    assert "Active Issues" in reviewer_html
    assert "WI:900002 · decision ask · warn" in reviewer_html
    assert "Issue #001 ask: Need LT decision on SCHIE timeline (owner lt)" in reviewer_html
    assert "https://dev.azure.com/your-org/One/_workitems/edit/900002" in reviewer_html
    assert "Open in ADO" in reviewer_html
    assert 'href="#review-ask-1"' in reviewer_html
    assert 'href="#review-action-1"' in reviewer_html
    assert 'href="#review-claim-1"' in reviewer_html
    assert 'href="#review-risk-1"' in reviewer_html
    assert "Open Asks" in reviewer_html
    assert 'id="review-ask-1"' in reviewer_html
    assert "ask-1 · issue #1" in reviewer_html
    assert "Need LT decision on SCHIE timeline" in reviewer_html
    assert 'id="review-action-1"' in reviewer_html
    assert 'id="review-claim-1"' in reviewer_html
    assert 'id="review-risk-1"' in reviewer_html


def test_generate_review_full_renders_eta_forecast_annotation_in_active_issues(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 20, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    append_decision_ask(
        DecisionAsk(
            id="ask-1",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=1,
            text="Need LT decision on SCHIE timeline",
            entity_refs=("WI:900002",),
            ask_date=date(2026, 5, 5),
            owner_alias="lt",
        ),
        programs_root=programs_root,
    )

    monkeypatch.setattr(
        review_full_module,
        "_load_eta_forecasts",
        lambda **_: {
            900002: ETAForecast(
                work_item_id=900002,
                ado_target_date=date(2026, 5, 8),
                predicted_target_date=date(2026, 5, 12),
                confidence=Confidence.LOW,
                slip_probability=0.78,
                reasoning="2 prior slips in 90 days -> 78% miss probability",
                prior_slips=2,
                p50_date=date(2026, 5, 12),
                p80_date=date(2026, 5, 15),
                p95_date=date(2026, 5, 18),
            )
        },
    )

    artifacts = generate_review_full(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        open_browser=False,
    )

    reviewer_html = artifacts.html_path.read_text(encoding="utf-8")

    assert "WI:900002 · decision ask · warn" in reviewer_html
    assert "Issue #001 ask: Need LT decision on SCHIE timeline (owner lt)" in reviewer_html
    assert (
        "Owner lt | low confidence — 2 prior slips, 78% miss probability | "
        "forecast p50 May 12, p80 May 15, p95 May 18 | Linked ask-1"
    ) in reviewer_html


def test_generate_review_full_renders_inbound_cross_program_cascades(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(
        repo_root,
        tmp_path,
        edition_names=("acme_weekly", "fabrikam_weekly"),
        program_names=("acme", "fabrikam"),
    )
    programs_root = reports_root.parent / "programs"
    (programs_root / "fabrikam" / "dependencies.yaml").write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "dependencies:",
                "  - id: fabrikam-acme-sequencing",
                "    from_item_id: 800001",
                "    to_workstream_id: acme:deployment_readiness",
                "    dependency_type: blocks",
                "    risk_if_broken: Acme deployment readiness remains blocked until Fabrikam sequencing is restored.",
                "    status: active",
                "    owner_alias: fabrikam-owner",
            )
        ),
        encoding="utf-8",
    )
    _append_approved_v2_signal(
        programs_root,
        signal=Signal(
            id="fabrikam-signal-1",
            timestamp=datetime(2026, 5, 5, 17, 0, tzinfo=timezone.utc),
            source="manual",
            program_id="fabrikam",
            workstream_id="buildouts",
            entity_refs=("WI:800001",),
            text="Fabrikam sequencing remains blocked and now threatens the Acme deployment-readiness plan.",
            raw_ref="WI:800001",
            confidence=Confidence.HIGH,
        ),
    )

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    artifacts = generate_review_full(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        open_browser=False,
    )
    reviewer_html = artifacts.html_path.read_text(encoding="utf-8")

    assert "Cross-Program Cascades" in reviewer_html
    assert "WI:800001 impacts acme:deployment_readiness" in reviewer_html
    assert "Impact Acme deployment readiness remains blocked until Fabrikam sequencing is restored. | Trigger signal on WI:800001 | Confidence high" in reviewer_html
    assert "Fabrikam sequencing remains blocked and now threatens the Acme deployment-readiness plan." in reviewer_html


def test_generate_review_full_reads_sqlite_backed_inbound_cross_program_cascades(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(
        repo_root,
        tmp_path,
        edition_names=("acme_weekly", "fabrikam_weekly"),
        program_names=("acme", "fabrikam"),
    )
    programs_root = reports_root.parent / "programs"
    _set_v2_program_storage_backend(programs_root, program_id="fabrikam", storage_backend="sqlite")
    (programs_root / "fabrikam" / "dependencies.yaml").write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "dependencies:",
                "  - id: fabrikam-acme-sequencing",
                "    from_item_id: 800001",
                "    to_workstream_id: acme:deployment_readiness",
                "    dependency_type: blocks",
                "    risk_if_broken: Acme deployment readiness remains blocked until Fabrikam sequencing is restored.",
                "    status: active",
                "    owner_alias: fabrikam-owner",
            )
        ),
        encoding="utf-8",
    )
    armada_signal_store = SQLiteSignalStore(programs_root=programs_root)
    armada_signal_store.append(
        Signal(
            id="fabrikam-signal-1",
            timestamp=datetime(2026, 5, 5, 17, 0, tzinfo=timezone.utc),
            source="manual",
            program_id="fabrikam",
            workstream_id="buildouts",
            entity_refs=("WI:800001",),
            text="Fabrikam sequencing remains blocked and now threatens the Acme deployment-readiness plan.",
            raw_ref="WI:800001",
            confidence=Confidence.HIGH,
        )
    )
    armada_signal_store.append_review(
        "fabrikam",
        SignalReviewDecision(
            signal_id="fabrikam-signal-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 5, 17, 5, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
    )

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    artifacts = generate_review_full(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        open_browser=False,
    )
    reviewer_html = artifacts.html_path.read_text(encoding="utf-8")

    assert "Cross-Program Cascades" in reviewer_html
    assert "WI:800001 impacts acme:deployment_readiness" in reviewer_html
    assert "Impact Acme deployment readiness remains blocked until Fabrikam sequencing is restored. | Trigger signal on WI:800001 | Confidence high" in reviewer_html
    assert "Fabrikam sequencing remains blocked and now threatens the Acme deployment-readiness plan." in reviewer_html


def test_generate_review_full_v2_satellite_filters_low_severity_drift_patterns(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    _set_v2_edition_altitude(reports_root.parent / "programs" / "acme" / "editions" / "acme_weekly.yaml", altitude="satellite")
    _seed_high_drift_pattern(programs_root, work_item_id=900001)
    _seed_low_drift_pattern(programs_root, work_item_id=900002)

    generate_report_draft(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    artifacts = generate_review_full(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        open_browser=False,
    )

    reviewer_html = artifacts.html_path.read_text(encoding="utf-8")

    assert "Drift · High · Eta Drift" in reviewer_html
    assert "Target date slipped 3 times in the last 90 days." in reviewer_html
    assert "No trajectory updates in the last 90 days while the item remains Active." not in reviewer_html
    assert "Coverage Gaps" not in reviewer_html


class _FakeEnricher:
    def __init__(self) -> None:
        self.calls: list[tuple[bool, bool, int]] = []

    def enrich_items(self, *, config, program_context, items, as_of):
        self.calls.append((config.enabled, program_context is not None, len(items)))
        return {
            900001: (
                Enrichment(
                    source="mail",
                    source_id="mail-1",
                    author="rushi@example.com",
                    timestamp=datetime(2026, 5, 5, 17, 45, tzinfo=timezone.utc),
                    excerpt="Subject: Leadership feedback digest",
                    permalink="https://outlook.office.com/mail/leadership-feedback",
                ),
            )
        }


def _set_v2_edition_altitude(edition_path: Path, *, altitude: str) -> None:
    document = yaml.safe_load(edition_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    document["altitude"] = altitude
    edition_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _set_v2_edition_forecast(editions_root: Path, *, edition_id: str, forecast_enabled: bool) -> None:
    edition_path = editions_root / f"{edition_id}.yaml"
    document = yaml.safe_load(edition_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    document["forecast_enabled"] = forecast_enabled
    edition_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _set_v2_program_m365(programs_root: Path, *, enabled: bool) -> None:
    program_path = programs_root / "acme" / "program.yaml"
    document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    m365_block = document.setdefault("m365", {})
    assert isinstance(m365_block, dict)
    m365_block["enabled"] = enabled
    program_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _set_v2_program_vitality_surface(programs_root: Path, *, surface: str, enabled: bool) -> None:
    program_path = programs_root / "acme" / "program.yaml"
    document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    vitality_block = document.setdefault("vitality", {})
    assert isinstance(vitality_block, dict)
    surfaces = vitality_block.setdefault("surfaces", {})
    assert isinstance(surfaces, dict)
    surfaces[surface] = enabled
    program_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _seed_review_full_trust_state(programs_root: Path) -> None:
    program_dir = programs_root / "acme"
    (program_dir / "journal").mkdir(parents=True, exist_ok=True)
    (program_dir / "_feedback").mkdir(parents=True, exist_ok=True)

    append_edit_patterns(
        "acme",
        (
            EditPattern(
                program_id="acme",
                edition_id="acme_weekly",
                issue_number=77,
                section_id="repair",
                recorded_at=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
                summary="Small blurb edit.",
                before_excerpt="Draft repair blurb.",
                after_excerpt="Confirmed repair blurb.",
                before_word_count=4,
                after_word_count=4,
                task_type="workstream_blurb",
                prompt_version="workstream_blurb.v1",
                author_override_magnitude=0.05,
            ),
            EditPattern(
                program_id="acme",
                edition_id="acme_weekly",
                issue_number=78,
                section_id="repair",
                recorded_at=datetime(2026, 5, 11, 10, 0, tzinfo=timezone.utc),
                summary="Small blurb edit.",
                before_excerpt="Draft repair blurb.",
                after_excerpt="Confirmed repair blurb.",
                before_word_count=4,
                after_word_count=4,
                task_type="workstream_blurb",
                prompt_version="workstream_blurb.v1",
                author_override_magnitude=0.1,
            ),
            EditPattern(
                program_id="acme",
                edition_id="acme_weekly",
                issue_number=79,
                section_id="repair",
                recorded_at=datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc),
                summary="Small blurb edit.",
                before_excerpt="Draft repair blurb.",
                after_excerpt="Confirmed repair blurb.",
                before_word_count=4,
                after_word_count=4,
                task_type="workstream_blurb",
                prompt_version="workstream_blurb.v1",
                author_override_magnitude=0.15,
            ),
        ),
        programs_root=programs_root,
    )

    for index in range(10):
        append_autonomy_audit_record(
            AutonomyAuditRecord(
                program_id="acme",
                action_id=f"review-escalation-{index}",
                level="l3",
                author_alias="owner",
                subject_alias="priya",
                evidence_refs=(f"decision_ask:ask-{index}", "workstream:acme"),
                policy_rule="decision_ask_escalation",
                accepted=True,
                applied_at=datetime(2026, 5, 1 + index, 8, 0, tzinfo=timezone.utc),
                action_type="decision_ask_escalation",
                blast_radius="1 draft to 2 recipients",
                rollback_mechanism="Delete draft.",
                prior_acceptance_rate=1.0,
            ),
            programs_root=programs_root,
        )

    append_claim_extraction_calibration_record(
        ClaimExtractionCalibrationRecord(
            program_id="acme",
            issue_number=77,
            recorded_at=datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc),
            mode="calibration",
            ai_claim_count=9,
            regex_claim_count=10,
            shared_claim_count=9,
            ai_only_count=0,
            regex_only_count=1,
            agreement_rate=0.9,
        ),
        programs_root=programs_root,
    )
    append_claim_extraction_calibration_record(
        ClaimExtractionCalibrationRecord(
            program_id="acme",
            issue_number=78,
            recorded_at=datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc),
            mode="calibration",
            ai_claim_count=9,
            regex_claim_count=9,
            shared_claim_count=9,
            ai_only_count=0,
            regex_only_count=0,
            agreement_rate=1.0,
        ),
        programs_root=programs_root,
    )

    (program_dir / "_feedback" / "author_salience.yaml").write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                'updated_at: "2026-05-20T11:00:00+00:00"',
                'author_alias: "owner"',
                'ema_alpha: 0.1',
                'min_weight: 0.2',
                'workstreams:',
                '  repair:',
                '    attention_weight: 0.31',
                '    sample_count: 3',
                '    average_override_magnitude: 0.10',
                '    last_event_at: "2026-05-12T10:00:00+00:00"',
                'dimensions: {}',
                '',
            )
        ),
        encoding="utf-8",
    )
    (program_dir / "_feedback" / "forecast_calibration.yaml").write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                'updated_at: "2026-05-20T11:00:00+00:00"',
                'since: null',
                'confidence: "high"',
                'workstream_modifiers:',
                '  repair: 0.18',
                'dri_modifiers: {}',
                '',
            )
        ),
        encoding="utf-8",
    )


def _set_v2_program_teams_webhook_url(programs_root: Path, *, webhook_url: str) -> None:
    program_path = programs_root / "acme" / "program.yaml"
    document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    m365 = document.setdefault("m365", {})
    assert isinstance(m365, dict)
    m365["teams_incoming_webhook_url"] = webhook_url
    program_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _set_v2_program_storage_backend(programs_root: Path, *, program_id: str, storage_backend: str) -> None:
    program_path = programs_root / program_id / "program.yaml"
    document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    document["storage_backend"] = storage_backend
    program_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _set_v2_workstream_raci(programs_root: Path, *, workstream_id: str, raci: dict[str, object]) -> None:
    workstreams_path = programs_root / "acme" / "workstreams.yaml"
    document = yaml.safe_load(workstreams_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    entries = document.get("workstreams")
    assert isinstance(entries, list)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("id") != workstream_id:
            continue
        entry["raci"] = raci
        workstreams_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        return
    raise AssertionError(f"workstream {workstream_id} not found")


def _seed_high_drift_pattern(programs_root: Path, *, work_item_id: int) -> None:
    backfill_trajectory_points(
        "acme",
        work_item_id,
        (
            TrajectoryPoint(date=date(2026, 2, 5), state="Active", assigned_to="maintainer@example.com", target_date=date(2026, 5, 1), risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme\\Deployment"),
            TrajectoryPoint(date=date(2026, 3, 5), state="Active", assigned_to="maintainer@example.com", target_date=date(2026, 5, 5), risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme\\Deployment"),
            TrajectoryPoint(date=date(2026, 4, 5), state="Active", assigned_to="maintainer@example.com", target_date=date(2026, 5, 10), risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme\\Deployment"),
            TrajectoryPoint(date=date(2026, 5, 1), state="Active", assigned_to="maintainer@example.com", target_date=date(2026, 5, 15), risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme\\Deployment"),
        ),
        programs_root=programs_root,
    )


def _seed_low_drift_pattern(programs_root: Path, *, work_item_id: int) -> None:
    backfill_trajectory_points(
        "acme",
        work_item_id,
        (
            TrajectoryPoint(date=date(2026, 1, 1), state="Active", assigned_to="maintainer@example.com", target_date=date(2026, 5, 8), risk_level=RiskLevel.HIGH, area_path="One\\Adventure\\Contoso\\Networking"),
        ),
        programs_root=programs_root,
    )
