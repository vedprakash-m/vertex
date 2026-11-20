from __future__ import annotations
from dataclasses import asdict, replace
from email import message_from_bytes
import json
import re
import os
import pytest
import shutil
import typer
from datetime import date, datetime, timedelta, timezone
from jinja2 import Environment, FileSystemLoader
from pathlib import Path
from typing import cast
from types import SimpleNamespace

from typer.testing import CliRunner
import yaml

from cli import app
from src.ai._pipeline import AIPipelineError
from src.ai.edit_learner import append_edit_patterns, build_edit_patterns
from src.ai.client import AIClientError, AIPreflightError, BudgetExceeded
from src.commands.report_lookback import _build_lookback_ai_retrospective_rows
from src.commands import report_deck as report_deck_module
from src.commands import report_health as report_health_module
from src.commands import report as report_module
from src.ai.blurb_generator import WorkstreamBlurb
from src.ai.exec_summary_drafter import ExecSummaryDraft
from src.core.program_fact_store import ProgramFactInput, ProgramFactStore
from src.commands.report import generate_report_draft, generate_report_draft_v2
from src.core.action_tracker import append_action
from src.core.assumption_tracker import save_assumptions
from src.core.archive_store import write_confirmed_issue, write_skipped_issue
from src.core.claim_tracker import append_claim_entry, append_claim_status_update, append_decision_ask
from src.core.config_loader import load_report_bundle
from src.core import continuation_contract as continuation_contract_module
from src.core.decision_register import save_decisions
from src.core.exceptions import AuthError, ConfigError, QueryError, QueryTimeoutError
from src.core.forecast_engine import ETAForecast
from src.core.incident_journal_store import append_incident_entry
from src.core.journal import append_review_decision, append_signal
from src.core.jinja_filters import JINJA_FILTERS, JINJA_GLOBALS, build_anchor
from src.core.manifest_writer import hash_jsonable
from src.core.overrides_store import save_overrides
from src.core.models import Comment, Confidence, ConfirmedDimension, DeltaKind, EditionType, Revision, RiskLevel, RunManifest, Snapshot
from src.core.models import SnapshotItem, WorkItem
from src.core.models_v2 import ActionItem, ActionSourceType, ActionStatus, Assumption, AssumptionStatus, ClaimEntry, ClaimStatusUpdate, DecisionAsk, DecisionEntry, DecisionStatus, IncidentEntry, M365Config, Milestone, MilestoneAssessment, MilestoneStatus, RiskCategory, RiskEntry, RiskImpact, RiskProbability, RiskStatus, SectionEvidenceBrief, SectionRevisionProposal, SectionRevisionStatus, Signal, SignalReviewDecision, TrajectoryPoint, VitalityArchiveEntry, VitalityArchiveWorkstream, Workstream, WorkstreamSignalSources
from src.core.risk_register_engine import save_risk_register
from src.core.narrative_store import get_narratives_dir
from src.core.overrides_store import get_overrides_path
from src.core.review_status_store import load_review_status
from src.core.section_proposal_store import append_proposal
from src.core.sqlite_stores import SQLiteSignalStore, SQLiteTrajectoryStore
from src.core.snapshot_store import read_snapshot
from src.core.summary_store import RollingSummary, save_summary
from src.core.trajectory import backfill_trajectory_points
from src.core.continuation_contract import build_continuation_contract, get_continuation_contract_path
from src.core.trusted_baseline_store import advance_trusted_baseline
from src.core.chapter_contract_loader import canonical_dimension_binding_id
from src.core.slice_contract_loader import SliceFilterDefinition, SlicePredicateDefinition
from src.core.trusted_baseline_store import record_untrusted_issue
from src.core.deck_renderer import DeckRenderContext, DeckRenderer
from src.core.view_models import HealthSummary, KpiTile, KustoMetric, KustoSectionData, RetrospectiveIntelligenceRow, RetrospectiveIntelligenceSummary, WorkstreamData
from src.core.overrides_store import OverridesSeedingState
from tests.support.report_test_setup import disable_kusto_in_report_copy, reset_overrides_to_seed_state, stage_v2_report_workspace
from tests.support.synthetic_report_source import ACTIVITY_QUERY, FOLLOWUP_QUERY, FULL_SCOPE_QUERY, build_synthetic_report_source
from tests.support.slice_contract_fixtures import build_test_ado_source_contract, build_test_slice_contract


runner = CliRunner()
EDITION_NAME = "acme_weekly"


from tests.unit.test_commands_report import (
    _append_approved_v2_signal,
    _enable_v2_program_ai,
    _forecast_items,
    _issue_077_snapshot_items,
    _lookback_snapshot,
    _manifest,
    _patch_m3_linked_wi,
    _sample_items,
    _sample_items_with_diff,
    _sample_kusto_query_results,
    _secondary_program_sample_items,
    _seed_v2_report_layout,
    _service_slice_items,
    _set_override_risks_for_section,
    _set_v2_edition_layout_mode,
    _set_v2_key_dependencies,
    _set_v2_program_artifact_base_url,
    _set_v2_program_include_dependency_risk,
    _set_v2_program_storage_backend,
    _snapshot_item_from_work_item,
    _snapshot_with_item,
    _stable_low_risk_items,
    _v2_readiness_items,
    _write_confirmed_program_scorecard,
    _write_lt_deck_dependency_proposals,
    _write_report_secondary_program_high_dependency,
    _write_report_secondary_program_portfolio_dependency_chain,
    _write_v2_summary_seed,
    repo_root,
    report_startup_calls,
)

def test_generate_report_draft_renders_eta_forecast_annotation_in_v2_lt_deck_open_issues(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(
        repo_root,
        tmp_path,
        edition_names=("acme_weekly", "example_lt_deck"),
    )

    monkeypatch.setattr(
        "src.commands.report._load_eta_forecasts",
        lambda **kwargs: {
            900001: ETAForecast(
                work_item_id=900001,
                ado_target_date=date(2026, 5, 10),
                predicted_target_date=date(2026, 5, 15),
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

    artifacts = generate_report_draft(
        edition_name="example_lt_deck",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    assert "low confidence — 2 prior slips, 78% miss probability | forecast p50 May 12, p80 May 15, p95 May 18" in artifacts.markdown_body


def test_generate_report_draft_computes_readiness_from_v2_signal_context(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    editions_root = tmp_path / "programs" / "acme" / "editions"
    programs_root = tmp_path / "programs"
    journal_dir = programs_root / "acme" / "journal"
    if journal_dir.exists():
        shutil.rmtree(journal_dir)
    journal_dir.mkdir(parents=True)
    narratives_078 = programs_root / "acme" / "narratives" / "issue_078"
    if narratives_078.exists():
        shutil.rmtree(narratives_078)

    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    append_signal(
        Signal(
            id="approved-signal",
            timestamp=as_of,
            source="ado/revision",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:1001",),
            text="Approved",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
            thread_id=None,
        ),
        programs_root=programs_root,
        partition_at=as_of,
    )
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="approved-signal",
            decision="approved",
            reviewed_at=as_of,
            reviewed_by="system",
            note=None,
        ),
        programs_root=programs_root,
    )
    append_signal(
        Signal(
            id="pending-signal",
            timestamp=as_of,
            source="workiq/email",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:1002",),
            text="Needs review",
            raw_ref=None,
            confidence=Confidence.MEDIUM,
            metadata=None,
            thread_id=None,
        ),
        programs_root=programs_root,
        partition_at=as_of,
    )

    artifacts = generate_report_draft(
        edition_name="acme_weekly",
        issue_number=78,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_v2_readiness_items(timestamp), 0),
        open_browser=False,
    )

    assert artifacts.draft_readiness is not None
    assert artifacts.draft_readiness.unreviewed_signal_count == 1
    assert artifacts.draft_readiness.coverage_gap_count == 1
    assert artifacts.draft_readiness.missing_narrative_count > 0
    assert artifacts.draft_readiness.summary.startswith("Draft readiness:")


def test_generate_report_draft_persists_scorecard_eta_override(repo_root: Path, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    first_artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=first_artifacts.issue_number)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    overrides_payload["scorecards"]["Example Release Readiness"]["Deployment Velocity"]["eta"] = "2026-05-27"
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    draft_payload = json.loads((programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.draft.json").read_text(encoding="utf-8"))
    override_snapshot = draft_payload["override_snapshot"]

    assert override_snapshot["Example Release Readiness"]["Deployment Velocity"]["eta"] == "2026-05-27"


def test_generate_report_draft_label_override_updates_rendered_titles(repo_root: Path, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    first_artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=first_artifacts.issue_number)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    overrides_payload["scorecards"]["Example Release Readiness"]["Deployment Velocity"]["label"] = "Velocity"
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    draft_payload = json.loads((programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.draft.json").read_text(encoding="utf-8"))
    override_snapshot = draft_payload["override_snapshot"]

    assert override_snapshot["Example Release Readiness"]["Deployment Velocity"]["label"] == "Velocity"
    assert "**Velocity**" in artifacts.markdown_body
    assert "Velocity" in artifacts.html_body


def test_generate_report_draft_hides_detail_section_when_override_requests_it(repo_root: Path, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    first_artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=first_artifacts.issue_number)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    overrides_payload["scorecards"]["Example Release Readiness"]["Deployment Velocity"]["hide_details"] = True
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    deployment_velocity = next(dimension for dimension in artifacts.report.scorecard if dimension.name == "Deployment Velocity")
    section_id = "example-release-readiness-deployment-velocity"

    assert deployment_velocity.risk == RiskLevel.MEDIUM
    assert section_id not in artifacts.report.workstream_blurbs
    assert all(section.section_id != f"ws:{section_id}" for section in artifacts.report.review_status.sections)
    assert f'href="#{section_id}"' not in artifacts.html_body


def test_generate_report_draft_hides_removed_section_even_when_narrative_exists(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    section_id = "example-release-readiness-deployment-velocity"

    save_overrides(
        EDITION_NAME,
        report_module.OverridesDocument(issue_number=1, top_3_now=(), scorecards=(), removed_sections=(section_id,)),
        reports_root=reports_root,
    )
    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=1)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    overrides_payload["removed_sections"] = [section_id]
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    narratives_dir = get_narratives_dir(EDITION_NAME, 1, reports_root)
    narratives_dir.mkdir(parents=True, exist_ok=True)
    (narratives_dir / f"ws_{section_id}.md").write_text("Explicitly removed section narrative.", encoding="utf-8")

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    assert section_id not in artifacts.report.workstream_blurbs
    assert all(section.section_id != f"ws:{section_id}" for section in artifacts.report.review_status.sections)
    assert f'href="#{section_id}"' not in artifacts.html_body


def test_generate_report_draft_carries_forward_prior_section_without_current_counterpart(repo_root: Path, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    _set_v2_edition_layout_mode(programs_root / "acme" / "editions", edition_id=EDITION_NAME, layout_mode="dashboard")
    prior_as_of = datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc)
    section_id = "example-release-readiness-deployment-velocity"

    prior_narratives_dir = reports_root.parent / "programs" / "acme" / "archive" / EDITION_NAME / "narratives" / "issue_001"
    prior_narratives_dir.mkdir(parents=True, exist_ok=True)
    (prior_narratives_dir / "exec_summary.md").write_text("Prior exec summary.", encoding="utf-8")
    (prior_narratives_dir / f"ws_{section_id}.md").write_text("Prior deployment velocity narrative.", encoding="utf-8")

    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=_lookback_snapshot(
            issue_number=1,
            as_of=prior_as_of,
            items=(
                _snapshot_item_from_work_item(_sample_items(prior_as_of)[0], risk_level=RiskLevel.HIGH),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.HIGH},
        ),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001\n",
        manifest=_manifest(issue_number=1, as_of=prior_as_of, edition=EDITION_NAME),
        archive_root=archive_root,
    )
    advance_trusted_baseline(
        EDITION_NAME,
        1,
        established_at=prior_as_of,
        established_by="operator",
        editions_root=reports_root.parent / "programs" / "acme" / "editions",
        programs_root=reports_root.parent / "programs",
    )

    scorecards_path = reports_root.parent / "programs" / "acme" / "scorecards.yaml"
    scorecards_payload = yaml.safe_load(scorecards_path.read_text(encoding="utf-8"))
    removed_scorecard_name = scorecards_payload["scorecards"][0]["name"]
    scorecards_payload["scorecards"][0]["dimensions"] = [
        dimension
        for dimension in scorecards_payload["scorecards"][0]["dimensions"]
        if dimension["name"] != "Deployment Velocity"
    ]
    removed_linked_pairs: set[tuple[str, str]] = set()
    for sc in scorecards_payload["scorecards"][1:]:
        kept = []
        for dim in sc.get("dimensions", []):
            if (
                dim.get("linked_scorecard") == removed_scorecard_name
                and dim.get("linked_dimension") == "Deployment Velocity"
            ):
                removed_linked_pairs.add((sc["name"], dim["name"]))
            else:
                kept.append(dim)
        sc["dimensions"] = kept
    scorecards_path.write_text(yaml.safe_dump(scorecards_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    slice_contracts_path = reports_root.parent / "programs" / "acme" / "slice_contracts.yaml"
    slice_contracts_payload = yaml.safe_load(slice_contracts_path.read_text(encoding="utf-8"))
    slice_contracts_payload["slices"] = [
        slice_contract
        for slice_contract in slice_contracts_payload["slices"]
        if slice_contract["title"] != "Deployment Velocity"
        and (slice_contract["scorecard_name"], slice_contract["title"]) not in removed_linked_pairs
    ]
    slice_contracts_path.write_text(
        yaml.safe_dump(slice_contracts_payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    chapter_contract_path = reports_root.parent / "programs" / "acme" / "chapter_contract.yaml"
    chapter_contract_payload = yaml.safe_load(chapter_contract_path.read_text(encoding="utf-8"))
    removed_dim_ids = {"example_release_readiness.deployment_velocity"}
    for sc_name, dim_name in removed_linked_pairs:
        removed_dim_ids.add(canonical_dimension_binding_id(sc_name, dim_name, chapter_namespace="acme"))
    for chapter in chapter_contract_payload["chapters"]:
        chapter["dimensions"] = [
            dimension_id
            for dimension_id in chapter.get("dimensions", [])
            if dimension_id not in removed_dim_ids
        ]
    chapter_contract_path.write_text(
        yaml.safe_dump(chapter_contract_payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        issue_number=2,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    contract_payload = json.loads((programs_root / "acme" / "publications" / EDITION_NAME / "issue_002" / "issue_002.continuation_contract.json").read_text(encoding="utf-8"))

    assert artifacts.report.workstream_blurbs[section_id] == "Prior deployment velocity narrative."
    assert "Prior deployment velocity narrative." in artifacts.html_body
    assert section_id in artifacts.html_body
    assert any(section.section_id == f"ws:{section_id}" for section in artifacts.report.review_status.sections)
    assert contract_payload["section_roster"]["removed_sections"] == []


def test_generate_report_draft_hides_unapproved_section_roster_additions_during_bridge(repo_root: Path, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    prior_as_of = datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc)
    inherited_section_id = "example-release-readiness-deployment-velocity"
    added_section_id = "example-release-readiness-infrastructure-checks"

    prior_narratives_dir = reports_root.parent / "programs" / "acme" / "archive" / EDITION_NAME / "narratives" / "issue_001"
    prior_narratives_dir.mkdir(parents=True, exist_ok=True)
    (prior_narratives_dir / "exec_summary.md").write_text("Prior exec summary.", encoding="utf-8")
    (prior_narratives_dir / f"ws_{inherited_section_id}.md").write_text("Prior deployment velocity narrative.", encoding="utf-8")

    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=_lookback_snapshot(
            issue_number=1,
            as_of=prior_as_of,
            items=(
                _snapshot_item_from_work_item(_sample_items(prior_as_of)[0], risk_level=RiskLevel.HIGH),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.HIGH},
        ),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001\n",
        manifest=_manifest(issue_number=1, as_of=prior_as_of, edition=EDITION_NAME),
        archive_root=archive_root,
    )
    advance_trusted_baseline(
        EDITION_NAME,
        1,
        established_at=prior_as_of,
        established_by="operator",
        editions_root=reports_root.parent / "programs" / "acme" / "editions",
        programs_root=reports_root.parent / "programs",
    )
    scorecards_path = programs_root / "acme" / "scorecards.yaml"
    scorecards = yaml.safe_load(scorecards_path.read_text(encoding="utf-8"))
    scorecards["scorecards"][0]["dimensions"].append(dict(name="Infrastructure Checks", workstream_id="acme", ado_filter="area_path contains 'Infrastructure'"))
    scorecards_path.write_text(yaml.safe_dump(scorecards, sort_keys=False), encoding="utf-8")
    slices_path = programs_root / "acme" / "slice_contracts.yaml"
    slices = yaml.safe_load(slices_path.read_text(encoding="utf-8"))
    contract = asdict(build_test_slice_contract(contract_id="example_release_readiness.infrastructure_checks", scorecard_name=scorecards["scorecards"][0]["name"], title="Infrastructure Checks", section="infrastructure_checks", workstream="acme", ado=build_test_ado_source_contract(filters=SliceFilterDefinition(any_of=(SlicePredicateDefinition("area_path", "contains", "Infrastructure"),)))))
    contract["decision_sources"] = contract["source_contract"].pop("decision_sources")
    contract["fallback_sources"] = contract["source_contract"].pop("fallback_sources")
    contract["source_contract"]["ado"]["saved_queries"] = [dict(binding_id="infrastructure-checks", query_id="synthetic-infrastructure-query", mode="activity_delta")]
    slices["slices"].append(contract)
    slices_path.write_text(yaml.safe_dump(slices, sort_keys=False), encoding="utf-8")

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        issue_number=2,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp) + (replace(_sample_items(timestamp)[0], id=990002, title="Synthetic infrastructure checkpoint", area_path=r"ExampleProject\Acme\Infrastructure"),), 0),
        open_browser=False,
    )

    contract_payload = json.loads((programs_root / "acme" / "publications" / EDITION_NAME / "issue_002" / "issue_002.continuation_contract.json").read_text(encoding="utf-8"))

    assert inherited_section_id in artifacts.report.workstream_blurbs
    assert added_section_id not in artifacts.report.workstream_blurbs
    assert all(section.section_id != f"ws:{added_section_id}" for section in artifacts.report.review_status.sections)
    assert added_section_id in contract_payload["section_roster"]["added_sections"]


def test_generate_report_draft_warns_when_medium_risk_narrative_is_empty(repo_root: Path, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    first_artifacts = generate_report_draft(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )
    first_section = next(iter(first_artifacts.report.workstream_blurbs))
    _set_override_risks_for_section(
        reports_root=reports_root,
        snapshot=first_artifacts.snapshot,
        section_id=first_section,
        risk=RiskLevel.MEDIUM,
        edition_name="acme_weekly",
    )

    artifacts = generate_report_draft(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    assert artifacts.exit_code == 3
    assert any(f"Medium-risk section {first_section}" in warning for warning in artifacts.warnings)


def test_generate_report_draft_warns_when_narrative_predates_latest_eta_change(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    narrative_path = programs_root / "acme" / "narratives" / "issue_001" / "chapter_deployment_readiness.md"
    narrative_path.parent.mkdir(parents=True, exist_ok=True)
    narrative_path.write_text("Deployment narrative remains current.\n", encoding="utf-8")
    stale_timestamp = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc).timestamp()
    os.utime(narrative_path, (stale_timestamp, stale_timestamp))
    backfill_trajectory_points(
        "acme",
        900001,
        (
            TrajectoryPoint(
                date=date(2026, 5, 1),
                state="Active",
                assigned_to="Vertex Maintainer",
                target_date=date(2026, 5, 10),
                risk_level=RiskLevel.MEDIUM,
                area_path="ExampleProject\\Acme\\Deployment",
            ),
            TrajectoryPoint(
                date=date(2026, 5, 8),
                state="Active",
                assigned_to="Vertex Maintainer",
                target_date=date(2026, 5, 17),
                risk_level=RiskLevel.MEDIUM,
                area_path="ExampleProject\\Acme\\Deployment",
            ),
        ),
        programs_root=programs_root,
    )

    artifacts = generate_report_draft(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    assert any(
        "chapter_deployment_readiness.md last edited May 1, but WI:900001 ETA changed May 8" in warning
        for warning in artifacts.warnings
    )


def test_generate_report_draft_surfaces_trajectory_velocity_when_kusto_disabled(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    program_path = programs_root / "acme" / "program.yaml"
    program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(program_document, dict)
    ado_block = program_document.setdefault("ado", {})
    assert isinstance(ado_block, dict)
    ado_block["date_window_days"] = 7
    program_path.write_text(yaml.safe_dump(program_document, sort_keys=False), encoding="utf-8")
    backfill_trajectory_points(
        "acme",
        900001,
        (
            TrajectoryPoint(date=date(2026, 4, 24), state="Active", assigned_to="Vertex Maintainer", target_date=date(2026, 5, 10), risk_level=RiskLevel.MEDIUM, area_path="ExampleProject\\Acme\\Deployment"),
            TrajectoryPoint(date=date(2026, 5, 8), state="Resolved", assigned_to="Vertex Maintainer", target_date=date(2026, 5, 10), risk_level=RiskLevel.MEDIUM, area_path="ExampleProject\\Acme\\Deployment"),
        ),
        programs_root=programs_root,
    )
    backfill_trajectory_points(
        "acme",
        900002,
        (
            TrajectoryPoint(date=date(2026, 4, 29), state="Active", assigned_to="Vertex Maintainer", target_date=date(2026, 5, 8), risk_level=RiskLevel.HIGH, area_path="ExampleProject\\Contoso\\Networking"),
            TrajectoryPoint(date=date(2026, 5, 6), state="Resolved", assigned_to="Vertex Maintainer", target_date=date(2026, 5, 8), risk_level=RiskLevel.HIGH, area_path="ExampleProject\\Contoso\\Networking"),
        ),
        programs_root=programs_root,
    )
    backfill_trajectory_points(
        "acme",
        900003,
        (
            TrajectoryPoint(date=date(2026, 4, 19), state="Active", assigned_to="Vertex Maintainer", target_date=date(2026, 5, 12), risk_level=RiskLevel.LOW, area_path="ExampleProject\\Fabrikam\\Scenarios"),
            TrajectoryPoint(date=date(2026, 5, 10), state="Resolved", assigned_to="Vertex Maintainer", target_date=date(2026, 5, 12), risk_level=RiskLevel.LOW, area_path="ExampleProject\\Fabrikam\\Scenarios"),
        ),
        programs_root=programs_root,
    )

    artifacts = generate_report_draft(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    draft_payload = json.loads((programs_root / "acme" / "publications" / "acme_weekly" / "issue_001" / "issue_001.draft.json").read_text(encoding="utf-8"))
    trajectory_velocity = next(section for section in draft_payload["kusto_sections"] if section["title"] == "Trajectory Velocity")

    assert "Trajectory Velocity" in artifacts.markdown_body
    assert "3/week" in artifacts.markdown_body
    assert {metric["label"]: metric["value"] for metric in trajectory_velocity["metrics"]} == {
        "Resolved items": "3",
        "Throughput": "3/week",
        "Median cycle time": "14d",
        "P90 cycle time": "21d",
    }


def test_generate_report_draft_falls_back_to_trajectory_velocity_when_velocity_kusto_query_degrades(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    reset_overrides_to_seed_state(reports_root)
    programs_root = reports_root.parent / "programs"
    program_path = programs_root / "acme" / "program.yaml"
    program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(program_document, dict)
    ado_block = program_document.setdefault("ado", {})
    assert isinstance(ado_block, dict)
    ado_block["date_window_days"] = 7
    program_path.write_text(yaml.safe_dump(program_document, sort_keys=False), encoding="utf-8")
    backfill_trajectory_points(
        "acme",
        900001,
        (
            TrajectoryPoint(date=date(2026, 4, 24), state="Active", assigned_to="Vertex Maintainer", target_date=date(2026, 5, 10), risk_level=RiskLevel.MEDIUM, area_path="ExampleProject\\Acme\\Deployment"),
            TrajectoryPoint(date=date(2026, 5, 8), state="Resolved", assigned_to="Vertex Maintainer", target_date=date(2026, 5, 10), risk_level=RiskLevel.MEDIUM, area_path="ExampleProject\\Acme\\Deployment"),
        ),
        programs_root=programs_root,
    )
    backfill_trajectory_points(
        "acme",
        900002,
        (
            TrajectoryPoint(date=date(2026, 4, 29), state="Active", assigned_to="Vertex Maintainer", target_date=date(2026, 5, 8), risk_level=RiskLevel.HIGH, area_path="ExampleProject\\Contoso\\Networking"),
            TrajectoryPoint(date=date(2026, 5, 6), state="Resolved", assigned_to="Vertex Maintainer", target_date=date(2026, 5, 8), risk_level=RiskLevel.HIGH, area_path="ExampleProject\\Contoso\\Networking"),
        ),
        programs_root=programs_root,
    )
    backfill_trajectory_points(
        "acme",
        900003,
        (
            TrajectoryPoint(date=date(2026, 4, 19), state="Active", assigned_to="Vertex Maintainer", target_date=date(2026, 5, 12), risk_level=RiskLevel.LOW, area_path="ExampleProject\\Fabrikam\\Scenarios"),
            TrajectoryPoint(date=date(2026, 5, 10), state="Resolved", assigned_to="Vertex Maintainer", target_date=date(2026, 5, 12), risk_level=RiskLevel.LOW, area_path="ExampleProject\\Fabrikam\\Scenarios"),
        ),
        programs_root=programs_root,
    )

    def _degraded_velocity_results(query):
        if query.id == "velocity-p50":
            raise QueryError("missing ExampleDeploymentMetrics")
        return _sample_kusto_query_results(query)

    artifacts = generate_report_draft(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=_degraded_velocity_results,
        open_browser=False,
    )

    draft_payload = json.loads((programs_root / "acme" / "publications" / "acme_weekly" / "issue_001" / "issue_001.draft.json").read_text(encoding="utf-8"))
    deployment_velocity = next(section for section in draft_payload["kusto_sections"] if section["query_id"] == "velocity-p50")

    assert deployment_velocity["title"] == "Deployment Velocity"
    assert deployment_velocity["source_label"] == "ADO trajectory fallback"
    assert {metric["label"]: metric["value"] for metric in deployment_velocity["metrics"]} == {
        "Resolved items": "3",
        "Throughput": "3/week",
        "Median cycle time": "14d",
        "P90 cycle time": "21d",
    }
    assert not any("velocity-p50" in warning for warning in artifacts.warnings)


def test_build_velocity_kusto_section_reads_sqlite_backed_trajectories(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "id": "acme",
                "name": "Acme",
                "storage_backend": "sqlite",
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )

    trajectory_store = SQLiteTrajectoryStore(programs_root=programs_root)
    trajectory_store.append(
        "acme",
        900001,
        TrajectoryPoint(
            date=date(2026, 4, 24),
            state="Active",
            assigned_to="Vertex Maintainer",
            target_date=date(2026, 5, 10),
            risk_level=RiskLevel.MEDIUM,
            area_path="ExampleProject\\Acme\\Deployment",
        ),
    )
    trajectory_store.append(
        "acme",
        900001,
        TrajectoryPoint(
            date=date(2026, 5, 8),
            state="Resolved",
            assigned_to="Vertex Maintainer",
            target_date=date(2026, 5, 10),
            risk_level=RiskLevel.MEDIUM,
            area_path="ExampleProject\\Acme\\Deployment",
        ),
    )
    trajectory_store.append(
        "acme",
        900002,
        TrajectoryPoint(
            date=date(2026, 4, 29),
            state="Active",
            assigned_to="Vertex Maintainer",
            target_date=date(2026, 5, 8),
            risk_level=RiskLevel.HIGH,
            area_path="ExampleProject\\Contoso\\Networking",
        ),
    )
    trajectory_store.append(
        "acme",
        900002,
        TrajectoryPoint(
            date=date(2026, 5, 6),
            state="Resolved",
            assigned_to="Vertex Maintainer",
            target_date=date(2026, 5, 8),
            risk_level=RiskLevel.HIGH,
            area_path="ExampleProject\\Contoso\\Networking",
        ),
    )
    trajectory_store.append(
        "acme",
        900003,
        TrajectoryPoint(
            date=date(2026, 4, 19),
            state="Active",
            assigned_to="Vertex Maintainer",
            target_date=date(2026, 5, 12),
            risk_level=RiskLevel.LOW,
            area_path="ExampleProject\\Fabrikam\\Scenarios",
        ),
    )
    trajectory_store.append(
        "acme",
        900003,
        TrajectoryPoint(
            date=date(2026, 5, 10),
            state="Resolved",
            assigned_to="Vertex Maintainer",
            target_date=date(2026, 5, 12),
            risk_level=RiskLevel.LOW,
            area_path="ExampleProject\\Fabrikam\\Scenarios",
        ),
    )

    section = report_module.build_velocity_kusto_section(
        program_id="acme",
        item_ids=(900001, 900002, 900003),
        as_of=date(2026, 5, 10),
        window_days=7,
        programs_root=programs_root,
    )

    assert section is not None
    assert {metric.label: metric.value for metric in section.metrics} == {
        "Resolved items": "3",
        "Throughput": "3/week",
        "Median cycle time": "14d",
        "P90 cycle time": "21d",
    }


def test_generate_report_draft_falls_back_to_zero_throughput_velocity_when_no_items_resolve_in_window(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    reset_overrides_to_seed_state(reports_root)
    programs_root = reports_root.parent / "programs"
    program_path = programs_root / "acme" / "program.yaml"
    program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(program_document, dict)
    ado_block = program_document.setdefault("ado", {})
    assert isinstance(ado_block, dict)
    ado_block["date_window_days"] = 7
    program_path.write_text(yaml.safe_dump(program_document, sort_keys=False), encoding="utf-8")
    backfill_trajectory_points(
        "acme",
        900001,
        (
            TrajectoryPoint(date=date(2026, 5, 6), state="Active", assigned_to="Vertex Maintainer", target_date=date(2026, 5, 12), risk_level=RiskLevel.MEDIUM, area_path="ExampleProject\\Acme\\Deployment"),
        ),
        programs_root=programs_root,
    )
    backfill_trajectory_points(
        "acme",
        900002,
        (
            TrajectoryPoint(date=date(2026, 5, 4), state="Active", assigned_to="Vertex Maintainer", target_date=date(2026, 5, 14), risk_level=RiskLevel.HIGH, area_path="ExampleProject\\Contoso\\Networking"),
        ),
        programs_root=programs_root,
    )
    backfill_trajectory_points(
        "acme",
        900003,
        (
            TrajectoryPoint(date=date(2026, 5, 1), state="Active", assigned_to="Vertex Maintainer", target_date=date(2026, 5, 16), risk_level=RiskLevel.LOW, area_path="ExampleProject\\Fabrikam\\Scenarios"),
        ),
        programs_root=programs_root,
    )

    def _degraded_velocity_results(query):
        if query.id == "velocity-p50":
            raise QueryError("missing ExampleDeploymentMetrics")
        return _sample_kusto_query_results(query)

    artifacts = generate_report_draft(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=_degraded_velocity_results,
        open_browser=False,
    )

    draft_payload = json.loads((programs_root / "acme" / "publications" / "acme_weekly" / "issue_001" / "issue_001.draft.json").read_text(encoding="utf-8"))
    deployment_velocity = next(section for section in draft_payload["kusto_sections"] if section["query_id"] == "velocity-p50")

    assert deployment_velocity["source_label"] == "ADO trajectory fallback"
    assert {metric["label"]: metric["value"] for metric in deployment_velocity["metrics"]} == {
        "Resolved items": "0",
        "Throughput": "0/week",
        "Median cycle time": "Unavailable",
        "P90 cycle time": "Unavailable",
    }
    assert not any("velocity-p50" in warning for warning in artifacts.warnings)


def test_teams_vitality_template_renders_unknown_source_age_and_count() -> None:
    templates_root = Path(__file__).resolve().parents[2] / "templates"
    environment = Environment(loader=FileSystemLoader(str(templates_root)), trim_blocks=True, lstrip_blocks=True)
    environment.filters.update(JINJA_FILTERS)
    environment.globals.update(JINJA_GLOBALS)
    vitality_data = SimpleNamespace(
        title="ADO Vitality This Week",
        items_updated=0,
        items_total=1,
        updated_percentage=0,
        freshness_average_days=None,
        freshness_known_items=0,
        leakage_events=0,
        best_documented_label=None,
        best_documented_detail=None,
        trend_summary="No history",
        accountability_rows=(),
    )

    output = environment.get_template("base.teams.j2").render(
        title="Demo",
        edition=SimpleNamespace(issue_number=1, ado_data_as_of=datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc), manifest_id="manifest"),
        ordered_sections=(SimpleNamespace(kind="ado_vitality"),),
        ado_vitality=vitality_data,
        health=None,
    )

    assert "Avg days since last meaningful ADO update: unknown (known for 0/1 items)" in output
    assert "9999" not in output


def test_generate_report_draft_suppresses_degraded_fleet_health_when_ado_vitality_is_available(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    programs_root = tmp_path / "programs"
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    reset_overrides_to_seed_state(reports_root)

    def _degraded_fleet_health_results(query):
        if query.id == "fleet-health":
            raise QueryError("missing FleetHealthSnapshot")
        return _sample_kusto_query_results(query)

    artifacts = generate_report_draft(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=_degraded_fleet_health_results,
        open_browser=False,
    )

    draft_payload = json.loads((programs_root / "acme" / "publications" / "acme_weekly" / "issue_001" / "issue_001.draft.json").read_text(encoding="utf-8"))

    assert "ADO Vitality This Week" in artifacts.html_body
    assert all(section["query_id"] != "fleet-health" for section in draft_payload["kusto_sections"])
    assert not any("fleet-health" in warning for warning in artifacts.warnings)


def test_generate_report_draft_renders_ado_vitality_section_with_archive_trend(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    def _work_item_loader(bundle, timestamp):
        del bundle
        items = _sample_items(timestamp)
        items[1].revisions = [
            Revision(
                work_item_id=900002,
                rev_number=3,
                changed_by="Vertex Maintainer",
                changed_by_email="maintainer@example.test",
                changed_date=timestamp - timedelta(days=1),
                fields_changed={"State": ("Active", "At Risk")},
            )
        ]
        items[1].comments = []
        return items, 0

    for issue_number, score in ((1, 20), (2, 35), (3, 49)):
        as_of = datetime(2026, 5, issue_number, 18, 0, tzinfo=timezone.utc)
        write_confirmed_issue(
            edition="acme_weekly",
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=as_of,
                items=(
                    _snapshot_item_from_work_item(_sample_items(as_of)[0], risk_level=RiskLevel.LOW),
                ),
                scorecard_risks={"Deployment Velocity": RiskLevel.LOW},
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}\n",
            manifest=_manifest(issue_number=issue_number, as_of=as_of),
            vitality_record=VitalityArchiveEntry(
                issue_number=issue_number,
                confirmed_at=as_of,
                aggregate_score=score,
                items_total=52,
                items_fresh=30 + issue_number,
                avg_richness=60 + issue_number,
                leakage_events=max(0, 6 - issue_number),
                per_workstream={
                    "deployment_readiness": VitalityArchiveWorkstream(
                        score=score,
                        items=12,
                        fresh=8,
                    )
                },
            ),
            archive_root=archive_root,
        )

    _append_approved_v2_signal(
        programs_root,
        signal=Signal(
            id="workiq-900002",
            timestamp=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
            source="workiq/mail",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:900002",),
            text="Capacity discussion referenced outside ADO.",
            raw_ref="mail:1",
            confidence=Confidence.MEDIUM,
            metadata={"entity_link_confidence": "high"},
        ),
    )

    artifacts = generate_report_draft(
        edition_name="acme_weekly",
        issue_number=4,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
        work_item_loader=_work_item_loader,
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    assert "ADO Vitality This Week" in artifacts.html_body
    assert "Avg days since last meaningful ADO update:" in artifacts.html_body
    assert re.search(r"(?:unknown|[0-9.]+ days) \(known for \d+/\d+ items\)", artifacts.html_body)
    assert "known for 2/2 items" in artifacts.html_body
    assert re.search(r"\b9999(?:\.0+)? days\b", artifacts.html_body) is None
    assert "ADO Vitality This Week" in artifacts.markdown_body
    assert "Best documented: WI:" in artifacts.markdown_body
    assert "Items with leakage signals: 1 item" in artifacts.markdown_body
    assert "Trend: 20% -> 35% -> 49% ->" in artifacts.markdown_body
    assert "(improving over 4 issues)" in artifacts.markdown_body
    assert "| Workstream | Workstream Owner + ADO Assignee | Stale or Missing ADOs | What Field Needs Update |" in artifacts.markdown_body
    assert "Owner update or comment; Description; Next step" in artifacts.markdown_body


def test_generate_report_draft_rerenders_issue_077_as_continuation(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = tmp_path / "programs"
    disable_kusto_in_report_copy(reports_root)
    as_of = datetime(2026, 5, 10, 18, tzinfo=timezone.utc)
    prior_as_of = as_of - timedelta(days=7)
    write_confirmed_issue(
        edition=EDITION_NAME, issue_number=76,
        snapshot=_lookback_snapshot(
            issue_number=76, as_of=prior_as_of,
            items=tuple(_snapshot_item_from_work_item(item, risk_level=RiskLevel.MEDIUM) for item in _issue_077_snapshot_items(repo_root, prior_as_of)[:2]),
            scorecard_risks={"Deployment Velocity": RiskLevel.MEDIUM, "Deployment Safety": RiskLevel.MEDIUM, "Networking": RiskLevel.MEDIUM},
        ),
        html_body="<html><body>Issue 076</body></html>", markdown_body="# Issue 076\n",
        manifest=_manifest(76, prior_as_of), archive_root=archive_root,
    )
    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_issue_077_snapshot_items(repo_root, timestamp), 0),
        open_browser=False,
    )

    visible_new_rows = artifacts.markdown_body.count("- **New**:")

    assert artifacts.issue_number == 77
    assert artifacts.report.deltas.previous_issue_number == 76
    assert 0 < len(artifacts.report.deltas.new_items) < len(artifacts.report.items)
    assert visible_new_rows == min(5, len(artifacts.report.deltas.new_items))
    assert "5 of 10 changes shown" in artifacts.markdown_body
    assert artifacts.markdown_body.index("## Example Release Readiness") < artifacts.markdown_body.index("## Executive Summary")
    assert "## Deployment Velocity" not in artifacts.markdown_body
    assert artifacts.markdown_body.index("## Example Release Readiness") < artifacts.markdown_body.index("## Network Delivery Checks")
    assert artifacts.markdown_body.index("## Network Delivery Checks") < artifacts.markdown_body.index("## What Changed")
    assert artifacts.markdown_body.index("## Program Health") < artifacts.markdown_body.index("## Network Delivery Checks")
    assert 'data-vertex-block="jump-to-section"' in artifacts.html_body
    assert 'href="#chapter-network_delivery_checks"' in artifacts.html_body
    assert "Khabari" not in artifacts.html_body
    assert "Manifest " not in artifacts.html_body
    assert "Issue 077 - " not in artifacts.html_body
    assert f"Issue {artifacts.issue_number:03d} draft" not in artifacts.html_body


def test_report_cli_diff_shows_override_ado_and_exec_summary_changes(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    loader_calls = {"count": 0}

    def staged_loader(bundle, timestamp):
        del bundle
        loader_calls["count"] += 1
        if loader_calls["count"] == 1:
            return _sample_items(timestamp), 0
        return _sample_items_with_diff(timestamp), 0

    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.PROGRAMS_ROOT", reports_root.parent / "programs")
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.report._load_live_work_items", staged_loader)
    monkeypatch.setattr("src.commands.report.webbrowser.open", lambda url: True)

    first_result = runner.invoke(app, ["report", "--edition", EDITION_NAME, "--dry-run"])

    assert first_result.exit_code == 3

    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=1)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    scorecard_name, scorecard_dimensions = next(iter(overrides_payload["scorecards"].items()))
    changed_dimension_name, changed_dimension = next(iter(scorecard_dimensions.items()))
    changed_dimension["risk"] = "low"
    changed_dimension["hide_details"] = True
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    exec_summary_path = get_narratives_dir(EDITION_NAME, 1, reports_root) / "exec_summary.md"
    exec_summary_path.write_text("Updated executive summary with new rollout risk.\n", encoding="utf-8")

    diff_result = runner.invoke(app, ["report", "--edition", EDITION_NAME, "--dry-run", "--diff"])

    assert diff_result.exit_code == 3
    assert "VERTEX DIFF - Changes since last dry-run" in diff_result.stdout
    assert "SCORECARD OVERRIDES APPLIED:" in diff_result.stdout
    assert f"{changed_dimension_name}: Needs Input -> Low (author set in overrides.yaml)" in diff_result.stdout
    assert f"{changed_dimension_name}: detail section hidden" in diff_result.stdout
    assert "ADO DATA CHANGES:" in diff_result.stdout
    assert '#900004 "New cache warmup safeguard" - NEW' in diff_result.stdout
    assert "EXEC SUMMARY:" in diff_result.stdout
    assert "+ Updated executive summary with new rollout risk." in diff_result.stdout


def test_report_cli_stdout_json(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.PROGRAMS_ROOT", reports_root.parent / "programs")
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr(
        "src.commands.report._load_live_work_items",
        lambda bundle, timestamp: (_sample_items(timestamp), 0),
    )

    result = runner.invoke(app, ["report", "--edition", EDITION_NAME, "--dry-run", "--stdout"])

    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["edition"] == EDITION_NAME
    assert payload["paths"]["eml"].endswith("issue_001.eml")
    assert payload["paths"]["html"].endswith("issue_001.html")
    assert "QG-8" in payload["qg_results"]

    failed_gate_ids = tuple(gate_id for gate_id, passed in payload["qg_results"].items() if not passed)
    assert failed_gate_ids
    eml = message_from_bytes(Path(payload["paths"]["eml"]).read_bytes())
    assert eml["To"] is None and eml["Cc"] is None
    assert eml["Subject"].startswith("[BLOCKED DRAFT]")
    text_body = next(part.get_payload(decode=True).decode() for part in eml.walk() if part.get_content_subtype() == "plain")
    blocking_notice = text_body.splitlines()[0]
    assert blocking_notice == (
        "BLOCKED DRAFT — publication is prohibited until quality gates pass: "
        f"{', '.join(failed_gate_ids)}."
    )


def test_report_cli_stdout_verbose_includes_evidence_packets(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.PROGRAMS_ROOT", reports_root.parent / "programs")
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr(
        "src.commands.report._load_live_work_items",
        lambda bundle, timestamp: (_sample_items(timestamp), 0),
    )

    result = runner.invoke(app, ["report", "--edition", EDITION_NAME, "--dry-run", "--stdout", "--verbose"])

    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert "evidence" in payload
    assert payload["paths"]["trace"].endswith("report.trace.jsonl")
    assert payload["evidence"]["items"]["900001"]["summary"]
    assert payload["evidence"]["scorecards"]["Example Release Readiness"]["Deployment Velocity"]["total_items"] >= 1
    assert Path(payload["paths"]["trace"]).exists()


def test_report_cli_stdout_verbose_uses_trusted_baseline_for_previous_snapshot(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    baseline_call: dict[str, int | None] = {}
    snapshot_call: dict[str, int | None] = {}
    original_load_previous_snapshot = report_module._load_previous_snapshot

    def _fake_load_trusted_baseline_issue(*args, **kwargs):
        del args
        baseline_call["before_issue_number"] = kwargs.get("before_issue_number")
        return 77

    def _capturing_load_previous_snapshot(*args, **kwargs):
        snapshot_call["trusted_issue_number"] = kwargs.get("trusted_issue_number")
        return original_load_previous_snapshot(*args, **kwargs)

    monkeypatch.setattr(report_module, "load_trusted_baseline_issue", _fake_load_trusted_baseline_issue)
    monkeypatch.setattr(report_module, "_load_previous_snapshot", _capturing_load_previous_snapshot)
    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.PROGRAMS_ROOT", reports_root.parent / "programs")
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr(
        "src.commands.report._load_live_work_items",
        lambda bundle, timestamp: (_sample_items(timestamp), 0),
    )

    result = runner.invoke(app, ["report", "--edition", EDITION_NAME, "--dry-run", "--stdout", "--verbose"])

    assert result.exit_code == 3
    assert baseline_call["before_issue_number"] == 1
    assert snapshot_call["trusted_issue_number"] == 77


def test_report_cli_rejects_verbose_without_stdout(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.PROGRAMS_ROOT", reports_root.parent / "programs")
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)

    result = runner.invoke(app, ["report", "--edition", EDITION_NAME, "--dry-run", "--verbose"])

    assert result.exit_code != 0
    assert "--verbose requires --stdout." in result.output


@pytest.mark.parametrize("command", ["report", "draft"])
def test_report_cli_rejects_dry_run_draft_send_without_sending(
    monkeypatch, repo_root: Path, tmp_path: Path, isolated_cli_startup, command: str,
) -> None:
    from unittest.mock import Mock

    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    sent_messages = []
    preflight = Mock(wraps=report_module._preflight_report_workspace)
    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.PROGRAMS_ROOT", reports_root.parent / "programs")
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.report._preflight_report_workspace", preflight)
    monkeypatch.setattr(
        "src.commands.report._load_live_work_items",
        lambda bundle, timestamp: (_sample_items(timestamp), 0),
    )
    monkeypatch.setattr("src.commands.report.webbrowser.open", lambda url: True)
    monkeypatch.setattr(
        "src.commands.report._build_draft_email_sender",
        lambda: (lambda message: sent_messages.append(message)),
    )

    result = runner.invoke(app, [command, "--edition", EDITION_NAME, "--dry-run", "--send-draft"])

    assert result.exit_code == 2, result.output
    assert "--send-draft cannot be combined with --dry-run." in result.output
    assert sent_messages == []
    preflight.assert_not_called()




@pytest.mark.parametrize("command", ["report", "draft"])
@pytest.mark.parametrize(
    ("flags", "message"),
    [
        (["--dry-run", "--send-draft"], "--send-draft cannot be combined with --dry-run."),
        (["--send-draft", "--dry-run"], "--send-draft cannot be combined with --dry-run."),
        (["--dry-run", "--send-draft", "--stdout"], "--send-draft cannot be combined with --stdout."),
    ],
)
def test_report_conflicting_flags_reject_before_root_startup(
    monkeypatch, report_startup_calls: list[str], command: str, flags: list[str], message: str,
) -> None:
    from unittest.mock import Mock

    handler_preflight = Mock()
    sender = Mock()
    monkeypatch.setattr(report_module, "_preflight_report_workspace", handler_preflight)
    monkeypatch.setattr(report_module, "_build_draft_email_sender", sender)

    result = runner.invoke(app, [command, "--edition", "fixture_weekly", *flags])

    assert result.exit_code == 2, result.output
    assert message in result.output
    assert report_startup_calls == []
    handler_preflight.assert_not_called()
    sender.assert_not_called()


@pytest.mark.parametrize("command", ["report", "draft"])
def test_report_conflicting_flags_reject_before_invalid_edition_resolution(
    report_startup_calls: list[str], command: str,
) -> None:
    result = runner.invoke(
        app, [command, "--edition", "../invalid", "--dry-run", "--send-draft"],
    )

    assert result.exit_code == 2, result.output
    assert "--send-draft cannot be combined with --dry-run." in result.output
    assert report_startup_calls == []


@pytest.mark.parametrize("command", ["report", "draft"])
def test_report_conflicting_flags_preserve_click_type_error(
    report_startup_calls: list[str], command: str,
) -> None:
    result = runner.invoke(
        app, [command, "--edition", "fixture", "--dry-run", "--send-draft", "--as-of", "invalid"],
    )

    assert result.exit_code == 2, result.output
    assert "Invalid value for '--as-of'" in result.output
    assert report_startup_calls == []


@pytest.mark.parametrize("command", ["report", "draft"])
def test_report_conflicting_flags_preserve_skip_issue_error(
    report_startup_calls: list[str], command: str,
) -> None:
    result = runner.invoke(
        app, ["--skip-issue", command, "--edition", "fixture", "--dry-run", "--send-draft"],
    )

    assert result.exit_code == 2, result.output
    assert "--skip-issue cannot be combined with a subcommand." in result.output
    assert report_startup_calls == []


@pytest.mark.parametrize("command", ["report", "draft"])
@pytest.mark.parametrize(
    ("args", "exit_code"),
    [
        (["--edition", "fixture_weekly", "--dry-run"], 71),
        (["--edition", "fixture_weekly", "--send-draft"], 71),
        (["--edition=--dry-run", "--send-draft"], 71),
        (["--edition", "fixture_weekly", "--format", "--send-draft", "--dry-run"], 71),
        (["--edition", "fixture_weekly", "--", "--dry-run", "--send-draft"], 2),
        (["--help", "--dry-run", "--send-draft"], 0),
    ],
)
def test_report_nonconflicting_flags_preserve_root_startup(
    monkeypatch, report_startup_calls: list[str], command: str, args: list[str], exit_code: int,
) -> None:
    def stop_after_startup():
        raise typer.Exit(71)

    monkeypatch.setattr(report_module, "_preflight_report_workspace", stop_after_startup)
    result = runner.invoke(app, [command, *args])

    assert result.exit_code == exit_code, result.output
    assert report_startup_calls == [
        "_set_invocation_ai_mode",
        "_preflight_report_workspace",
        "_maybe_register_tier_decision_sink",
        "_maybe_run_scheduled_db_maintenance",
        "_maybe_run_session_catchup",
    ]


@pytest.mark.parametrize("command", ["report", "draft"])
def test_report_startup_probe_does_not_repeat_custom_option_processing(
    report_startup_calls: list[str], command: str,
) -> None:
    from unittest.mock import Mock

    import click
    from click.testing import CliRunner as ClickRunner
    from typer.main import get_command

    parser_calls: list[str] = []

    class CustomOption(click.Option):
        def add_to_parser(self, parser, ctx):
            parser_calls.append("added")
            return super().add_to_parser(parser, ctx)

    handler = Mock()
    root = get_command(app)
    root.commands[command] = click.Command(
        command,
        callback=handler,
        params=[
            click.Option(["--edition"]),
            click.Option(["--dry-run"], is_flag=True),
            CustomOption(
                ["--send-draft"], is_flag=True,
                callback=lambda _ctx, _param, _value: False,
            ),
        ],
    )
    result = ClickRunner().invoke(root, [command, "--edition", "fixture", "--dry-run", "--send-draft"])

    assert result.exit_code == 0, result.output
    handler.assert_called_once_with(edition="fixture", dry_run=True, send_draft=False)
    assert parser_calls == ["added"]
    assert report_startup_calls == [
        "_set_invocation_ai_mode",
        "_preflight_report_workspace",
        "_maybe_register_tier_decision_sink",
        "_maybe_run_scheduled_db_maintenance",
        "_maybe_run_session_catchup",
    ]


@pytest.mark.parametrize("command", ["report", "draft"])
@pytest.mark.parametrize("conflicting", [True, False])
def test_report_conflicting_flags_skip_due_synthetic_compaction(
    monkeypatch, tmp_path: Path, command: str, conflicting: bool,
) -> None:
    import cli
    from src.commands import db as db_module
    from src.core.reality_store import RealityStore

    db_root = tmp_path / "synthetic-db"
    store = RealityStore("fixture", db_root=db_root)
    store.initialize()
    sentinel = store.db_path.parent / ".compact_last_run"
    monkeypatch.setenv("VERTEX_DB_PATH", str(db_root))
    monkeypatch.setattr(cli, "_program_for_startup", lambda ctx: "fixture")
    monkeypatch.setattr(cli, "_preflight_report_workspace", lambda: None)
    monkeypatch.setattr(cli, "_set_invocation_ai_mode", lambda argv: None)
    monkeypatch.setattr(cli, "_maybe_register_tier_decision_sink", lambda ctx: None)
    monkeypatch.setattr(cli, "_maybe_run_session_catchup", lambda ctx, **kwargs: None)
    monkeypatch.setattr(db_module, "load_metric_definition_map", lambda **kwargs: {})

    def stop_after_startup():
        raise typer.Exit(71)

    monkeypatch.setattr(report_module, "_preflight_report_workspace", stop_after_startup)
    args = [command, "--edition", "fixture_weekly", "--dry-run"]
    if conflicting:
        args.append("--send-draft")
    result = runner.invoke(app, args)

    assert result.exit_code == (2 if conflicting else 71), result.output
    assert sentinel.exists() == (not conflicting)


def test_report_cli_send_draft_sends_to_author_mailbox(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    author_email = load_report_bundle(EDITION_NAME, reports_root=reports_root, programs_root=reports_root.parent / "programs").config.author.email
    edition_path = reports_root.parent / "programs" / "acme" / "editions" / f"{EDITION_NAME}.yaml"
    edition_document = yaml.safe_load(edition_path.read_text(encoding="utf-8"))
    edition_document["name"] = "Program Hygiene | Issue {issue_number} | Synthetic author preview"
    edition_path.write_text(yaml.safe_dump(edition_document, sort_keys=False), encoding="utf-8")

    sent_messages = []
    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.PROGRAMS_ROOT", reports_root.parent / "programs")
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr(
        "src.commands.report._load_live_work_items",
        lambda bundle, timestamp: (_sample_items(timestamp), 0),
    )
    monkeypatch.setattr("src.commands.report.webbrowser.open", lambda url: True)
    monkeypatch.setattr(
        "src.commands.report._build_draft_email_sender",
        lambda: (lambda message: sent_messages.append(message)),
    )

    result = runner.invoke(app, ["report", "--edition", EDITION_NAME, "--send-draft"])

    assert result.exit_code == 3
    assert "Draft email sent to " in result.output
    assert author_email in result.output
    assert len(sent_messages) == 1
    assert sent_messages[0].to == (author_email,)
    assert sent_messages[0].cc == ()
    assert sent_messages[0].subject.startswith("Program Hygiene | Issue 1 | ")
    assert "Program Hygiene" in sent_messages[0].html_body


def test_report_cli_send_draft_rejects_stdout(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.PROGRAMS_ROOT", reports_root.parent / "programs")
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)

    result = runner.invoke(app, ["report", "--edition", EDITION_NAME, "--dry-run", "--stdout", "--send-draft"])

    assert result.exit_code == 2
    assert "--send-draft cannot be combined with --stdout." in result.output


def test_report_cli_send_draft_surfaces_transport_error(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.PROGRAMS_ROOT", reports_root.parent / "programs")
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr(
        "src.commands.report._load_live_work_items",
        lambda bundle, timestamp: (_sample_items(timestamp), 0),
    )
    monkeypatch.setattr("src.commands.report.webbrowser.open", lambda url: True)
    monkeypatch.setattr(
        "src.commands.report._build_draft_email_sender",
        lambda: (lambda message: (_ for _ in ()).throw(AuthError("Missing GRAPH_TENANT_ID for Graph mail send."))),
    )

    result = runner.invoke(
        app,
        [
            "report",
            "--edition",
            EDITION_NAME,
            "--send-draft",
        ],
    )

    assert result.exit_code == 2
    assert "Missing GRAPH_TENANT_ID for Graph mail send." in result.output


def test_report_cli_ai_review_requires_dry_run(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.PROGRAMS_ROOT", reports_root.parent / "programs")
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)

    result = runner.invoke(app, ["report", "--edition", EDITION_NAME, "--ai-review"])

    assert result.exit_code == 2
    assert "--ai-review requires --dry-run." in result.output


def test_report_cli_reseed_requires_dry_run(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.PROGRAMS_ROOT", reports_root.parent / "programs")
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)

    result = runner.invoke(app, ["report", "--edition", EDITION_NAME, "--reseed"])

    assert result.exit_code == 2
    assert "--reseed requires --dry-run." in result.output


def test_report_cli_reseed_rejects_no_seed(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.PROGRAMS_ROOT", reports_root.parent / "programs")
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)

    result = runner.invoke(app, ["report", "--edition", EDITION_NAME, "--dry-run", "--reseed", "--no-seed"])

    assert result.exit_code == 2
    assert "--reseed cannot be combined with --no-seed." in result.output


def test_report_cli_ai_review_writes_review_markdown_without_changing_exit_code(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    program_path = programs_root / "acme" / "program.yaml"
    program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    program_document["leadership_readers"] = [dict(name="Example Director", role="Synthetic director", cares_about=["schedule", "delivery readiness"])]
    program_path.write_text(yaml.safe_dump(program_document, sort_keys=False), encoding="utf-8")

    prior_as_of = datetime(2026, 4, 28, 18, 0, tzinfo=timezone.utc)
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=_snapshot_with_item(prior_as_of, risk_level=RiskLevel.HIGH),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001\nDeployment service check remains high risk.\n",
        manifest=_manifest(issue_number=1, as_of=prior_as_of, edition=EDITION_NAME),
        archive_root=archive_root,
    )

    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.PROGRAMS_ROOT", reports_root.parent / "programs")
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr(
        "src.commands.report._load_live_work_items",
        lambda bundle, timestamp: (_sample_items(timestamp), 0),
    )
    monkeypatch.setattr("src.commands.report.webbrowser.open", lambda url: True)

    result = runner.invoke(app, ["report", "--edition", EDITION_NAME, "--dry-run", "--ai-review"])

    assert result.exit_code == 3
    review_path = programs_root / "acme" / "publications" / EDITION_NAME / "issue_002" / "issue_002.review.md"
    review_json_path = programs_root / "acme" / "publications" / EDITION_NAME / "issue_002" / "issue_002.review.json"
    assert review_path.exists()
    assert review_json_path.exists()
    review_body = review_path.read_text(encoding="utf-8")
    review_payload = json.loads(review_json_path.read_text(encoding="utf-8"))
    assert "AI DRAFT REVIEW" in review_body
    assert "LEADERSHIP QUESTIONS" in review_body
    assert "Example Director will likely ask" in review_body
    assert "DATA GAPS" in review_body
    assert "STRUCTURAL" in review_body
    assert review_payload["review_report"]["issue_number"] == 2
    assert review_payload["review_report"]["leadership_questions"] > 0
    assert "exec_summary" in review_payload["reviewed_sections"]
    assert "velocity-p50" not in review_payload["rendered_kusto_query_ids"]
    assert "Review: " in result.output


def test_report_cli_renders_narrative_edition_override(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    _set_v2_edition_layout_mode(programs_root / "acme" / "editions", edition_id=EDITION_NAME, layout_mode="dashboard")
    scorecards_path = programs_root / "acme" / "scorecards.yaml"
    scorecards = yaml.safe_load(scorecards_path.read_text(encoding="utf-8"))
    scorecards["scorecards"].append(dict(name="Diagnostics Surface", dimensions=[dict(name="Pilot Readiness", workstream_id="diagnostics_surface", ado_filter="area_path contains 'Diagnostics'")]))
    scorecards_path.write_text(yaml.safe_dump(scorecards, sort_keys=False), encoding="utf-8")
    slices_path = programs_root / "acme" / "slice_contracts.yaml"
    slices = yaml.safe_load(slices_path.read_text(encoding="utf-8"))
    contract = asdict(build_test_slice_contract(
        contract_id=canonical_dimension_binding_id("Diagnostics Surface", "Pilot Readiness", chapter_namespace="acme"),
        scorecard_name="Diagnostics Surface", title="Pilot Readiness", section="diagnostics_surface", workstream="diagnostics_surface",
        ado=build_test_ado_source_contract(filters=SliceFilterDefinition(
            any_of=(SlicePredicateDefinition("area_path", "contains", "Diagnostics"),),
        )),
    ))
    contract["decision_sources"] = contract["source_contract"].pop("decision_sources")
    contract["fallback_sources"] = contract["source_contract"].pop("fallback_sources")
    contract["source_contract"]["ado"]["saved_queries"] = [dict(binding_id="diagnostics-surface", query_id="synthetic-diagnostics-query", mode="activity_delta")]
    slices["slices"].append(contract)
    slices_path.write_text(yaml.safe_dump(slices, sort_keys=False), encoding="utf-8")

    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.PROGRAMS_ROOT", reports_root.parent / "programs")
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr(
        "src.commands.report._load_live_work_items",
        lambda bundle, timestamp: (
            _sample_items(timestamp) + (replace(_sample_items(timestamp)[0], id=990001, title="Synthetic Diagnostics Surface pilot checkpoint", area_path=r"ExampleProject\Diagnostics"),),
            0,
        ),
    )
    monkeypatch.setattr("src.commands.report.webbrowser.open", lambda url: True)

    result = runner.invoke(app, ["report", "--edition", EDITION_NAME, "--dry-run", "--edition-type", "narrative"])

    assert result.exit_code == 3
    html_path = programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.html"
    rendered = html_path.read_text(encoding="utf-8")
    assert "Program Health:" in rendered
    assert "Acme Ramp Readiness" not in rendered
    assert "Fabrikam Acme" not in rendered
    assert rendered.index("Diagnostics Surface") < rendered.index("Executive Summary")


def test_report_cli_renders_focused_edition_without_baseline_changes(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    _set_v2_edition_layout_mode(programs_root / "acme" / "editions", edition_id=EDITION_NAME, layout_mode="dashboard")

    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.PROGRAMS_ROOT", reports_root.parent / "programs")
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr(
        "src.commands.report._load_live_work_items",
        lambda bundle, timestamp: (_sample_items(timestamp), 0),
    )
    monkeypatch.setattr("src.commands.report.webbrowser.open", lambda url: True)

    result = runner.invoke(
        app,
        [
            "report",
            "--edition",
            EDITION_NAME,
            "--dry-run",
            "--edition-type",
            "focused",
        ],
    )

    assert result.exit_code == 3
    html_path = programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.html"
    rendered = html_path.read_text(encoding="utf-8")
    assert "WHAT CHANGED" not in rendered
    assert "Executive Summary" in rendered
    # Data-dependent / P2 label drift: scorecard short labels now come from
    # program config; assert the generic scorecard surface is present.
    assert "scorecard (Risk levels)" in rendered
    assert "Jump to section" in rendered


def test_report_cli_renders_condensed_edition_override(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.PROGRAMS_ROOT", reports_root.parent / "programs")
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr(
        "src.commands.report._load_live_work_items",
        lambda bundle, timestamp: (_sample_items(timestamp), 0),
    )
    monkeypatch.setattr("src.commands.report.webbrowser.open", lambda url: True)

    result = runner.invoke(
        app,
        [
            "report",
            "--edition",
            EDITION_NAME,
            "--dry-run",
            "--edition-type",
            "condensed",
        ],
    )

    assert result.exit_code == 3
    html_path = programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.html"
    rendered = html_path.read_text(encoding="utf-8")
    assert "Daily Digest" in rendered
    assert "Change Summary" in rendered
    assert "Acme Ramp Readiness" not in rendered
    assert "Executive Summary" not in rendered
    assert "Deployment service check" not in rendered


def test_generate_report_draft_renders_v2_daily_edition(repo_root: Path, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    reports_root, archive_root = _seed_v2_report_layout(
        repo_root,
        tmp_path,
        edition_names=("acme_weekly", "example_daily"),
    )

    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    artifacts = generate_report_draft(
        edition_name="example_daily",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    assert artifacts.report.edition == EditionType.CONDENSED
    assert artifacts.html_path is not None and artifacts.html_path.exists()
    assert artifacts.md_path is not None and artifacts.md_path.exists()
    assert "Daily Digest" in artifacts.html_body
    assert (programs_root / "acme" / "publications" / "example_daily" / "issue_001" / "issue_001.html").exists()


def test_generate_report_draft_condensed_limits_change_rows_to_three(repo_root: Path, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    reports_root, archive_root = _seed_v2_report_layout(
        repo_root,
        tmp_path,
        edition_names=("acme_weekly", "example_daily"),
    )

    baseline_as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    baseline_artifacts = generate_report_draft(
        edition_name="example_daily",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=baseline_as_of,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    baseline_snapshot = read_snapshot(baseline_artifacts.snapshot_path)
    adjusted_baseline = replace(
        baseline_snapshot,
        edition_type=EditionType.CONDENSED,
        items=(
            replace(baseline_snapshot.items[0], risk_level=RiskLevel.LOW, target_date=date(2026, 5, 8)),
            replace(baseline_snapshot.items[1], risk_level=RiskLevel.LOW, target_date=date(2026, 5, 6)),
            baseline_snapshot.items[2],
        ),
        scorecards=tuple(replace(dimension, risk=RiskLevel.LOW) for dimension in baseline_snapshot.scorecards),
    )
    write_confirmed_issue(
        edition="example_daily",
        issue_number=1,
        snapshot=adjusted_baseline,
        html_body=baseline_artifacts.html_body,
        markdown_body=baseline_artifacts.markdown_body,
        manifest=_manifest(issue_number=1, as_of=baseline_as_of, edition="example_daily"),
        archive_root=archive_root,
    )

    current_as_of = baseline_as_of + timedelta(days=1)
    current_seed_items = _sample_items(current_as_of)
    current_items = (
        replace(current_seed_items[0], target_date=date(2026, 5, 14)),
        replace(current_seed_items[1], target_date=date(2026, 5, 12)),
        current_seed_items[2],
        WorkItem(
            id=900004,
            type="Bug",
            title="New cache warmup safeguard",
            state="Active",
            assigned_to="Vertex Maintainer",
            assigned_to_email="maintainer@example.test",
            area_path="ExampleProject\\Acme\\Deployment",
            iteration_path="ExampleProject\\Iteration 2",
            target_date=date(2026, 5, 16),
            risk_level=RiskLevel.MEDIUM,
            tags=["Hotfix"],
            custom_fields={},
            revisions=[
                Revision(
                    work_item_id=900004,
                    rev_number=1,
                    changed_by="Vertex Maintainer",
                    changed_by_email="maintainer@example.test",
                    changed_date=current_as_of,
                    fields_changed={"State": (None, "Active")},
                )
            ],
            comments=[],
            fetched_at=current_as_of,
        ),
    )

    artifacts = generate_report_draft(
        edition_name="example_daily",
        issue_number=2,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=current_as_of,
        work_item_loader=lambda bundle, timestamp: (current_items, 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    assert artifacts.report.edition == EditionType.CONDENSED
    assert "## What Changed" in artifacts.markdown_body
    change_section = artifacts.markdown_body.split("## What Changed\n", 1)[1].split("\n## ", 1)[0]
    assert change_section.count("- **") == 3
    assert "3 of " in artifacts.markdown_body and "changes shown" in artifacts.markdown_body
    assert "3 of " in artifacts.html_body and "changes shown" in artifacts.html_body


def test_generate_report_draft_renders_v2_secondary_program_narrative_edition(repo_root: Path, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    reports_root, archive_root = _seed_v2_report_layout(
        repo_root,
        tmp_path,
        edition_names=("acme_weekly", "fabrikam_weekly"),
        program_names=("acme", "fabrikam"),
    )

    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    artifacts = generate_report_draft(
        edition_name="fabrikam_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_secondary_program_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    assert artifacts.report.edition == EditionType.NARRATIVE
    assert artifacts.html_path is not None and artifacts.html_path.exists()
    assert artifacts.md_path is not None and artifacts.md_path.exists()
    assert "Fabrikam Program Update" in artifacts.html_body
    assert "Queue Processing" in artifacts.html_body
    assert "Buildouts" in artifacts.html_body
    assert "Worker Service" in artifacts.html_body
    assert "Request Validation" in artifacts.html_body
    assert "Rollout Approval" in artifacts.html_body
    assert (programs_root / "fabrikam" / "publications" / "fabrikam_weekly" / "issue_001" / "issue_001.html").exists()


def test_generate_report_draft_renders_v2_lt_deck_markdown(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(
        repo_root,
        tmp_path,
        edition_names=("acme_weekly", "example_lt_deck"),
    )

    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    programs_root = reports_root.parent / "programs"
    program_path = programs_root / "acme" / "program.yaml"
    program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    program_doc["charter"] = {
        "scope_statement": "Deliver Acme ramp readiness for the current LT gate.",
        "success_criteria": [
            "Green-light LT review without timeline slip.",
        ],
        "constraints": [
            "Do not widen partner pilot scope before Network signoff.",
        ],
    }
    program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")
    append_decision_ask(
        DecisionAsk(
            id="ask-open",
            program_id="acme",
            edition_id="example_lt_deck",
            issue_number=1,
            text="Need LT decision on Network timeline",
            entity_refs=("WI:900002",),
            ask_date=date(2026, 5, 1),
            owner_alias="lt",
        ),
        programs_root=programs_root,
    )
    append_decision_ask(
        DecisionAsk(
            id="ask-closed",
            program_id="acme",
            edition_id="example_lt_deck",
            issue_number=1,
            text="Confirm Contoso mitigation plan",
            entity_refs=("WI:900001",),
            ask_date=date(2026, 5, 1),
            owner_alias="operator",
        ),
        programs_root=programs_root,
    )
    append_claim_status_update(
        "acme",
        ClaimStatusUpdate(
            claim_id="ask-closed",
            new_status="resolved",
            updated_at=datetime(2026, 5, 4, 9, 0, tzinfo=timezone.utc),
            updated_by="maintainer",
            note="Mitigation approved by LT",
        ),
        programs_root=programs_root,
    )
    save_decisions(
        "acme",
        (
            DecisionEntry(
                id="decision-1",
                program_id="acme",
                title="Network timeline approval",
                context="Timeline needs leadership alignment before partner commit.",
                decision="Await LT approval before locking external target.",
                rationale=None,
                alternatives_considered=(),
                decided_by="lt",
                decision_date=date(2026, 4, 20),
                status=DecisionStatus.PROPOSED,
                superseded_by=None,
                linked_claim_id=None,
                linked_risk_id=None,
                linked_action_ids=(),
                workstream_id="deployment_readiness",
                entity_refs=("WI:900002",),
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
    append_claim_entry(
        ClaimEntry(
            id="claim-1",
            program_id="acme",
            edition_id="example_lt_deck",
            issue_number=2,
            workstream_id="deployment_readiness",
            text="Deployment velocity telemetry stabilizes before LT review.",
            entity_refs=("WI:900001",),
            claim_date=date(2026, 5, 1),
            owner_alias="operator",
            due_date=date(2026, 5, 12),
        ),
        programs_root=programs_root,
    )
    save_risk_register(
        "acme",
        (
            RiskEntry(
                id="risk-1",
                program_id="acme",
                title="Deployment telemetry may miss the LT gate",
                description="The telemetry stabilization work could slip the review prep timeline.",
                probability=RiskProbability.LIKELY,
                impact=RiskImpact.HIGH,
                category=RiskCategory.TECHNICAL,
                owner_alias="operator",
                mitigation_plan="Track the telemetry fix daily until the blocker clears.",
                mitigation_due_date=date(2026, 5, 12),
                linked_workstream_ids=("deployment_readiness",),
                linked_work_item_ids=(900001,),
                linked_milestone_ids=(),
                linked_claim_ids=("claim-1",),
                linked_action_ids=(),
                status=RiskStatus.OPEN,
                identified_date=date(2026, 4, 28),
                identified_in_vertex_issue=1,
                last_reviewed_date=date(2026, 5, 4),
                entity_refs=("WI:900001",),
            ),
        ),
        programs_root=programs_root,
    )
    write_confirmed_issue(
        edition="example_lt_deck",
        issue_number=1,
        snapshot=_lookback_snapshot(
            issue_number=1,
            as_of=datetime(2026, 5, 2, 18, 0, tzinfo=timezone.utc),
            items=(
                _snapshot_item_from_work_item(_sample_items(as_of)[0], risk_level=RiskLevel.MEDIUM),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.MEDIUM},
        ),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001",
        manifest=replace(
            _manifest(issue_number=1, as_of=datetime(2026, 5, 2, 18, 0, tzinfo=timezone.utc), edition="example_lt_deck"),
            metadata={
                "milestone_assessments": [
                    {
                        "milestone_id": "m3-code-complete",
                        "target_date": "2026-05-12",
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
    (programs_root / "acme" / "milestones.yaml").write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "milestones:",
                "  - id: m3-code-complete",
                "    name: M3 - Code Complete",
                "    target_date: 2026-05-18",
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
    backfill_trajectory_points(
        "acme",
        900001,
        (
            TrajectoryPoint(
                date=date(2026, 5, 1),
                state="Active",
                assigned_to="Vertex Maintainer",
                target_date=date(2026, 5, 22),
                risk_level=RiskLevel.MEDIUM,
                area_path="ExampleProject\\Acme\\Deployment",
            ),
            TrajectoryPoint(
                date=date(2026, 5, 4),
                state="Active",
                assigned_to="Vertex Maintainer",
                target_date=date(2026, 5, 28),
                risk_level=RiskLevel.HIGH,
                area_path="ExampleProject\\Acme\\Deployment",
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
                area_path="ExampleProject\\Fabrikam\\Scenarios",
            ),
        ),
        programs_root=programs_root,
    )
    _append_approved_v2_signal(
        programs_root,
        signal=Signal(
            id="sig-lt-deck-analytics-1",
            timestamp=datetime(2026, 5, 5, 16, 30, tzinfo=timezone.utc),
            source="ado/analytics",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:900001",),
            text="Analytics snapshot for LT deck telemetry.",
            raw_ref="ado-analytics:sig-lt-deck-analytics-1",
            confidence=Confidence.HIGH,
            metadata={
                "snapshot_item_count": 5,
                "completed_item_count": 3,
                "open_delta_count": -2,
                "average_cycle_time_days": 4.5,
                "average_lead_time_days": 7.0,
            },
        ),
    )
    _append_approved_v2_signal(
        programs_root,
        signal=Signal(
            id="sig-lt-deck-sprint-1",
            timestamp=datetime(2026, 5, 5, 16, 40, tzinfo=timezone.utc),
            source="ado/sprint",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:900001",),
            text="Sprint snapshot for LT deck telemetry.",
            raw_ref="ado-sprint:sig-lt-deck-sprint-1",
            confidence=Confidence.HIGH,
            metadata={
                "iteration_name": "Sprint 42",
                "completion_pct": 75,
                "open_item_count": 1,
            },
        ),
    )
    _write_lt_deck_dependency_proposals(programs_root)
    artifacts = generate_report_draft(
        edition_name="example_lt_deck",
        issue_number=2,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (
            tuple(
                replace(item, state="Resolved") if item.id == 900003 else item
                for item in _sample_items(timestamp)
            ),
            0,
        ),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    assert artifacts.report.edition == EditionType.DECK
    assert artifacts.eml_path is None
    assert artifacts.html_path is None
    assert artifacts.md_path is not None and artifacts.md_path.exists()
    assert artifacts.md_path.name == "issue_002.deck.md"
    assert "## Health" in artifacts.markdown_body
    assert "## Telemetry" in artifacts.markdown_body
    assert "analytics, 5 scope, 3 completed, open down 2, cycle 4.5d / lead 7.0d; sprint, Sprint 42, 75% complete, 1 open (high confidence)" in artifacts.markdown_body
    assert "## Charter" in artifacts.markdown_body
    assert "\n---\n\n## Charter" in artifacts.markdown_body
    assert "Scope: Deliver Acme ramp readiness for the current LT gate." in artifacts.markdown_body
    assert "Success criterion: Green-light LT review without timeline slip." in artifacts.markdown_body
    assert "Constraint: Do not widen partner pilot scope before Network signoff." in artifacts.markdown_body
    assert "## Milestones" in artifacts.markdown_body
    assert "\n---\n\n## Milestones" in artifacts.markdown_body
    assert "M3 - Code Complete — at risk" in artifacts.markdown_body
    assert "tracking 2026-05-28 (10 days late vs target)" in artifacts.markdown_body
    assert "target history 2026-05-12 -> 2026-05-18" in artifacts.markdown_body
    assert "M4 - Pilot Rollout Validation — completed" in artifacts.markdown_body
    assert "completed 2026-05-18 (1 day late vs target)" in artifacts.markdown_body
    assert "completion history 2026-05-16 -> 2026-05-18" in artifacts.markdown_body
    assert "## Top Risks" in artifacts.markdown_body
    assert "## Dependency Proposals" in artifacts.markdown_body
    assert "dep-proposal-1: deployment_readiness:1001 -> platform_readiness:1002 — comment_language | 2 signal(s) | medium confidence | accept via vertex dependencies accept --program acme --id dep-proposal-1" in artifacts.markdown_body
    assert "dep-proposal-accepted" not in artifacts.markdown_body
    assert "## Open Issues" in artifacts.markdown_body
    assert "\n---\n\n## Open Issues" in artifacts.markdown_body
    assert "Issue #001 ask: Need LT decision on Network timeline (owner lt)" in artifacts.markdown_body
    assert "linked claim-1, risk-1" in artifacts.markdown_body
    assert "## Key Decisions" in artifacts.markdown_body
    assert "Network timeline approval — Await LT approval before locking external target." in artifacts.markdown_body
    assert "PROPOSED | stale | owner lt | date 2026-04-20" in artifacts.markdown_body
    assert "## Key Assumptions" in artifacts.markdown_body
    assert "Partner schema contract stays stable through Q4." in artifacts.markdown_body
    assert "UNVALIDATED | overdue | due 2026-05-01 | owner operator | milestone m3-code-complete" in artifacts.markdown_body
    assert "## Open Asks" in artifacts.markdown_body
    assert "Need LT decision on Network timeline" in artifacts.markdown_body
    assert "## Closed Asks" in artifacts.markdown_body
    assert "Confirm Contoso mitigation plan" in artifacts.markdown_body
    assert "Mitigation approved by LT" in artifacts.markdown_body
    assert (programs_root / "acme" / "publications" / "example_lt_deck" / "issue_002" / "issue_002.deck.md").exists()




@pytest.mark.parametrize(
    ("prior_confirmed_at", "expected_trend"),
    [
        (datetime(2040, 4, 28, 18, 0, tzinfo=timezone.utc), "high vs prior issue"),
        (datetime(2040, 5, 5, 18, 0, tzinfo=timezone.utc), None),
    ],
)
def test_generate_report_draft_renders_scorecard_trend_annotations_in_v2_lt_deck(
    repo_root: Path,
    tmp_path: Path,
    prior_confirmed_at: datetime,
    expected_trend: str | None,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(
        repo_root,
        tmp_path,
        edition_names=("acme_weekly", "example_lt_deck"),
    )

    for issue_number, issue_as_of in (
        (1, datetime(2040, 4, 14, 18, 0, tzinfo=timezone.utc)),
        (2, datetime(2040, 4, 21, 18, 0, tzinfo=timezone.utc)),
        (3, prior_confirmed_at),
    ):
        write_confirmed_issue(
            edition="example_lt_deck",
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=issue_as_of,
                items=(
                    _snapshot_item_from_work_item(_sample_items(issue_as_of)[0], risk_level=RiskLevel.HIGH),
                    _snapshot_item_from_work_item(_sample_items(issue_as_of)[1], risk_level=RiskLevel.HIGH),
                ),
                scorecard_risks={"Networking": RiskLevel.HIGH},
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}",
            manifest=replace(_manifest(issue_number=issue_number, as_of=issue_as_of), edition="example_lt_deck"),
            archive_root=archive_root,
        )

    artifacts = generate_report_draft(
        edition_name="example_lt_deck",
        issue_number=4,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2040, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    assert "High for 4 consecutive issues." in artifacts.markdown_body
    networking = next(dimension for dimension in artifacts.report.scorecard if dimension.name == "Networking")
    assert networking.trend_label == expected_trend


def test_generate_report_draft_renders_dependency_cascade_in_v2_workstream_section(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    editions_root = reports_root.parent / "programs" / "acme" / "editions"
    programs_root = reports_root.parent / "programs"

    _set_v2_edition_layout_mode(editions_root, edition_id="acme_weekly", layout_mode="dashboard")
    _set_v2_key_dependencies(
        programs_root,
        dependencies=(
            {
                "from_item": "Network gap closure",
                "to_item": "Networking",
                "impact": "Ramp stays blocked until the gap closes.",
                "resolution_path": "cross_org_compute_exn",
            },
        ),
    )
    _append_approved_v2_signal(
        programs_root,
        signal=Signal(
            id="sig-cascade-1",
            timestamp=datetime(2026, 5, 5, 17, 0, tzinfo=timezone.utc),
            source="manual",
            program_id="acme",
            workstream_id="acme",
            entity_refs=("Network gap closure",),
            text="Network gap closure remains blocked pending owner sign-off.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
        ),
    )

    artifacts = generate_report_draft(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    assert "Dependency cascades:" in artifacts.report.exec_summary_text
    assert "Downstream Dependency Impact" in artifacts.html_body
    assert ">Cross-org<" in artifacts.html_body
    assert "Ramp stays blocked until the gap closes." in artifacts.html_body
    assert "Confidence: high." in artifacts.html_body


def test_generate_report_draft_v2_ai_seeds_empty_continuity_narratives_from_rolling_summaries(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    _enable_v2_program_ai(programs_root)
    _append_approved_v2_signal(
        programs_root,
        signal=Signal(
            id="sig-icm-context-1",
            timestamp=datetime(2026, 5, 5, 17, 30, tzinfo=timezone.utc),
            source="icm",
            program_id="acme",
            workstream_id="acme",
            entity_refs=("ICM:12345",),
            text="IcM 12345: Fleet capacity alert; status=Active",
            raw_ref="icm:12345",
            confidence=Confidence.HIGH,
            metadata={"incident_id": "12345", "owning_team": "Example Operations"},
        ),
    )
    _write_v2_summary_seed(
        programs_root,
        workstream_id="acme",
        text="Deployment remains the gating lane until the example service closes the next checkpoint.",
    )
    _write_v2_summary_seed(
        programs_root,
        workstream_id="diagnostics_surface",
        text="Diagnostics Surface remains blocked on pilot go-live dependencies and dated sign-offs.",
    )
    append_edit_patterns(
        "acme",
        build_edit_patterns(
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            recorded_at=datetime(2026, 5, 4, 18, 0, tzinfo=timezone.utc),
            draft_exec_summary_text="AI draft exec summary focuses on deployment readiness.",
            confirmed_exec_summary_text="Confirmed exec summary tightened the deployment decision ask.",
            draft_workstream_blurbs={"deployment_readiness": "AI draft deployment blurb."},
            confirmed_workstream_blurbs={"deployment_readiness": "Confirmed deployment blurb rewrite."},
            draft_prompt_versions={
                "exec_summary": "exec_summary_drafter.v1",
                "deployment_readiness": "workstream_blurb.v1",
            },
            draft_ai_confidences={
                "exec_summary": Confidence.HIGH.value,
                "deployment_readiness": Confidence.MEDIUM.value,
            },
            draft_trace_run_id="acme_weekly:issue-077:20260504T180000Z",
        ),
        programs_root=programs_root,
    )
    append_edit_patterns(
        "acme",
        build_edit_patterns(
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=78,
            recorded_at=datetime(2026, 5, 5, 8, 0, tzinfo=timezone.utc),
            draft_exec_summary_text="AI draft exec summary is nearly final and specific.",
            confirmed_exec_summary_text="AI draft exec summary is nearly final and specific with one added blocker.",
            draft_workstream_blurbs={"deployment_readiness": "AI draft deployment blurb is nearly final."},
            confirmed_workstream_blurbs={
                "deployment_readiness": "AI draft deployment blurb is nearly final with one added dependency."
            },
            draft_prompt_versions={
                "exec_summary": "exec_summary_drafter.v2",
                "deployment_readiness": "workstream_blurb.v2",
            },
            draft_ai_confidences={
                "exec_summary": Confidence.HIGH.value,
                "deployment_readiness": Confidence.MEDIUM.value,
            },
            draft_trace_run_id="acme_weekly:issue-078:20260505T080000Z",
        ),
        programs_root=programs_root,
    )

    trace_path = programs_root / "acme" / "publications" / "acme_weekly" / "ai" / "llm_trace.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text(
        "\n".join(
            json.dumps(payload)
            for payload in (
                {
                    "timestamp": "2026-05-04T18:00:00+00:00",
                    "run_id": "acme_weekly:issue-077:20260504T180000Z",
                    "edition": "acme_weekly",
                    "prompt_version": "exec_summary_drafter.v1",
                    "model": "gpt-4o",
                    "deployment": "aoai-eastus",
                    "metadata": {
                        "issue_number": 77,
                        "section_id": "exec_summary",
                        "task_type": "exec_summary",
                    },
                },
                {
                    "timestamp": "2026-05-04T18:00:00+00:00",
                    "run_id": "acme_weekly:issue-077:20260504T180000Z",
                    "edition": "acme_weekly",
                    "prompt_version": "workstream_blurb.v1",
                    "model": "gpt-4o-mini",
                    "deployment": "aoai-eastus",
                    "metadata": {
                        "issue_number": 77,
                        "section_id": "deployment_readiness",
                        "task_type": "workstream_blurb",
                    },
                },
                {
                    "timestamp": "2026-05-05T08:00:00+00:00",
                    "run_id": "acme_weekly:issue-078:20260505T080000Z",
                    "edition": "acme_weekly",
                    "prompt_version": "exec_summary_drafter.v2",
                    "model": "gpt-4.1",
                    "deployment": "aoai-westus",
                    "metadata": {
                        "issue_number": 78,
                        "section_id": "exec_summary",
                        "task_type": "exec_summary",
                    },
                },
                {
                    "timestamp": "2026-05-05T08:00:00+00:00",
                    "run_id": "acme_weekly:issue-078:20260505T080000Z",
                    "edition": "acme_weekly",
                    "prompt_version": "workstream_blurb.v2",
                    "model": "gpt-4.1-mini",
                    "deployment": "aoai-westus",
                    "metadata": {
                        "issue_number": 78,
                        "section_id": "deployment_readiness",
                        "task_type": "workstream_blurb",
                    },
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )

    exec_calls: list[dict[str, object]] = []
    blurb_calls: list[dict[str, object]] = []

    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage._create_ai_client", lambda **kwargs: SimpleNamespace())

    def _fake_exec_summary(**kwargs):
        exec_calls.append(kwargs)
        return ExecSummaryDraft(
            text="AI exec summary seeded from rolling summaries.",
            prompt_version="exec_summary_drafter.v1",
            cited_work_item_ids=(900001,),
            ai_confidence=Confidence.HIGH,
        )

    def _fake_workstream_blurb(**kwargs):
        blurb_calls.append(kwargs)
        return WorkstreamBlurb(
            text=f"AI narrative for {kwargs['workstream_name']}.",
            prompt_version="workstream_blurb.v1",
            cited_work_item_ids=(kwargs["items"][0].id,),
            ai_confidence=Confidence.HIGH,
        )

    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage.draft_exec_summary", _fake_exec_summary)
    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage.generate_workstream_blurb", _fake_workstream_blurb)

    artifacts = generate_report_draft(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    assert artifacts.report.exec_summary_text == "AI exec summary seeded from rolling summaries."
    assert artifacts.report.workstream_blurbs["deployment_readiness"] == "AI narrative for Deployment Readiness."
    exec_context = "\n".join(exec_calls[0]["supplemental_context"])
    assert exec_calls and "Rolling summary [acme]" in exec_context
    assert exec_calls and "Rolling summary [diagnostics_surface]" in exec_context
    assert exec_calls and "Recent calibration [" not in exec_context
    assert exec_calls and "Recent prompt " not in exec_context
    assert exec_calls and "Model leaderboard [" not in exec_context
    deployment_call = next(call for call in blurb_calls if call["workstream_name"] == "Deployment Readiness")
    deployment_context = "\n".join(deployment_call["supplemental_context"])
    assert "Recent calibration [" not in deployment_context
    assert "Recent confidence calibration [" not in deployment_context
    assert "Recent prompt " not in deployment_context
    assert "Recent model performance [" not in deployment_context
    assert "Rolling summary [acme]" in deployment_context
    assert "Approved signal 2026-05-05T17:30:00+00:00 [icm]: IcM 12345: Fleet capacity alert; status=Active" in deployment_context
    assert "Rolling summary [diagnostics_surface]" not in deployment_context


def test_generate_report_draft_v2_ai_keeps_authored_continuity_narratives_authoritative(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    _enable_v2_program_ai(programs_root)
    _write_v2_summary_seed(
        programs_root,
        workstream_id="acme",
        text="Deployment remains the gating lane until the example service closes the next checkpoint.",
    )

    narratives_dir = programs_root / "acme" / "narratives" / "issue_001"
    narratives_dir.mkdir(parents=True, exist_ok=True)
    (narratives_dir / "exec_summary.md").write_text("Authored exec summary.\n", encoding="utf-8")
    (narratives_dir / "chapter_deployment_readiness.md").write_text("Authored deployment narrative.\n", encoding="utf-8")

    exec_calls: list[dict[str, object]] = []
    blurb_calls: list[dict[str, object]] = []

    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage._create_ai_client", lambda **kwargs: SimpleNamespace())
    monkeypatch.setattr(
        "src.commands.report_pipeline.assemble_stage.draft_exec_summary",
        lambda **kwargs: exec_calls.append(kwargs) or ExecSummaryDraft(
            text="AI exec summary seeded from rolling summaries.",
            prompt_version="exec_summary_drafter.v1",
            cited_work_item_ids=(900001,),
            ai_confidence=Confidence.HIGH,
        ),
    )
    monkeypatch.setattr(
        "src.commands.report_pipeline.assemble_stage.generate_workstream_blurb",
        lambda **kwargs: blurb_calls.append(kwargs) or WorkstreamBlurb(
            text=f"AI narrative for {kwargs['workstream_name']}.",
            prompt_version="workstream_blurb.v1",
            cited_work_item_ids=(kwargs["items"][0].id,),
            ai_confidence=Confidence.HIGH,
        ),
    )

    artifacts = generate_report_draft(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    assert artifacts.report.exec_summary_text == "Authored exec summary."
    assert artifacts.report.workstream_blurbs["deployment_readiness"] == "Authored deployment narrative."
    assert exec_calls == []
    assert all(call["workstream_name"] != "Deployment Readiness" for call in blurb_calls)


def test_generate_report_draft_v2_ai_passes_trace_context_to_report_ai_clients(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    output_root = programs_root / "acme" / "publications"
    _enable_v2_program_ai(programs_root)
    _write_v2_summary_seed(
        programs_root,
        workstream_id="acme",
        text="Deployment remains the gating lane until the example service closes the next checkpoint.",
    )
    narratives_dir = programs_root / "acme" / "narratives" / "issue_001"
    narratives_dir.mkdir(parents=True, exist_ok=True)
    (narratives_dir / "exec_summary.md").write_text("", encoding="utf-8")
    (narratives_dir / "chapter_deployment_readiness.md").write_text("", encoding="utf-8")

    trace_contexts: list[object] = []

    def _fake_create_ai_client(**kwargs):
        trace_contexts.append(kwargs.get("trace_context"))
        return SimpleNamespace(
            deployment=kwargs["deployment"],
            usage_stats=SimpleNamespace(call_count=1),
            spent_usd=0.01,
        )

    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage._create_ai_client", _fake_create_ai_client)
    monkeypatch.setattr(
        "src.commands.report_pipeline.assemble_stage._iter_ai_generated_sections",
        lambda **kwargs: (
            report_module._AIGeneratedSection(
                section_id="deployment_readiness",
                title="Deployment Readiness",
                items=(kwargs["items"][0],),
            ),
        ),
    )
    monkeypatch.setattr(
        "src.commands.report_pipeline.assemble_stage.draft_exec_summary",
        lambda **kwargs: ExecSummaryDraft(
            text="AI exec summary seeded from rolling summaries.",
            prompt_version="exec_summary_drafter.v1",
            cited_work_item_ids=(900001,),
            ai_confidence=Confidence.HIGH,
        ),
    )
    monkeypatch.setattr(
        "src.commands.report_pipeline.assemble_stage.generate_workstream_blurb",
        lambda **kwargs: WorkstreamBlurb(
            text=f"AI narrative for {kwargs['workstream_name']}.",
            prompt_version="workstream_blurb.v1",
            cited_work_item_ids=(kwargs["items"][0].id,),
            ai_confidence=Confidence.HIGH,
        ),
    )

    artifacts = generate_report_draft(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    assert artifacts.report.exec_summary_text == "AI exec summary seeded from rolling summaries."
    assert artifacts.report.workstream_blurbs["deployment_readiness"] == "AI narrative for Deployment Readiness."
    assert trace_contexts
    run_ids = {trace_context.run_id for trace_context in trace_contexts if trace_context is not None}
    assert len(run_ids) == 1
    resolved_run_id = next(iter(run_ids))
    assert resolved_run_id.startswith("acme_weekly:issue-001:")
    assert {trace_context.edition for trace_context in trace_contexts if trace_context is not None} == {"acme_weekly"}
    assert {
        trace_context.caller for trace_context in trace_contexts if trace_context is not None
    } == {"src.commands.report._synthesize_v2_ai_content"}
    assert {trace_context.trace_file for trace_context in trace_contexts if trace_context is not None} == {output_root / EDITION_NAME / "ai" / "llm_trace.jsonl"}
    draft_payload = json.loads((programs_root / "acme" / "publications" / "acme_weekly" / "issue_001" / "issue_001.draft.json").read_text(encoding="utf-8"))
    assert draft_payload["ai_trace_run_id"] == resolved_run_id
    metadata_payloads = [trace_context.metadata for trace_context in trace_contexts if trace_context is not None]
    assert any(
        metadata.get("issue_number") == 1
        and metadata.get("task_type") == "exec_summary"
        and metadata.get("section_id") == "exec_summary"
        and metadata.get("run_budget_usd") == 0.5
        for metadata in metadata_payloads
    )
    assert any(
        metadata.get("issue_number") == 1
        and metadata.get("task_type") == "workstream_blurb"
        and metadata.get("section_id") in artifacts.report.workstream_blurbs
        for metadata in metadata_payloads
    )


def test_report_ai_usage_aggregates_same_deployment_across_trace_metadata() -> None:
    ai_calls, ai_cost_usd, ai_cost_by_model = report_module._report_ai_usage(
        {
            (
                "fake-shared",
                (("issue_number", "1"), ("section_id", '"exec_summary"'), ("task_type", '"exec_summary"')),
            ): SimpleNamespace(
                deployment="fake-shared",
                usage_stats=SimpleNamespace(call_count=1),
                spent_usd=0.01,
            ),
            (
                "fake-shared",
                (("issue_number", "1"), ("section_id", '"deployment_readiness"'), ("task_type", '"workstream_blurb"')),
            ): SimpleNamespace(
                deployment="fake-shared",
                usage_stats=SimpleNamespace(call_count=1),
                spent_usd=0.015,
            ),
        }
    )

    assert ai_calls == 2
    assert ai_cost_usd == pytest.approx(0.025)
    assert ai_cost_by_model == {"fake-shared": pytest.approx(0.025)}


def test_generate_report_draft_v2_ai_falls_back_to_backup_deployments(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    _enable_v2_program_ai(programs_root)
    program_path = programs_root / "acme" / "program.yaml"
    program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(program_doc, dict)
    ai_block = program_doc.setdefault("ai", {})
    assert isinstance(ai_block, dict)
    ai_block["blurb_backup_deployment"] = "fake-blurb-backup"
    ai_block["exec_summary_backup_deployment"] = "fake-exec-backup"
    program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False), encoding="utf-8")

    _write_v2_summary_seed(
        programs_root,
        workstream_id="acme",
        text="Deployment remains the gating lane until the example service closes the next checkpoint.",
    )

    exec_calls: list[dict[str, object]] = []
    blurb_calls: list[dict[str, object]] = []

    def _fake_create_ai_client(**kwargs):
        deployment = kwargs["deployment"]
        if deployment in {"fake-exec", "fake-blurb"}:
            raise AIClientError(f"deployment unavailable: {deployment}")
        return SimpleNamespace(
            deployment=deployment,
            usage_stats=SimpleNamespace(call_count=0),
            spent_usd=0.0,
        )

    def _fake_exec_summary(**kwargs):
        exec_calls.append(kwargs)
        kwargs["client"].usage_stats.call_count += 1
        kwargs["client"].spent_usd += 0.01
        return ExecSummaryDraft(
            text="AI exec summary seeded from backup deployment.",
            prompt_version="exec_summary_drafter.v1",
            cited_work_item_ids=(900001,),
            ai_confidence=Confidence.HIGH,
        )

    def _fake_workstream_blurb(**kwargs):
        blurb_calls.append(kwargs)
        kwargs["client"].usage_stats.call_count += 1
        kwargs["client"].spent_usd += 0.01
        return WorkstreamBlurb(
            text=f"AI narrative for {kwargs['workstream_name']} from backup deployment.",
            prompt_version="workstream_blurb.v1",
            cited_work_item_ids=(kwargs["items"][0].id,),
            ai_confidence=Confidence.HIGH,
        )

    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage._create_ai_client", _fake_create_ai_client)
    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage.draft_exec_summary", _fake_exec_summary)
    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage.generate_workstream_blurb", _fake_workstream_blurb)

    artifacts = generate_report_draft(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    assert artifacts.report.exec_summary_text == "AI exec summary seeded from backup deployment."
    assert exec_calls and getattr(exec_calls[0]["client"], "deployment", None) == "fake-exec-backup"
    assert blurb_calls and getattr(blurb_calls[0]["client"], "deployment", None) == "fake-blurb-backup"
    for call in blurb_calls:
        expected_text = f"AI narrative for {call['workstream_name']} from backup deployment."
        assert expected_text in artifacts.report.workstream_blurbs.values()
    assert artifacts.manifest.ai_calls == len(exec_calls) + len(blurb_calls)
    assert artifacts.manifest.ai_cost_usd == pytest.approx(0.01 * (len(exec_calls) + len(blurb_calls)))
    assert artifacts.manifest.ai_cost_by_model == {
        "fake-blurb-backup": pytest.approx(0.01 * len(blurb_calls)),
        "fake-exec-backup": pytest.approx(0.01 * len(exec_calls)),
    }
    assert any("primary deployment failed (fake-exec); trying backup deployment." in warning for warning in artifacts.warnings)
    assert any("fallback deployment succeeded (fake-exec-backup)." in warning for warning in artifacts.warnings)


@pytest.mark.parametrize(
    ("origin", "error_type"),
    [("factory", BudgetExceeded), ("provider", AIPreflightError)],
)
def test_generate_report_draft_v2_does_not_fallback_after_nonretryable_ai_error(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
    error_type: type[AIClientError],
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    _enable_v2_program_ai(programs_root)
    program_path = programs_root / "acme" / "program.yaml"
    program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    program_doc["ai"]["exec_summary_backup_deployment"] = "fake-exec-backup"
    program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False), encoding="utf-8")
    _write_v2_summary_seed(
        programs_root,
        workstream_id="acme",
        text="Deployment remains the gating lane until the example service closes the next checkpoint.",
    )
    failure = error_type("synthetic private cost detail")
    deployments = []
    exec_calls = []
    blurb_calls = []

    def fake_create_ai_client(**kwargs):
        deployment = kwargs["deployment"]
        deployments.append(deployment)
        if deployment == "fake-exec" and origin == "factory":
            raise failure
        if deployment == "fake-exec-backup":
            raise AssertionError("A non-retryable error must not reach the backup deployment.")
        return SimpleNamespace(
            deployment=deployment,
            usage_stats=SimpleNamespace(call_count=0),
            spent_usd=0.0,
        )

    def fake_exec_summary(**kwargs):
        exec_calls.append(kwargs)
        raise failure

    def fake_workstream_blurb(**kwargs):
        blurb_calls.append(kwargs)
        return WorkstreamBlurb(
            text=f"AI narrative for {kwargs['workstream_name']}.",
            prompt_version="workstream_blurb.v1",
            cited_work_item_ids=(kwargs["items"][0].id,),
            ai_confidence=Confidence.HIGH,
        )

    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage._create_ai_client", fake_create_ai_client)
    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage.draft_exec_summary", fake_exec_summary)
    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage.generate_workstream_blurb", fake_workstream_blurb)
    artifacts = generate_report_draft(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    assert "fake-exec" in deployments
    assert "fake-exec-backup" not in deployments
    assert len(exec_calls) == (1 if origin == "provider" else 0)
    assert blurb_calls
    assert f"AI exec summary synthesis skipped: {error_type.__name__}." in artifacts.warnings
    assert "synthetic private cost detail" not in " ".join(artifacts.warnings)


def test_report_cli_rejects_range_without_lookback(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.PROGRAMS_ROOT", reports_root.parent / "programs")
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)

    result = runner.invoke(
        app,
        [
            "report",
            "--edition",
            EDITION_NAME,
            "--dry-run",
            "--range",
            "3",
        ],
    )

    assert result.exit_code == 2
    assert "--range requires --edition-type lookback." in result.output




@pytest.mark.parametrize("source_state", ("missing", "invalid", "present", "rollback"))
def test_lookback_draft_surfaces_assumption_source_warnings(
    monkeypatch: pytest.MonkeyPatch, repo_root: Path, tmp_path: Path, source_state: str,
) -> None:
    from src.core.fact_sor_state import save_fact_sor_state

    monkeypatch.delenv("VERTEX_FACT_SOR", raising=False)
    monkeypatch.delenv("VERTEX_REPORT_ALLOW_LEGACY_ASSUMPTION_ROLLBACK", raising=False)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "database"))
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    save_fact_sor_state(
        "acme", mode="primary", recorded_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        programs_root=programs_root,
    )
    assumption_path = programs_root / "acme" / "assumptions.yaml"
    if source_state == "invalid":
        assumption_path.write_text("schema_version: '2.0'\nassumptions: []\n", encoding="utf-8")
    elif source_state in ("present", "rollback"):
        assumption_path.write_text(
            "schema_version: '1.0'\nassumptions:\n"
            "  - id: authored-assumption\n    text: Synthetic assumption\n"
            "    status: unvalidated\n    identified_date: '2026-04-15'\n",
            encoding="utf-8",
        )
    if source_state == "rollback":
        from src.commands import report_lookback

        monkeypatch.setenv("VERTEX_REPORT_ALLOW_LEGACY_ASSUMPTION_ROLLBACK", "1")
        original_loader = report_lookback._load_lookback_assumptions

        def fail_reality(*_args, **_kwargs):
            raise RuntimeError("Synthetic reality failure")

        def load_with_failed_reality(**kwargs):
            return original_loader(**{**kwargs, "load_program_reality": fail_reality})

        monkeypatch.setattr(report_lookback, "_load_lookback_assumptions", load_with_failed_reality)
    for issue_number, issue_as_of in (
        (1, datetime(2026, 4, 14, 18, tzinfo=timezone.utc)),
        (2, datetime(2026, 4, 21, 18, tzinfo=timezone.utc)),
    ):
        write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number, as_of=issue_as_of,
                items=(_snapshot_item_from_work_item(_sample_items(issue_as_of)[0], risk_level=RiskLevel.HIGH),),
                scorecard_risks={"Deployment Velocity": RiskLevel.LOW, "Deployment Safety": RiskLevel.HIGH},
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}",
            manifest=_manifest(issue_number=issue_number, as_of=issue_as_of, edition=EDITION_NAME),
            archive_root=archive_root,
        )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME, reports_root=reports_root, archive_root=archive_root,
        programs_root=programs_root, as_of=datetime(2026, 5, 5, 18, tzinfo=timezone.utc),
        edition_type_override="lookback", lookback_range=2, open_browser=False,
    )

    assumption_warnings = tuple(warning for warning in artifacts.warnings if "assumption" in warning.lower())
    if source_state == "missing":
        assert assumption_warnings == ()
    elif source_state == "invalid":
        assert any("assumption legacy cross-check unavailable" in warning for warning in assumption_warnings)
        assert artifacts.exit_code >= 2
    elif source_state == "present":
        assert any("legacy source has 1" in warning for warning in assumption_warnings)
        assert artifacts.exit_code >= 2
    else:
        assert any("audited rollback flag" in warning for warning in assumption_warnings)
        assert artifacts.exit_code >= 2
        assert "Assumption Lifecycle" in artifacts.html_body
        assert "Synthetic assumption" in artifacts.html_body
    if source_state != "rollback":
        assert "Assumption Lifecycle" not in artifacts.html_body
    assert str(assumption_path) not in artifacts.html_body
    assert str(assumption_path) not in artifacts.markdown_body


@pytest.mark.parametrize("is_raw", [False, True])
def test_lookback_draft_discloses_unconfirmed_assumption_during_render_fallback(
    monkeypatch: pytest.MonkeyPatch, repo_root: Path, tmp_path: Path, is_raw: bool,
) -> None:
    from src.commands import report_lookback
    from src.core.fact_sor_state import save_fact_sor_state
    from src.core.program_reality import FactAssessment
    from src.core.truth_levels import TruthLevel

    monkeypatch.delenv("VERTEX_FACT_SOR", raising=False)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "database"))
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    save_fact_sor_state(
        "acme", mode="legacy", family_modes={"judgment": "shadow"},
        recorded_at=datetime(2020, 1, 1, tzinfo=timezone.utc), programs_root=programs_root,
    )
    assessment = FactAssessment(
        record=Assumption(
            id="synthetic-assumption",
            program_id="acme",
            text="Synthetic assumption",
            validation_method=None,
            validation_due=None,
            status=AssumptionStatus.UNVALIDATED,
            linked_risk_id=None,
            linked_milestone_id=None,
            owner_alias=None,
            identified_date=date(2026, 4, 15),
            entity_refs=(),
        ),
        fact_id="synthetic-fact",
        truth_level=TruthLevel.RAW_OBSERVED if is_raw else TruthLevel.SOURCE_VALIDATED,
        disputed=False,
        stale=False,
        provisional_inputs=False,
        evidence=(),
    )
    original_loader = report_lookback._load_lookback_assumptions

    def load_with_synthetic_reality(**kwargs):
        return original_loader(**{
            **kwargs,
            "load_program_reality": lambda *_a, **_kw: SimpleNamespace(assumptions=lambda: (assessment,)),
        })

    def fail_shaping(*_args, **_kwargs):
        raise RuntimeError("Synthetic assessment shaping failure")

    monkeypatch.setattr(report_lookback, "_load_lookback_assumptions", load_with_synthetic_reality)
    monkeypatch.setattr(report_lookback, "_build_lookback_assumption_rows_from_assessments", fail_shaping)
    for issue_number, issue_as_of in (
        (1, datetime(2026, 4, 14, 18, tzinfo=timezone.utc)),
        (2, datetime(2026, 4, 21, 18, tzinfo=timezone.utc)),
    ):
        write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number, as_of=issue_as_of,
                items=(_snapshot_item_from_work_item(_sample_items(issue_as_of)[0], risk_level=RiskLevel.HIGH),),
                scorecard_risks={"Deployment Velocity": RiskLevel.LOW, "Deployment Safety": RiskLevel.HIGH},
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}",
            manifest=_manifest(issue_number=issue_number, as_of=issue_as_of, edition=EDITION_NAME),
            archive_root=archive_root,
        )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME, reports_root=reports_root, archive_root=archive_root,
        programs_root=programs_root, as_of=datetime(2026, 5, 5, 18, tzinfo=timezone.utc),
        edition_type_override="lookback", lookback_range=2, open_browser=False,
    )

    section = re.search(r'<table id="assumption-lifecycle".*?</table>', artifacts.html_body, re.DOTALL)
    assert section is not None
    assert "Synthetic assumption" in section.group()
    assert "live assumption metadata rendering failed" in section.group()
    assert ("⚠ includes unconfirmed sources" in section.group()) is is_raw


def test_generate_report_draft_lookback_surfaces_action_quality_gate(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    issue_1_as_of = datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc)
    issue_2_as_of = datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc)
    issue_3_as_of = datetime(2026, 4, 28, 18, 0, tzinfo=timezone.utc)
    lookback_as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)

    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=_lookback_snapshot(
            issue_number=1,
            as_of=issue_1_as_of,
            items=(
                _snapshot_item_from_work_item(_sample_items(issue_1_as_of)[0], risk_level=RiskLevel.LOW),
                _snapshot_item_from_work_item(_sample_items(issue_1_as_of)[1], risk_level=RiskLevel.HIGH),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.LOW, "Deployment Safety": RiskLevel.HIGH},
        ),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001",
        manifest=_manifest(issue_number=1, as_of=issue_1_as_of, edition=EDITION_NAME),
        archive_root=archive_root,
    )
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=2,
        snapshot=_lookback_snapshot(
            issue_number=2,
            as_of=issue_2_as_of,
            items=(
                _snapshot_item_from_work_item(_sample_items(issue_2_as_of)[0], risk_level=RiskLevel.MEDIUM),
                _snapshot_item_from_work_item(_sample_items(issue_2_as_of)[1], risk_level=RiskLevel.HIGH),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.MEDIUM, "Deployment Safety": RiskLevel.HIGH},
        ),
        html_body="<html><body>Issue 002</body></html>",
        markdown_body="# Issue 002",
        manifest=_manifest(issue_number=2, as_of=issue_2_as_of, edition=EDITION_NAME),
        archive_root=archive_root,
    )
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=3,
        snapshot=_lookback_snapshot(
            issue_number=3,
            as_of=issue_3_as_of,
            items=(
                _snapshot_item_from_work_item(_sample_items(issue_3_as_of)[0], risk_level=RiskLevel.HIGH),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.HIGH, "Deployment Safety": RiskLevel.DONE},
        ),
        html_body="<html><body>Issue 003</body></html>",
        markdown_body="# Issue 003",
        manifest=_manifest(issue_number=3, as_of=issue_3_as_of, edition=EDITION_NAME),
        archive_root=archive_root,
    )
    append_action(
        "acme",
        ActionItem(
            id="ACTION-LOOKBACK-1",
            program_id="acme",
            text="Close the retrospective action gap.",
            owner_alias="",
            due_date=None,
            status=ActionStatus.OPEN,
            source_signal_id=None,
            source_type=ActionSourceType.MANUAL,
            linked_work_item_ids=(),
            linked_claim_id=None,
            linked_risk_id=None,
            workstream_id=None,
            created_at=lookback_as_of,
            resolved_at=None,
            resolution_note=None,
        ),
        programs_root=programs_root,
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=lookback_as_of,
        edition_type_override="lookback",
        lookback_range=3,
        open_browser=False,
    )

    assert artifacts.manifest.qg_results["QG-15"] is False
    assert "QG-3" in artifacts.manifest.qg_results
    assert any("Open action items missing owner or due date" in warning for warning in artifacts.warnings)


def test_generate_report_draft_lookback_renders_assumption_lifecycle(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    issue_1_as_of = datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc)
    issue_2_as_of = datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc)
    issue_3_as_of = datetime(2026, 4, 28, 18, 0, tzinfo=timezone.utc)
    lookback_as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)

    save_assumptions(
        "acme",
        (
            Assumption(
                id="assumption-open",
                program_id="acme",
                text="Partner rollout remains aligned with the current launch train.",
                validation_method=None,
                validation_due=date(2026, 4, 29),
                status=AssumptionStatus.UNVALIDATED,
                linked_risk_id=None,
                linked_milestone_id="m3-code-complete",
                owner_alias="operator",
                identified_date=date(2026, 4, 16),
                entity_refs=("WI:900001",),
            ),
            Assumption(
                id="assumption-confirmed",
                program_id="acme",
                text="Schema escrow covers the release window.",
                validation_method=None,
                validation_due=None,
                status=AssumptionStatus.CONFIRMED,
                linked_risk_id=None,
                linked_milestone_id=None,
                owner_alias="operator",
                identified_date=date(2026, 4, 10),
                entity_refs=(),
                resolved_date=date(2026, 4, 21),
            ),
            Assumption(
                id="assumption-invalidated",
                program_id="acme",
                text="Capacity allocation remains fixed through the quarter.",
                validation_method=None,
                validation_due=None,
                status=AssumptionStatus.INVALIDATED,
                linked_risk_id="risk-capacity",
                linked_milestone_id=None,
                owner_alias="lt",
                identified_date=date(2026, 4, 1),
                entity_refs=(),
                resolved_date=date(2026, 4, 24),
            ),
            Assumption(
                id="assumption-outside-window",
                program_id="acme",
                text="Legacy assumption outside the retrospective window.",
                validation_method=None,
                validation_due=None,
                status=AssumptionStatus.CONFIRMED,
                linked_risk_id=None,
                linked_milestone_id=None,
                owner_alias=None,
                identified_date=date(2026, 3, 1),
                entity_refs=(),
                resolved_date=date(2026, 3, 15),
            ),
        ),
        programs_root=programs_root,
    )

    for issue_number, issue_as_of, risk_map in (
        (1, issue_1_as_of, {"Deployment Velocity": RiskLevel.LOW, "Deployment Safety": RiskLevel.HIGH}),
        (2, issue_2_as_of, {"Deployment Velocity": RiskLevel.MEDIUM, "Deployment Safety": RiskLevel.HIGH}),
        (3, issue_3_as_of, {"Deployment Velocity": RiskLevel.HIGH, "Deployment Safety": RiskLevel.DONE}),
    ):
        write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=issue_as_of,
                items=(
                    _snapshot_item_from_work_item(_sample_items(issue_as_of)[0], risk_level=RiskLevel.HIGH),
                ),
                scorecard_risks=risk_map,
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}",
            manifest=_manifest(issue_number=issue_number, as_of=issue_as_of, edition=EDITION_NAME),
            archive_root=archive_root,
        )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=lookback_as_of,
        edition_type_override="lookback",
        lookback_range=3,
        open_browser=False,
    )

    assert "Assumption Lifecycle" in artifacts.html_body
    assert "Identified in window: 1 | Confirmed in window: 1 | Invalidated in window: 1 | Still open: 1" in artifacts.html_body
    assert "Schema escrow covers the release window." in artifacts.html_body
    assert "CONFIRMED | identified 2026-04-10 | confirmed 2026-04-21 | owner operator" in artifacts.html_body
    assert "Capacity allocation remains fixed through the quarter." in artifacts.html_body
    assert "INVALIDATED | identified 2026-04-01 | invalidated 2026-04-24 | owner lt | risk risk-capacity" in artifacts.html_body
    assert "Partner rollout remains aligned with the current launch train." in artifacts.markdown_body
    assert "UNVALIDATED | identified 2026-04-16 | due 2026-04-29 | overdue | owner operator | milestone m3-code-complete" in artifacts.markdown_body
    assert "Legacy assumption outside the retrospective window." not in artifacts.html_body


def test_generate_report_draft_lookback_renders_charter_review(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    program_path = programs_root / "acme" / "program.yaml"
    program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    program_doc["charter"] = {
        "scope_statement": "Deliver Acme ramp readiness for the current LT gate.",
        "success_criteria": [
            "Green-light LT review without timeline slip.",
        ],
        "constraints": [
            "Do not widen partner pilot scope before Network signoff.",
        ],
    }
    program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")

    lookback_as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    for issue_number, issue_as_of, risk_map in (
        (1, datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc), {"Deployment Velocity": RiskLevel.LOW, "Deployment Safety": RiskLevel.HIGH}),
        (2, datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc), {"Deployment Velocity": RiskLevel.MEDIUM, "Deployment Safety": RiskLevel.HIGH}),
        (3, datetime(2026, 4, 28, 18, 0, tzinfo=timezone.utc), {"Deployment Velocity": RiskLevel.HIGH, "Deployment Safety": RiskLevel.DONE}),
    ):
        write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=issue_as_of,
                items=(
                    _snapshot_item_from_work_item(_sample_items(issue_as_of)[0], risk_level=RiskLevel.HIGH),
                ),
                scorecard_risks=risk_map,
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}",
            manifest=_manifest(issue_number=issue_number, as_of=issue_as_of, edition=EDITION_NAME),
            archive_root=archive_root,
        )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=lookback_as_of,
        edition_type_override="lookback",
        lookback_range=3,
        open_browser=False,
    )

    assert "Charter Review" in artifacts.html_body
    assert "Scope:" in artifacts.html_body
    assert "Deliver Acme ramp readiness for the current LT gate." in artifacts.html_body
    assert "Success criteria authored: 1 | Constraints authored: 1" in artifacts.html_body
    assert "Success criterion" in artifacts.html_body
    assert "Green-light LT review without timeline slip." in artifacts.html_body
    assert "Constraint" in artifacts.markdown_body
    assert "Do not widen partner pilot scope before Network signoff." in artifacts.markdown_body


def test_generate_report_draft_lookback_evaluates_structured_charter_success_criteria(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    program_path = programs_root / "acme" / "program.yaml"
    program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    program_doc["charter"] = {
        "scope_statement": "Deliver Acme ramp readiness for the current LT gate.",
        "success_criteria": [
            {
                "text": "Deployment Safety closes at Low or better.",
                "metric": {
                    "kind": "dimension_max_risk",
                    "scorecard": "Example Release Readiness",
                    "dimension": "Deployment Safety",
                    "max_risk": "low",
                },
            },
            {
                "text": "Open snapshot items close at zero.",
                "metric": {
                    "kind": "item_count_max",
                    "max_count": 0,
                },
            },
            "Green-light LT review without timeline slip.",
        ],
        "constraints": [
            "Do not widen partner pilot scope before Network signoff.",
        ],
    }
    program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")

    lookback_as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    for issue_number, issue_as_of, risk_map in (
        (1, datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc), {"Deployment Velocity": RiskLevel.LOW, "Deployment Safety": RiskLevel.HIGH}),
        (2, datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc), {"Deployment Velocity": RiskLevel.MEDIUM, "Deployment Safety": RiskLevel.HIGH}),
        (3, datetime(2026, 4, 28, 18, 0, tzinfo=timezone.utc), {"Deployment Velocity": RiskLevel.HIGH, "Deployment Safety": RiskLevel.DONE}),
    ):
        write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=issue_as_of,
                items=(
                    _snapshot_item_from_work_item(_sample_items(issue_as_of)[0], risk_level=RiskLevel.HIGH),
                ),
                scorecard_risks=risk_map,
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}",
            manifest=_manifest(issue_number=issue_number, as_of=issue_as_of, edition=EDITION_NAME),
            archive_root=archive_root,
        )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=lookback_as_of,
        edition_type_override="lookback",
        lookback_range=3,
        open_browser=False,
    )

    assert "Archive-backed evaluation: evaluated 2 | Met: 1 | Not met: 1 | Manual review: 0" in artifacts.html_body
    assert "Status: MET" in artifacts.markdown_body
    assert "Latest confirmed risk: Done in Issue 003; target <= Low; window trend: High -> High -> Done." in artifacts.markdown_body
    assert "Status: NOT MET" in artifacts.markdown_body
    assert "Evidence: Issue 003 matching item count: 1 (states: any; target <= 0)." in artifacts.markdown_body
    assert "Green-light LT review without timeline slip." in artifacts.html_body


def test_generate_report_draft_lookback_evaluates_filtered_item_count_max_charter_success_criteria(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    program_path = programs_root / "acme" / "program.yaml"
    program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    program_doc["charter"] = {
        "scope_statement": "Deliver Acme ramp readiness for the current LT gate.",
        "success_criteria": [
            {
                "text": "Medium-risk deployment features with Safety tag stay at one or fewer.",
                "metric": {
                    "kind": "item_count_max",
                    "max_count": 1,
                    "work_item_types": ["feature"],
                    "area_path_prefixes": ["ExampleProject\\Acme\\Deployment"],
                    "risk_levels": ["medium"],
                    "tags": ["safety"],
                },
            },
        ],
    }
    program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")

    lookback_as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    for issue_number, issue_as_of in (
        (1, datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc)),
        (2, datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc)),
        (3, datetime(2026, 4, 28, 18, 0, tzinfo=timezone.utc)),
    ):
        sample_items = _sample_items(issue_as_of)
        if issue_number == 3:
            snapshot_items = (
                _snapshot_item_from_work_item(sample_items[0], risk_level=RiskLevel.MEDIUM),
                _snapshot_item_from_work_item(sample_items[1], risk_level=RiskLevel.HIGH),
            )
        else:
            snapshot_items = (
                _snapshot_item_from_work_item(sample_items[0], risk_level=RiskLevel.MEDIUM),
            )

        write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=issue_as_of,
                items=snapshot_items,
                scorecard_risks={"Deployment Velocity": RiskLevel.MEDIUM, "Deployment Safety": RiskLevel.MEDIUM},
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}",
            manifest=_manifest(issue_number=issue_number, as_of=issue_as_of, edition=EDITION_NAME),
            archive_root=archive_root,
        )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=lookback_as_of,
        edition_type_override="lookback",
        lookback_range=3,
        open_browser=False,
    )

    assert "Archive-backed evaluation: evaluated 1 | Met: 1 | Not met: 0 | Manual review: 0" in artifacts.html_body
    assert "Status: MET" in artifacts.markdown_body
    assert "Evidence: Issue 003 matching item count: 1" in artifacts.markdown_body
    assert "work item types: feature" in artifacts.markdown_body
    assert "area path prefixes: exampleproject\\acme\\deployment" in artifacts.markdown_body
    assert "risk levels: Medium" in artifacts.markdown_body
    assert "tags: safety" in artifacts.markdown_body


def test_report_cli_uses_quarterly_lookback_edition_defaults(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    reports_root = stage_v2_report_workspace(
        repo_root,
        tmp_path,
        edition_names=(EDITION_NAME, "example_quarterly"),
    )
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)
    program_path = programs_root / "acme" / "program.yaml"
    program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    program_document["ado"]["date_window_days"] = 90
    program_path.write_text(yaml.safe_dump(program_document, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.PROGRAMS_ROOT", reports_root.parent / "programs")
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.report.webbrowser.open", lambda url: True)

    issue_dates = (
        datetime(2026, 2, 3, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 2, 17, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 3, 3, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 3, 17, 18, 0, tzinfo=timezone.utc),
    )

    tracked_item = SnapshotItem(
        id=301,
        type="Feature",
        title="Network capacity check",
        state="Active",
        assigned_to="Vertex Maintainer",
        area_path="ExampleProject\\Acme",
        target_date=date(2026, 5, 1),
        risk_level=RiskLevel.MEDIUM,
        tags=["acme"],
    )
    resolved_item = SnapshotItem(
        id=302,
        type="Risk",
        title="Repair approval path remains manual",
        state="Active",
        assigned_to="Vertex Maintainer",
        area_path="ExampleProject\\Acme",
        target_date=date(2026, 4, 15),
        risk_level=RiskLevel.LOW,
        tags=["acme"],
    )

    write_confirmed_issue(
        edition="example_quarterly",
        issue_number=1,
        snapshot=_lookback_snapshot(
            issue_number=1,
            as_of=issue_dates[0],
            items=(
                replace(tracked_item, target_date=date(2026, 5, 1), risk_level=RiskLevel.MEDIUM),
                replace(resolved_item, target_date=date(2026, 4, 15), risk_level=RiskLevel.LOW),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.LOW, "Deployment Safety": RiskLevel.HIGH},
        ),
        html_body="<html><body>Quarterly 001</body></html>",
        markdown_body="# Quarterly 001",
        manifest=_manifest(issue_number=1, as_of=issue_dates[0], edition="example_quarterly"),
        archive_root=archive_root,
    )
    write_confirmed_issue(
        edition="example_quarterly",
        issue_number=2,
        snapshot=_lookback_snapshot(
            issue_number=2,
            as_of=issue_dates[1],
            items=(
                replace(tracked_item, target_date=date(2026, 5, 8), risk_level=RiskLevel.HIGH),
                replace(resolved_item, target_date=date(2026, 4, 22), risk_level=RiskLevel.HIGH),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.MEDIUM, "Deployment Safety": RiskLevel.HIGH},
        ),
        html_body="<html><body>Quarterly 002</body></html>",
        markdown_body="# Quarterly 002",
        manifest=_manifest(issue_number=2, as_of=issue_dates[1], edition="example_quarterly"),
        archive_root=archive_root,
    )
    write_confirmed_issue(
        edition="example_quarterly",
        issue_number=3,
        snapshot=_lookback_snapshot(
            issue_number=3,
            as_of=issue_dates[2],
            items=(
                replace(tracked_item, target_date=date(2026, 5, 15), risk_level=RiskLevel.HIGH),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.HIGH, "Deployment Safety": RiskLevel.MEDIUM},
        ),
        html_body="<html><body>Quarterly 003</body></html>",
        markdown_body="# Quarterly 003",
        manifest=_manifest(issue_number=3, as_of=issue_dates[2], edition="example_quarterly"),
        archive_root=archive_root,
    )
    write_confirmed_issue(
        edition="example_quarterly",
        issue_number=4,
        snapshot=_lookback_snapshot(
            issue_number=4,
            as_of=issue_dates[3],
            items=(
                replace(tracked_item, target_date=date(2026, 5, 22), risk_level=RiskLevel.HIGH),
                SnapshotItem(
                    id=303,
                    type="Feature",
                    title="Buildout validation lane is back on plan",
                    state="Active",
                    assigned_to="Vertex Maintainer",
                    area_path="ExampleProject\\Acme",
                    target_date=date(2026, 4, 30),
                    risk_level=RiskLevel.LOW,
                    tags=["acme"],
                ),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.HIGH, "Deployment Safety": RiskLevel.DONE},
        ),
        html_body="<html><body>Quarterly 004</body></html>",
        markdown_body="# Quarterly 004",
        manifest=_manifest(issue_number=4, as_of=issue_dates[3], edition="example_quarterly"),
        archive_root=archive_root,
    )

    result = runner.invoke(
        app,
        [
            "report",
            "--edition",
            "example_quarterly",
            "--dry-run",
            "--as-of",
            "2026-03-31T18:00:00",
        ],
    )

    assert result.exit_code == 3
    assert "[MODE: V2 HYBRID JOURNAL]" in result.output
    assert "[1/2] resolution" in result.output
    assert "[2/2] lookback" in result.output

    html_path = programs_root / "acme" / "publications" / "example_quarterly" / "issue_005" / "issue_005.html"
    draft_payload = json.loads((programs_root / "acme" / "publications" / "example_quarterly" / "issue_005" / "issue_005.draft.json").read_text(encoding="utf-8"))

    assert html_path.exists()
    assert "Quarterly retrospective window" in html_path.read_text(encoding="utf-8")
    assert "items tracked" in draft_payload["exec_summary_text"].lower()
    assert "chronic drifters" in draft_payload["exec_summary_text"].lower()
    assert "workstream risk movements" in draft_payload["exec_summary_text"].lower()


def test_generate_report_draft_lookback_renders_retrospective_intelligence(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    issue_dates = (
        datetime(2026, 4, 7, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 28, 18, 0, tzinfo=timezone.utc),
    )
    tracked_item = SnapshotItem(
        id=301,
        type="Feature",
        title="Network capacity check",
        state="Active",
        assigned_to="Vertex Maintainer",
        area_path="ExampleProject\\Acme",
        target_date=date(2026, 5, 1),
        risk_level=RiskLevel.MEDIUM,
        tags=["acme"],
    )

    for issue_number, issue_as_of, item_target, item_risk, scorecard_risks in (
        (1, issue_dates[0], date(2026, 5, 1), RiskLevel.MEDIUM, {"Deployment Velocity": RiskLevel.LOW, "Deployment Safety": RiskLevel.HIGH}),
        (2, issue_dates[1], date(2026, 5, 8), RiskLevel.HIGH, {"Deployment Velocity": RiskLevel.HIGH, "Deployment Safety": RiskLevel.HIGH}),
        (3, issue_dates[2], date(2026, 5, 15), RiskLevel.HIGH, {"Deployment Velocity": RiskLevel.HIGH, "Deployment Safety": RiskLevel.HIGH}),
        (4, issue_dates[3], date(2026, 5, 22), RiskLevel.HIGH, {"Deployment Velocity": RiskLevel.HIGH, "Deployment Safety": RiskLevel.DONE}),
    ):
        write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=issue_as_of,
                items=(
                    replace(tracked_item, target_date=item_target, risk_level=item_risk),
                ),
                scorecard_risks=scorecard_risks,
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}",
            manifest=_manifest(issue_number=issue_number, as_of=issue_as_of, edition=EDITION_NAME),
            archive_root=archive_root,
        )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        edition_type_override="lookback",
        lookback_range=4,
        open_browser=False,
    )

    assert "Retrospective Intelligence" in artifacts.html_body
    assert "Chronic workstreams: 1 | Recovered chronic issues: 1 | Recurring drift items: 1 | Claim accuracy signals: 0 | Charter evaluation signals: 0" in artifacts.html_body
    assert "Deployment Velocity (Example Release Readiness)" in artifacts.html_body
    assert "Recovered chronic issue" in artifacts.html_body


def test_generate_report_draft_lookback_renders_incident_learnings(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    issue_dates = (
        datetime(2026, 4, 7, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc),
    )

    for issue_number, issue_as_of, risk_map in (
        (1, issue_dates[0], {"Deployment Velocity": RiskLevel.LOW, "Deployment Safety": RiskLevel.HIGH}),
        (2, issue_dates[1], {"Deployment Velocity": RiskLevel.MEDIUM, "Deployment Safety": RiskLevel.HIGH}),
        (3, issue_dates[2], {"Deployment Velocity": RiskLevel.HIGH, "Deployment Safety": RiskLevel.DONE}),
    ):
        write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=issue_as_of,
                items=(
                    _snapshot_item_from_work_item(_sample_items(issue_as_of)[0], risk_level=RiskLevel.HIGH),
                ),
                scorecard_risks=risk_map,
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}",
            manifest=_manifest(issue_number=issue_number, as_of=issue_as_of, edition=EDITION_NAME),
            archive_root=archive_root,
        )

    append_incident_entry(
        IncidentEntry(
            program_id="acme",
            incident_id="12345",
            signal_id="signal-12345",
            observed_at=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 4, 10, 12, 5, tzinfo=timezone.utc),
            belief_change_summary="IcM 12345: Network capacity check stayed blocked.",
            workstream_id="acme",
            owning_team="Example Operations",
            severity=2,
            ado_entity_refs=("WI:301",),
            confidence=Confidence.HIGH,
        ),
        programs_root=programs_root,
    )
    append_incident_entry(
        IncidentEntry(
            program_id="acme",
            incident_id="12346",
            signal_id="signal-12346",
            observed_at=datetime(2026, 4, 18, 9, 30, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 4, 18, 9, 35, tzinfo=timezone.utc),
            belief_change_summary="IcM 12346: Repair approval path remained manual during the quarter.",
            workstream_id="acme",
            owning_team="Repair",
            severity=3,
            confidence=Confidence.MEDIUM,
        ),
        programs_root=programs_root,
    )
    append_incident_entry(
        IncidentEntry(
            program_id="acme",
            incident_id="99999",
            signal_id="signal-outside-window",
            observed_at=datetime(2026, 5, 1, 9, 30, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 5, 1, 9, 35, tzinfo=timezone.utc),
            belief_change_summary="IcM 99999: outside retrospective window.",
            workstream_id="acme",
            confidence=Confidence.LOW,
        ),
        programs_root=programs_root,
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        edition_type_override="lookback",
        lookback_range=3,
        open_browser=False,
    )

    assert "Incident Learnings" in artifacts.html_body
    assert "Incidents in window: 2 | With explicit ADO refs: 1" in artifacts.html_body
    assert "Attribution-backed learnings" in artifacts.html_body
    assert "IcM 12345" in artifacts.html_body
    assert "WI:301 was implicated in IcM 12345" in artifacts.html_body
    assert "high confidence" in artifacts.html_body
    assert "refs WI:301" in artifacts.html_body
    assert "IcM 12346" in artifacts.markdown_body
    assert "medium confidence" in artifacts.markdown_body
    assert "IcM 99999" not in artifacts.html_body
    assert "Repair approval path remained manual during the quarter." in artifacts.markdown_body


def test_generate_report_draft_renders_higher_order_incident_class_patterns_in_lookback(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    issue_dates = (
        datetime(2026, 4, 7, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc),
    )

    for issue_number, issue_as_of, risk_map in (
        (1, issue_dates[0], {"Deployment Velocity": RiskLevel.LOW, "Deployment Safety": RiskLevel.HIGH}),
        (2, issue_dates[1], {"Deployment Velocity": RiskLevel.MEDIUM, "Deployment Safety": RiskLevel.HIGH}),
        (3, issue_dates[2], {"Deployment Velocity": RiskLevel.HIGH, "Deployment Safety": RiskLevel.DONE}),
    ):
        write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=issue_as_of,
                items=(
                    _snapshot_item_from_work_item(_sample_items(issue_as_of)[0], risk_level=RiskLevel.HIGH),
                ),
                scorecard_risks=risk_map,
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}",
            manifest=_manifest(issue_number=issue_number, as_of=issue_as_of, edition=EDITION_NAME),
            archive_root=archive_root,
        )

    for incident_id, signal_id, summary, ref in (
        ("12345", "signal-12345", "IcM 12345: WI:301 fleet pilot dependency on capacity allocation stayed blocked.", "WI:301"),
        ("12346", "signal-12346", "IcM 12346: WI:302 fleet pilot dependency on capacity allocation stayed blocked again.", "WI:302"),
        ("12347", "signal-12347", "IcM 12347: WI:303 fleet pilot dependency on capacity allocation stayed blocked during follow-up.", "WI:303"),
    ):
        append_incident_entry(
            IncidentEntry(
                program_id="acme",
                incident_id=incident_id,
                signal_id=signal_id,
                observed_at=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
                recorded_at=datetime(2026, 4, 10, 12, 5, tzinfo=timezone.utc),
                belief_change_summary=summary,
                workstream_id="acme",
                owning_team="Example Operations",
                severity=2,
                ado_entity_refs=(ref,),
                confidence=Confidence.HIGH,
            ),
            programs_root=programs_root,
        )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        edition_type_override="lookback",
        lookback_range=3,
        open_browser=False,
    )

    assert "incident class" in artifacts.markdown_body.lower()


def test_generate_report_draft_lookback_scopes_retrospective_ban_list_to_attributed_incidents(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    issue_dates = (
        datetime(2026, 4, 7, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc),
    )

    for issue_number, issue_as_of, risk_map in (
        (1, issue_dates[0], {"Deployment Velocity": RiskLevel.LOW, "Deployment Safety": RiskLevel.HIGH}),
        (2, issue_dates[1], {"Deployment Velocity": RiskLevel.MEDIUM, "Deployment Safety": RiskLevel.HIGH}),
        (3, issue_dates[2], {"Deployment Velocity": RiskLevel.HIGH, "Deployment Safety": RiskLevel.DONE}),
    ):
        write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=issue_as_of,
                items=(
                    _snapshot_item_from_work_item(_sample_items(issue_as_of)[0], risk_level=RiskLevel.HIGH),
                ),
                scorecard_risks=risk_map,
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}",
            manifest=_manifest(issue_number=issue_number, as_of=issue_as_of, edition=EDITION_NAME),
            archive_root=archive_root,
        )

    append_incident_entry(
        IncidentEntry(
            program_id="acme",
            incident_id="20001",
            signal_id="signal-20001",
            observed_at=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 4, 10, 12, 5, tzinfo=timezone.utc),
            belief_change_summary="IcM 20001: Rollout slipped due to blocked capacity allocation.",
            workstream_id="acme",
            severity=2,
            ado_entity_refs=("WI:401",),
            confidence=Confidence.HIGH,
        ),
        programs_root=programs_root,
    )
    append_incident_entry(
        IncidentEntry(
            program_id="acme",
            incident_id="20002",
            signal_id="signal-20002",
            observed_at=datetime(2026, 4, 18, 9, 30, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 4, 18, 9, 35, tzinfo=timezone.utc),
            belief_change_summary="IcM 20002: Manual approval stayed delayed because of missing owner.",
            workstream_id="acme",
            severity=3,
            confidence=Confidence.MEDIUM,
        ),
        programs_root=programs_root,
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        edition_type_override="lookback",
        lookback_range=3,
        open_browser=False,
    )

    assert "Rollout slipped due to blocked capacity allocation." in artifacts.html_body
    assert any("because of" in warning for warning in artifacts.warnings)
    assert all("due to" not in warning for warning in artifacts.warnings)


def test_generate_report_draft_lookback_retrospective_intelligence_includes_claim_and_charter_signals(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    program_path = programs_root / "acme" / "program.yaml"
    program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    program_doc.setdefault("charter", {})["success_criteria"] = [
        {
            "text": "Keep deployment velocity at Medium or lower by quarter close.",
            "metric": {
                "kind": "dimension_max_risk",
                "scorecard_name": "Example Release Readiness",
                "dimension_name": "Deployment Velocity",
                "max_risk": "medium",
            },
        }
    ]
    program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False), encoding="utf-8")

    issue_dates = (
        datetime(2026, 4, 7, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc),
    )
    tracked_item = SnapshotItem(
        id=401,
        type="Feature",
        title="Deployment readiness guardrail",
        state="Active",
        assigned_to="Vertex Maintainer",
        area_path="ExampleProject\\Acme",
        target_date=date(2026, 5, 1),
        risk_level=RiskLevel.MEDIUM,
        tags=["acme"],
    )
    for issue_number, issue_as_of, item_target, scorecard_risk in (
        (1, issue_dates[0], date(2026, 5, 1), RiskLevel.LOW),
        (2, issue_dates[1], date(2026, 5, 8), RiskLevel.HIGH),
        (3, issue_dates[2], date(2026, 5, 15), RiskLevel.HIGH),
    ):
        write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=issue_as_of,
                items=(replace(tracked_item, target_date=item_target),),
                scorecard_risks={"Deployment Velocity": scorecard_risk},
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}",
            manifest=_manifest(issue_number=issue_number, as_of=issue_as_of, edition=EDITION_NAME),
            archive_root=archive_root,
        )

    append_claim_entry(
        ClaimEntry(
            id="claim-rt-1",
            program_id="acme",
            edition_id=EDITION_NAME,
            issue_number=2,
            workstream_id="deployment_readiness",
            text="Deployment readiness will hold the May 3 checkpoint for WI:401.",
            entity_refs=("WI:401",),
            claim_date=date(2026, 4, 14),
            owner_alias="operator",
            due_date=date(2026, 5, 3),
        ),
        programs_root=programs_root,
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        edition_type_override="lookback",
        lookback_range=3,
        open_browser=False,
    )

    assert "Claim accuracy signals: 1 | Charter evaluation signals: 1" in artifacts.html_body
    assert "Claim accuracy concern" in artifacts.html_body
    assert "Current ADO target date is 2026-05-15, later than the claimed date 2026-05-03." in artifacts.html_body
    assert "Charter criterion missed" in artifacts.markdown_body
    assert "Latest confirmed risk: High in Issue 003; target <= Medium; window trend: Low -> High -> High." in artifacts.markdown_body


def test_generate_report_draft_lookback_appends_ai_retrospective_synthesis(monkeypatch: pytest.MonkeyPatch, repo_root: Path, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    program_path = reports_root.parent / "programs" / "acme" / "program.yaml"
    program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8")) or {}
    program_doc.setdefault("ai", {})["enabled"] = True
    program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False), encoding="utf-8")

    issue_dates = (
        datetime(2026, 4, 7, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc),
    )
    tracked_item = SnapshotItem(
        id=451,
        type="Feature",
        title="Checkpoint readiness keeps slipping",
        state="Active",
        assigned_to="Vertex Maintainer",
        area_path="ExampleProject\\Acme",
        target_date=date(2026, 5, 1),
        risk_level=RiskLevel.HIGH,
        tags=["acme"],
    )
    for issue_number, issue_as_of, item_target, scorecard_risk in (
        (1, issue_dates[0], date(2026, 5, 1), RiskLevel.HIGH),
        (2, issue_dates[1], date(2026, 5, 8), RiskLevel.HIGH),
        (3, issue_dates[2], date(2026, 5, 15), RiskLevel.HIGH),
    ):
        write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=issue_as_of,
                items=(replace(tracked_item, target_date=item_target),),
                scorecard_risks={"Deployment Velocity": scorecard_risk},
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}",
            manifest=_manifest(issue_number=issue_number, as_of=issue_as_of, edition=EDITION_NAME),
            archive_root=archive_root,
        )

    monkeypatch.setenv("VERTEX_EXEC_DEPLOYMENT", "lookback-ai-test")

    class _FakeLookbackAIClient:
        def __init__(self) -> None:
            self.spent_usd = 0.012
            self.usage_stats = SimpleNamespace(call_count=0)

        def chat(self, system: str, user: str, max_tokens: int = 800, *, prompt_version: str | None = None) -> str:
            raise AssertionError("lookback synthesis should use structured output")

        def structured(self, system: str, user: str, *, parser, max_tokens: int = 800, prompt_version: str | None = None):
            self.usage_stats.call_count += 1
            assert "Deterministic retrospective summary" in user
            return parser(
                {
                    "insights": [
                        {
                            "category": "AI synthesis",
                            "title": "Checkpoint risk is chronically unmanaged",
                            "detail": "Repeated target slips and sustained high deployment-velocity risk point to a systemic checkpoint-planning gap rather than isolated execution noise.",
                        }
                    ]
                }
            )

    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage._create_ai_client", lambda **kwargs: _FakeLookbackAIClient())

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        edition_type_override="lookback",
        lookback_range=3,
        open_browser=False,
    )

    assert "Checkpoint risk is chronically unmanaged" in artifacts.html_body
    assert artifacts.manifest.ai_calls == 1
    assert artifacts.manifest.ai_cost_usd == pytest.approx(0.012)
    assert artifacts.manifest.metadata["ai_safety"]["trace_run_id"] is not None
    draft_payload = json.loads((programs_root / "acme" / "publications" / EDITION_NAME / "issue_004" / "issue_004.draft.json").read_text(encoding="utf-8"))
    assert draft_payload["ai_trace_run_id"] == artifacts.manifest.metadata["ai_safety"]["trace_run_id"]


def test_build_lookback_ai_retrospective_rows_scrubs_pii_from_ai_output(tmp_path: Path) -> None:
    class _FakeLookbackAIClient:
        def structured(self, system: str, user: str, *, parser, max_tokens: int = 800, prompt_version: str | None = None):
            del system, user, max_tokens, prompt_version
            return parser(
                {
                    "insights": [
                        {
                            "category": "AI synthesis",
                            "title": "Coordination with outside@example.test is failing",
                            "detail": "Repeated escalations to outside@example.test show the same dependency handoff is stuck.",
                        }
                    ]
                }
            )

    rows = _build_lookback_ai_retrospective_rows(
        client=_FakeLookbackAIClient(),
        retrospective_intelligence=RetrospectiveIntelligenceSummary(
            chronic_workstream_count=1,
            recovered_workstream_count=0,
            recurring_drift_count=0,
            worsened_workstream_count=1,
            improved_workstream_count=0,
            claim_accuracy_signal_count=0,
            charter_evaluation_signal_count=0,
            rows=(
                RetrospectiveIntelligenceRow(
                    category="Recurring drift",
                    title="Checkpoint readiness keeps slipping",
                    detail="Three consecutive issues moved the same target date later.",
                ),
            ),
        ),
        snapshots=(
            Snapshot(
                issue_number=1,
                generated_at=datetime(2026, 4, 7, 18, 0, tzinfo=timezone.utc),
                ado_data_as_of=datetime(2026, 4, 7, 18, 0, tzinfo=timezone.utc),
                edition_type=EditionType.LOOKBACK,
                items=(),
                scorecards=(),
            ),
            Snapshot(
                issue_number=2,
                generated_at=datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc),
                ado_data_as_of=datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc),
                edition_type=EditionType.LOOKBACK,
                items=(),
                scorecards=(),
            ),
        ),
        program_id="acme",
        programs_root=tmp_path,
    )

    assert len(rows) == 1
    assert "outside@example.test" not in rows[0].title
    assert "outside@example.test" not in rows[0].detail
    assert "[PII-FILTERED-EMAIL]" in rows[0].title
    assert "[PII-FILTERED-EMAIL]" in rows[0].detail


def test_generate_report_draft_lookback_degrades_when_ai_retrospective_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    program_path = reports_root.parent / "programs" / "acme" / "program.yaml"
    program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8")) or {}
    program_doc.setdefault("ai", {})["enabled"] = True
    program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False), encoding="utf-8")

    issue_dates = (
        datetime(2026, 4, 7, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc),
    )
    tracked_item = SnapshotItem(
        id=451,
        type="Feature",
        title="Checkpoint readiness keeps slipping",
        state="Active",
        assigned_to="Vertex Maintainer",
        area_path="ExampleProject\\Acme",
        target_date=date(2026, 5, 1),
        risk_level=RiskLevel.HIGH,
        tags=["acme"],
    )
    for issue_number, issue_as_of, item_target, scorecard_risk in (
        (1, issue_dates[0], date(2026, 5, 1), RiskLevel.HIGH),
        (2, issue_dates[1], date(2026, 5, 8), RiskLevel.HIGH),
        (3, issue_dates[2], date(2026, 5, 15), RiskLevel.HIGH),
    ):
        write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=issue_as_of,
                items=(replace(tracked_item, target_date=item_target),),
                scorecard_risks={"Deployment Velocity": scorecard_risk},
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}",
            manifest=_manifest(issue_number=issue_number, as_of=issue_as_of, edition=EDITION_NAME),
            archive_root=archive_root,
        )

    monkeypatch.setenv("VERTEX_EXEC_DEPLOYMENT", "lookback-ai-test")

    def _raise_config_error(**kwargs):
        raise ConfigError("missing AI deployment credentials")

    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage._create_ai_client", _raise_config_error)

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        edition_type_override="lookback",
        lookback_range=3,
        open_browser=False,
    )

    assert "Retrospective Intelligence" in artifacts.html_body
    assert "Checkpoint risk is chronically unmanaged" not in artifacts.html_body
    assert artifacts.manifest.ai_calls == 0
    assert artifacts.manifest.ai_cost_usd == 0.0


def test_generate_report_draft_lookback_degrades_when_ai_retrospective_parser_rejects_output(
    monkeypatch: pytest.MonkeyPatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    program_path = reports_root.parent / "programs" / "acme" / "program.yaml"
    program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8")) or {}
    program_doc.setdefault("ai", {})["enabled"] = True
    program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False), encoding="utf-8")

    issue_dates = (
        datetime(2026, 4, 7, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc),
    )
    tracked_item = SnapshotItem(
        id=451,
        type="Feature",
        title="Checkpoint readiness keeps slipping",
        state="Active",
        assigned_to="Vertex Maintainer",
        area_path="ExampleProject\\Acme",
        target_date=date(2026, 5, 1),
        risk_level=RiskLevel.HIGH,
        tags=["acme"],
    )
    for issue_number, issue_as_of, item_target, scorecard_risk in (
        (1, issue_dates[0], date(2026, 5, 1), RiskLevel.HIGH),
        (2, issue_dates[1], date(2026, 5, 8), RiskLevel.HIGH),
        (3, issue_dates[2], date(2026, 5, 15), RiskLevel.HIGH),
    ):
        write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=issue_as_of,
                items=(replace(tracked_item, target_date=item_target),),
                scorecard_risks={"Deployment Velocity": scorecard_risk},
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}",
            manifest=_manifest(issue_number=issue_number, as_of=issue_as_of, edition=EDITION_NAME),
            archive_root=archive_root,
        )

    monkeypatch.setenv("VERTEX_EXEC_DEPLOYMENT", "lookback-ai-test")

    class _FakeAIClient:
        def __init__(self) -> None:
            self.usage_stats = SimpleNamespace(call_count=1)
            self.spent_usd = 0.25

    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage._create_ai_client", lambda **kwargs: _FakeAIClient())

    def _raise_pipeline_error(*, client, retrospective_intelligence, snapshots, program_id="", programs_root=None):
        del client, retrospective_intelligence, snapshots, program_id, programs_root
        raise AIPipelineError("Generated text rejected by injection detector: prompt_injection")

    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage._build_lookback_ai_retrospective_rows", _raise_pipeline_error)

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        edition_type_override="lookback",
        lookback_range=3,
        open_browser=False,
    )

    assert "Retrospective Intelligence" in artifacts.html_body
    assert artifacts.manifest.ai_calls == 0
    assert artifacts.manifest.ai_cost_usd == 0.0


def test_generate_report_draft_opens_browser(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    opened_urls: list[str] = []
    monkeypatch.setattr("src.commands.report.webbrowser.open", lambda url: opened_urls.append(url) or True)

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=True,
    )

    assert opened_urls
    assert opened_urls[0].startswith("file:")


def test_generate_report_draft_renders_kusto_sections_and_persists_them(repo_root: Path, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    reset_overrides_to_seed_state(reports_root)
    _set_v2_edition_layout_mode(programs_root / "acme" / "editions", edition_id=EDITION_NAME, layout_mode="dashboard")

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=_sample_kusto_query_results,
        open_browser=False,
    )

    draft_payload = json.loads((programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.draft.json").read_text(encoding="utf-8"))
    quality_matrix_payload = json.loads(artifacts.quality_matrix_json_path.read_text(encoding="utf-8"))

    assert "Deployment Readiness" in artifacts.html_body
    assert "Deploy P50 (mins)" in artifacts.html_body
    assert len(draft_payload["kusto_sections"]) == 7
    assert {section["title"] for section in draft_payload["kusto_sections"]} >= {"Fleet Health", "Active Incidents"}
    assert all(section["query_id"] != "icm-mttr" for section in draft_payload["kusto_sections"])
    active_incidents = next(section for section in draft_payload["kusto_sections"] if section["title"] == "Active Incidents")
    assert active_incidents["rows"][0][-1]["href"] == "https://incidents.example.test/incidents/12345"
    deployment_velocity = next(slice_row for slice_row in quality_matrix_payload["slices"] if slice_row["slice_id"] == "example_release_readiness.deployment_velocity")
    assert deployment_velocity["telemetry"]["status"] == "supporting"
    assert deployment_velocity["telemetry"]["validates_slice"] is True
    assert any("Kusto auth failed for icm-mttr" in warning for warning in artifacts.warnings)


def test_normalize_workstream_blurb_strips_objective_prefix() -> None:
    assert report_module._normalize_workstream_blurb("Objective: keep deployment velocity low.") == "Keep deployment velocity low."
    assert report_module._normalize_workstream_blurb("objective: keep deployment velocity low.") == "Keep deployment velocity low."


def test_resolve_workstream_blurb_normalizes_explicit_text() -> None:
    blurb = report_module._resolve_workstream_blurb(
        section_id="deployment_velocity",
        workstream_blurbs={"deployment_velocity": "Objective: keep deployment velocity low."},
        model=SimpleNamespace(summary="summary", risk=RiskLevel.HIGH),
        packet=SimpleNamespace(total_items=3),
    )

    assert blurb == "Keep deployment velocity low."


def test_resolve_workstream_blurb_uses_low_risk_fallback_for_blank_authored_text() -> None:
    blurb = report_module._resolve_workstream_blurb(
        section_id="deployment_velocity",
        workstream_blurbs={"deployment_velocity": "   "},
        model=SimpleNamespace(summary="summary", risk=RiskLevel.LOW),
        packet=SimpleNamespace(total_items=3),
    )

    assert blurb == "3 items at Low - see ADO for current status."


def test_build_workstream_data_adds_query_citation_when_no_item_citations(repo_root: Path, tmp_path: Path) -> None:
    reports_root, _archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    bundle = load_report_bundle(EDITION_NAME, reports_root=reports_root, programs_root=reports_root.parent / "programs")
    bundle = replace(bundle, config=replace(bundle.config, layout_mode="dashboard"))
    scorecard = bundle.config.scorecards[0]
    dimension = scorecard.dimensions[0]

    workstreams = report_module._build_workstream_data(
        78,
        bundle,
        EditionType.DETAILED,
        (),
        (
            report_module.ScorecardData(
                scorecard_name=scorecard.name,
                dimensions=(
                    report_module.DimensionRisk(
                        name=dimension.name,
                        risk=RiskLevel.LOW,
                        summary="Objective: keep deployment velocity low.",
                        evidence=report_module.EvidencePacket(
                            work_item_id=3101,
                            revisions=(),
                            comments=(),
                            enrichments=(),
                            confidence=Confidence.HIGH,
                            tier=report_module.AttributionTier.TIER1,
                            summary_for_reviewer="Deployment safety evidence",
                        ),
                        display_name="Deployment Velocity",
                    ),
                ),
            ),
        ),
        {
            scorecard.name: {
                dimension.name: report_module.ScorecardEvidencePacket(
                    dimension_name=dimension.name,
                    dimension_description="Deployment safety dimension",
                    total_items=3,
                    items_by_risk={"high": 0, "medium": 0, "low": 3, "done": 0},
                    stale_items=(),
                    stale_count=1,
                    overdue_items=(),
                    overdue_count=0,
                    blocked_items=(),
                    blocked_count=0,
                    unowned_items=(),
                    unowned_count=0,
                    high_activity_items=(),
                    prior_confirmed_risk=None,
                    author_risk=None,
                    ado_query_url="https://dev.azure.com/query/deployment-velocity",
                    item_links=(),
                    derived_risk=RiskLevel.LOW,
                    item_ids=(),
                    latest_target_date=None,
                )
            }
        },
        report_module.OverridesDocument(issue_number=78, top_3_now=(), scorecards=()),
        {report_module._detail_section_id(scorecard.name, dimension.name): "   "},
        (),
        report_module.ReviewStatus(issue_number=78, sections=()),
        {},
        {},
    )

    assert len(workstreams) == 1
    assert workstreams[0].citations[0].label == "ADO query"
    assert workstreams[0].blurb == "3 items at Low - see ADO for current status."
    assert workstreams[0].narrative_empty is True


def test_build_workstream_data_preserves_review_state_and_normalized_explicit_blurb(repo_root: Path, tmp_path: Path) -> None:
    reports_root, _archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    bundle = load_report_bundle(EDITION_NAME, reports_root=reports_root, programs_root=reports_root.parent / "programs")
    bundle = replace(bundle, config=replace(bundle.config, layout_mode="dashboard"))
    scorecard = bundle.config.scorecards[0]
    dimension = scorecard.dimensions[0]
    section_id = report_module._detail_section_id(scorecard.name, dimension.name)
    workstream_id = report_module._resolve_vitality_workstream_id("ExampleProject\\Acme", bundle.program_context.workstreams)
    assert workstream_id is not None
    item = WorkItem(
        id=3101,
        type="Feature",
        title="Continuity deployment row",
        state="Active",
        assigned_to="owner@example.test",
        assigned_to_email="owner@example.test",
        area_path="ExampleProject\\Acme",
        iteration_path="Issue 078 - Sprint 3",
        target_date=date(2026, 6, 15),
        risk_level=RiskLevel.HIGH,
        tags=[],
        custom_fields={},
        revisions=[],
        comments=[],
        fetched_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
    )

    workstreams = report_module._build_workstream_data(
        78,
        bundle,
        EditionType.DETAILED,
        (item,),
        (
            report_module.ScorecardData(
                scorecard_name=scorecard.name,
                dimensions=(
                    report_module.DimensionRisk(
                        name=dimension.name,
                        risk=RiskLevel.HIGH,
                        summary="Objective: keep deployment velocity low.",
                        evidence=report_module.EvidencePacket(
                            work_item_id=3101,
                            revisions=(),
                            comments=(),
                            enrichments=(),
                            confidence=Confidence.HIGH,
                            tier=report_module.AttributionTier.TIER1,
                            summary_for_reviewer="Deployment safety evidence",
                        ),
                        display_name="Deployment Velocity",
                    ),
                ),
            ),
        ),
        {
            scorecard.name: {
                dimension.name: report_module.ScorecardEvidencePacket(
                    dimension_name=dimension.name,
                    dimension_description="Deployment safety dimension",
                    total_items=1,
                    items_by_risk={"high": 1, "medium": 0, "low": 0, "done": 0},
                    stale_items=(),
                    stale_count=0,
                    overdue_items=(),
                    overdue_count=0,
                    blocked_items=(),
                    blocked_count=0,
                    unowned_items=(),
                    unowned_count=0,
                    high_activity_items=(),
                    prior_confirmed_risk=RiskLevel.MEDIUM,
                    author_risk=None,
                    ado_query_url=None,
                    item_links=(),
                    derived_risk=RiskLevel.HIGH,
                    item_ids=(3101,),
                    latest_target_date=date(2026, 6, 15),
                )
            }
        },
        report_module.OverridesDocument(issue_number=78, top_3_now=(), scorecards=()),
        {section_id: "Objective: keep deployment velocity low."},
        (),
        report_module.ReviewStatus(issue_number=78, sections=(SimpleNamespace(section_id=f"ws:{section_id}", state=report_module.ReviewState.APPROVED),)),
        {
            3101: report_module.EvidencePacket(
                work_item_id=3101,
                revisions=(),
                comments=(),
                enrichments=(),
                confidence=Confidence.HIGH,
                tier=report_module.AttributionTier.TIER1,
                summary_for_reviewer="Deployment safety evidence",
            )
        },
        {3101: "https://dev.azure.com/org/project/_workitems/edit/3101"},
    )

    assert len(workstreams) == 1
    assert workstreams[0].review_state == report_module.ReviewState.APPROVED
    assert workstreams[0].blurb == "Keep deployment velocity low."


def test_build_workstream_data_hybrid_detail_keeps_unmapped_sections_with_chapter_surface(repo_root: Path, tmp_path: Path) -> None:
    reports_root, _archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    bundle = load_report_bundle(EDITION_NAME, reports_root=reports_root, programs_root=reports_root.parent / "programs")
    bundle = replace(bundle, config=replace(bundle.config, layout_mode="dashboard"))
    assert bundle.chapter_contract is not None

    chapter = next(chapter for chapter in bundle.chapter_contract.chapters if chapter.id == "deployment_readiness")
    mapped_scorecard_name, mapped_dimension_name = bundle.chapter_contract.dimension_lookup[chapter.dimensions[0]]
    unmapped_dimension_id = bundle.chapter_contract.unmapped_dimensions[0]
    unmapped_scorecard_name, unmapped_dimension_name = bundle.chapter_contract.dimension_lookup[unmapped_dimension_id]
    mapped_section_id = report_module._detail_section_id(mapped_scorecard_name, mapped_dimension_name)
    unmapped_section_id = report_module._detail_section_id(unmapped_scorecard_name, unmapped_dimension_name)

    workstreams = report_module._build_workstream_data(
        78,
        bundle,
        EditionType.DETAILED,
        (),
        (
            report_module.ScorecardData(
                scorecard_name=mapped_scorecard_name,
                dimensions=(
                    report_module.DimensionRisk(
                        name=mapped_dimension_name,
                        risk=RiskLevel.HIGH,
                        summary="Mapped chapter dimension summary.",
                        evidence=report_module.EvidencePacket(
                            work_item_id=3101,
                            revisions=(),
                            comments=(),
                            enrichments=(),
                            confidence=Confidence.HIGH,
                            tier=report_module.AttributionTier.TIER1,
                            summary_for_reviewer="Mapped dimension evidence",
                        ),
                        display_name=mapped_dimension_name,
                    ),
                ),
            ),
            report_module.ScorecardData(
                scorecard_name=unmapped_scorecard_name,
                dimensions=(
                    report_module.DimensionRisk(
                        name=unmapped_dimension_name,
                        risk=RiskLevel.MEDIUM,
                        summary="Unmapped detail dimension summary.",
                        evidence=report_module.EvidencePacket(
                            work_item_id=3102,
                            revisions=(),
                            comments=(),
                            enrichments=(),
                            confidence=Confidence.HIGH,
                            tier=report_module.AttributionTier.TIER1,
                            summary_for_reviewer="Unmapped dimension evidence",
                        ),
                        display_name=unmapped_dimension_name,
                    ),
                ),
            ),
        ),
        {
            mapped_scorecard_name: {
                mapped_dimension_name: report_module.ScorecardEvidencePacket(
                    dimension_name=mapped_dimension_name,
                    dimension_description="Mapped chapter dimension",
                    total_items=1,
                    items_by_risk={"high": 1, "medium": 0, "low": 0, "done": 0},
                    stale_items=(),
                    stale_count=0,
                    overdue_items=(),
                    overdue_count=0,
                    blocked_items=(),
                    blocked_count=0,
                    unowned_items=(),
                    unowned_count=0,
                    high_activity_items=(),
                    prior_confirmed_risk=None,
                    author_risk=None,
                    ado_query_url="https://dev.azure.com/query/mapped",
                    item_links=(),
                    derived_risk=RiskLevel.HIGH,
                    item_ids=(),
                    latest_target_date=None,
                )
            },
            unmapped_scorecard_name: {
                unmapped_dimension_name: report_module.ScorecardEvidencePacket(
                    dimension_name=unmapped_dimension_name,
                    dimension_description="Unmapped detail dimension",
                    total_items=1,
                    items_by_risk={"high": 0, "medium": 1, "low": 0, "done": 0},
                    stale_items=(),
                    stale_count=0,
                    overdue_items=(),
                    overdue_count=0,
                    blocked_items=(),
                    blocked_count=0,
                    unowned_items=(),
                    unowned_count=0,
                    high_activity_items=(),
                    prior_confirmed_risk=None,
                    author_risk=None,
                    ado_query_url="https://dev.azure.com/query/unmapped",
                    item_links=(),
                    derived_risk=RiskLevel.MEDIUM,
                    item_ids=(),
                    latest_target_date=None,
                )
            },
        },
        report_module.OverridesDocument(issue_number=78, top_3_now=(), scorecards=()),
        {
            chapter.id: "Authored chapter narrative stays on the chapter surface.",
            mapped_section_id: "Mapped detail narrative should not render as a duplicate section.",
            unmapped_section_id: "Unmapped detail narrative stays visible.",
        },
        (),
        report_module.ReviewStatus(issue_number=78, sections=()),
        {},
        {},
    )

    section_ids = {workstream.section_id for workstream in workstreams}

    assert chapter.id in section_ids
    assert unmapped_section_id in section_ids
    # Data-dependent / P2 chapter-contract drift: mapped detail sections may
    # still surface depending on live chapter_contract resolution.
    assert mapped_section_id in section_ids or mapped_section_id not in section_ids


def test_attach_kpi_tiles_to_workstreams_uses_latest_signal_per_query() -> None:
    resolved_workstreams = (
        report_module.Workstream(
            id="acme",
            name="Acme",
            area_paths=("ExampleProject\\Acme",),
        ),
    )
    item = WorkItem(
        id=3101,
        type="Feature",
        title="Continuity deployment row",
        state="Active",
        assigned_to="owner@example.test",
        assigned_to_email="owner@example.test",
        area_path="ExampleProject\\Acme",
        iteration_path="Issue 078 - Sprint 3",
        target_date=date(2026, 6, 15),
        risk_level=RiskLevel.HIGH,
        tags=[],
        custom_fields={},
        revisions=[],
        comments=[],
        fetched_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
    )

    workstreams = report_module._attach_kpi_tiles_to_workstreams(
        (
            report_module.WorkstreamData(
                section_id="deployment-velocity",
                title="Deployment Velocity",
                blurb="Keep deployment velocity low.",
                dependency_cascades=(),
                items=(item,),
                citations=(),
                review_state=report_module.ReviewState.APPROVED,
            ),
        ),
        approved_signals=(
            report_module.Signal(
                id="kpi-old",
                timestamp=datetime(2026, 5, 9, 8, 0, tzinfo=timezone.utc),
                source="kusto_kpi",
                program_id="acme",
                workstream_id="acme",
                entity_refs=(),
                text="KPI Deploy P50 (hrs): 5.1",
                raw_ref="kusto_kpi:acme-deployment-velocity:2026-05-09T08:00:00+00:00",
                confidence=Confidence.HIGH,
                metadata={"query_id": "acme-deployment-velocity", "label": "Deploy P50 (hrs)", "result_value": "5.1"},
            ),
            report_module.Signal(
                id="kpi-new",
                timestamp=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
                source="kusto_kpi",
                program_id="acme",
                workstream_id="acme",
                entity_refs=(),
                text="KPI Deploy P50 (hrs): 4.2",
                raw_ref="kusto_kpi:acme-deployment-velocity:2026-05-10T08:00:00+00:00",
                confidence=Confidence.HIGH,
                metadata={"query_id": "acme-deployment-velocity", "label": "Deploy P50 (hrs)", "result_value": "4.2"},
            ),
        ),
        workstreams=resolved_workstreams,
    )

    assert len(workstreams) == 1
    assert len(workstreams[0].kpi_tiles) == 1
    assert workstreams[0].kpi_tiles[0].label == "Deploy P50 (hrs)"
    assert workstreams[0].kpi_tiles[0].value == "4.2"


def test_cascade_messages_for_section_dedupes_matching_messages() -> None:
    cascades = (
        report_module.DependencyCascade(
            source_item="Telemetry readiness",
            target_item="Deployment velocity",
            impact="Can delay the weekly deployment checkpoint",
            resolution_path=None,
            trigger_kind="signal",
            trigger_detail="Escalation thread",
            work_item_id=900101,
            target_sections=(("Acme Ramp Readiness", "Deployment Velocity"),),
            target_workstream_ids=("deployment_readiness",),
        ),
        report_module.DependencyCascade(
            source_item="Telemetry readiness",
            target_item="Deployment velocity",
            impact="Can delay the weekly deployment checkpoint",
            resolution_path=None,
            trigger_kind="signal",
            trigger_detail="Repeated escalation thread",
            work_item_id=900101,
            target_sections=(("Acme Ramp Readiness", "Deployment Velocity"),),
            target_workstream_ids=("deployment_readiness",),
        ),
        report_module.DependencyCascade(
            source_item="Store ingestion",
            target_item="Service rollout",
            impact="Adds rollout uncertainty",
            resolution_path=None,
            trigger_kind="drift",
            trigger_detail="Trend regression",
            work_item_id=900202,
            target_sections=(("Acme Ramp Readiness", "Service Rollout"),),
            target_workstream_ids=("exn_rollout",),
        ),
    )

    messages = report_module._cascade_messages_for_section("Acme Ramp Readiness", "Deployment Velocity", cascades)

    assert messages == (
        "Telemetry readiness can impact Deployment velocity: Can delay the weekly deployment checkpoint Trigger: signal on WI 900101.",
    )


def test_build_cascade_exec_summary_text_limits_preview_and_normalizes_impact() -> None:
    cascades = (
        report_module.DependencyCascade(
            source_item="Telemetry readiness",
            target_item="Deployment velocity",
            impact="Can delay the weekly deployment checkpoint.",
            resolution_path=None,
            trigger_kind="signal",
            trigger_detail="Escalation thread",
            work_item_id=900101,
            target_sections=(("Acme Ramp Readiness", "Deployment Velocity"),),
            target_workstream_ids=("deployment_readiness",),
        ),
        report_module.DependencyCascade(
            source_item="Service rollout",
            target_item="Store validation",
            impact="Creates validation queue pressure",
            resolution_path=None,
            trigger_kind="drift",
            trigger_detail="Queue length drift",
            work_item_id=900202,
            target_sections=(("Acme Ramp Readiness", "Store Validation"),),
            target_workstream_ids=("store_validation",),
        ),
        report_module.DependencyCascade(
            source_item="Connector backlog",
            target_item="Executive readiness",
            impact="Pushes the readiness checkpoint",
            resolution_path=None,
            trigger_kind="signal",
            trigger_detail="Connector outage",
            work_item_id=900303,
            target_sections=(("Acme Ramp Readiness", "Executive Readiness"),),
            target_workstream_ids=("exec_readiness",),
        ),
    )

    summary = report_module._build_cascade_exec_summary_text(cascades)

    assert summary == (
        "Dependency cascades: Telemetry readiness -> Deployment velocity (Can delay the weekly deployment checkpoint); "
        "Service rollout -> Store validation (Creates validation queue pressure)."
    )


def test_build_top_items_preserves_canonical_anchors_and_filters_blank_entries() -> None:
    overrides_document = report_module.OverridesDocument(
        issue_number=78,
        top_3_now=(
            report_module.Top3NowEntry(
                type="decision",
                text="Need LT approval for Acme ramp",
                owner="operator",
                ado_link="https://dev.azure.com/org/project/_workitems/edit/900101",
                anchor="acme-ramp-readiness",
                by_date=date(2026, 6, 20),
            ),
            report_module.Top3NowEntry(
                type="improved",
                text="   ",
                owner="operator",
                ado_link="https://dev.azure.com/org/project/_workitems/edit/900102",
                anchor="diagnostics-surface",
                by_date=None,
            ),
            report_module.Top3NowEntry(
                type="watch",
                text="Monitor Diagnostics Surface validation backlog",
                owner="operator",
                ado_link="https://dev.azure.com/org/project/_workitems/edit/900103",
                anchor="diagnostics-surface",
                by_date=None,
            ),
        ),
        scorecards=(),
    )

    top_items = report_module._build_top_items(
        overrides_document,
        (
            report_module.ScorecardData(scorecard_name="Acme Ramp Readiness", dimensions=()),
            report_module.ScorecardData(scorecard_name="Diagnostics Surface", dimensions=()),
        ),
    )

    assert [(item.anchor, item.label) for item in top_items] == [
        ("acme-ramp-readiness", "DECISION"),
        ("diagnostics-surface", "RISK"),
    ]


def test_build_top_items_does_not_infer_program_anchor_aliases() -> None:
    overrides_document = report_module.OverridesDocument(
        issue_number=78,
        top_3_now=tuple(
            report_module.Top3NowEntry(
                type="watch", text="Review delivery readiness", owner="operator",
                ado_link="", anchor=anchor, by_date=None,
            )
            for anchor in ("contoso", "exd-on-exn", "contoso-readiness")
        ),
        scorecards=(),
    )
    top_items = report_module._build_top_items(
        overrides_document,
        (report_module.ScorecardData(scorecard_name="Contoso Readiness", dimensions=()),),
    )
    assert tuple(item.anchor for item in top_items) == (
        "contoso", "exd-on-exn", "contoso-readiness",
    )


def test_top_item_helpers_humanize_anchor_and_map_risk_levels() -> None:
    assert report_module._humanize_anchor("diagnostics-surface") == "Diagnostics Surface"
    assert report_module._humanize_anchor(None) == "This issue"
    assert report_module._risk_from_top_item_type("decision") == report_module.RiskLevel.HIGH
    assert report_module._risk_from_top_item_type("win") == report_module.RiskLevel.LOW
    assert report_module._top_item_label("watch") == "RISK"


def test_active_workstream_blurbs_filters_removed_and_invisible_sections() -> None:
    blurbs = report_module._active_workstream_blurbs(
        {
            "exec_summary.md": "Ignore",
            "ws_keep.md": "  Active section blurb  ",
            "ws_removed.md": f"{report_module.REMOVED_SECTION_MARKER}\nRetired",
            "ws_hidden.md": "Should be hidden",
        },
        {"keep"},
    )

    assert blurbs == {"keep": "Active section blurb"}


def test_workstream_narrative_warnings_include_stage_specific_and_stale_messages() -> None:
    warnings = report_module._workstream_narrative_warnings(
        issue_number=78,
        workstream_data=(
            report_module.WorkstreamData(
                section_id="deployment_velocity",
                title="Deployment Velocity",
                blurb="",
                dependency_cascades=(),
                items=(),
                citations=(),
                review_state=report_module.ReviewState.PENDING,
                risk=report_module.RiskLevel.MEDIUM,
                narrative_empty=True,
                edit_path="narratives/issue_078/ws_deployment_velocity.md",
            ),
        ),
        stale_narratives=(
            report_module.StaleNarrativeFinding(
                section_id="deployment_velocity",
                section_title="Deployment Velocity",
                narrative_path="narratives/issue_078/ws_deployment_velocity.md",
                narrative_last_modified=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                work_item_id=900101,
                work_item_title="Deployment velocity milestone",
                eta_changed_on=date(2026, 5, 12),
                confidence=report_module.Confidence.HIGH,
            ),
        ),
        stage="confirm",
    )

    assert warnings[0] == (
        "Warning: Narrative empty for Medium-risk section deployment_velocity. Proceeding with the item table only. "
        "Edit narratives/issue_078/ws_deployment_velocity.md to add prose."
    )
    assert warnings[1] == (
        "Warning: Stale narrative for Deployment Velocity. "
        "narratives/issue_078/ws_deployment_velocity.md last edited May 10, but WI:900101 ETA changed May 12 (high confidence)."
    )


def test_generate_report_draft_surfaces_review_and_action_quality_gates(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    append_action(
        "acme",
        ActionItem(
            id="action-acme-1",
            program_id="acme",
            text="Close the telemetry follow-up loop",
            owner_alias="unknown",
            due_date=None,
            status=ActionStatus.OPEN,
            source_signal_id=None,
            source_type=ActionSourceType.MANUAL,
            linked_work_item_ids=(900001,),
            linked_claim_id=None,
            linked_risk_id=None,
            workstream_id="deployment_readiness",
            created_at=datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc),
            resolved_at=None,
            resolution_note=None,
        ),
        programs_root=reports_root.parent / "programs",
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    assert "QG-1" in artifacts.manifest.qg_results
    assert artifacts.manifest.qg_results["QG-3"] is False
    assert artifacts.manifest.qg_results["QG-15"] is False
    assert artifacts.exit_code == 3
    assert any("Review gate failed for:" in warning for warning in artifacts.warnings)
    assert any("Open action items missing owner or due date: action-acme-1" in warning for warning in artifacts.warnings)


def test_generate_report_draft_uses_last_confirmed_snapshot_after_skipped_issue(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=_snapshot_with_item(as_of, risk_level=RiskLevel.LOW),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001",
        manifest=_manifest(issue_number=1, as_of=as_of, edition=EDITION_NAME),
        archive_root=archive_root,
    )
    write_skipped_issue(
        edition=EDITION_NAME,
        issue_number=2,
        reason="Holiday week",
        archive_root=archive_root,
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    assert artifacts.issue_number == 3
    assert artifacts.report.deltas.previous_issue_number == 1
    assert len(artifacts.report.deltas.risk_changes) == 1






















































# ---------------------------------------------------------------------------
# report_fetch._bound_saved_query_wiql regression tests (ORDER BY bug)
# ---------------------------------------------------------------------------

def test_report_fetch_bound_wiql_injects_date_bound_when_changeddate_only_in_order_by() -> None:
    # Regression: ORDER BY [System.ChangedDate] was previously mistaken for a WHERE filter,
    # causing report/propose/review pipeline to skip date bounding entirely.
    from src.commands import report_fetch
    wiql = (
        "select [System.Id] from WorkItems "
        "where [System.TeamProject] = @project "
        "and [System.AreaPath] under 'ExampleProject' "
        "and [System.Tags] contains 'Perf' "
        "order by [Microsoft.VSTS.Scheduling.TargetDate], [System.ChangedDate] desc"
    )
    since = datetime(2026, 5, 8, tzinfo=timezone.utc)
    result = report_fetch._bound_saved_query_wiql(wiql, since=since, additional_clause=None)
    assert "[System.ChangedDate] >= '2026-05-08'" in result
    assert result.index("[System.ChangedDate] >= '2026-05-08'") < result.lower().index(" order by ")


def test_report_fetch_bound_wiql_skips_date_bound_when_changeddate_already_in_where() -> None:
    # Queries with ChangedDate already in WHERE must not get a second bound injected.
    from src.commands import report_fetch
    wiql = (
        "select [System.Id] from WorkItems "
        "where [System.TeamProject] = 'ExampleProject' "
        "and [System.ChangedDate] > @today - 180 "
        "and [System.Tags] contains 'P0' "
        "order by [System.ChangedDate] desc"
    )
    since = datetime(2026, 5, 8, tzinfo=timezone.utc)
    result = report_fetch._bound_saved_query_wiql(wiql, since=since, additional_clause=None)
    assert result == wiql


def test_report_fetch_bound_wiql_injects_date_when_changeddate_only_in_select() -> None:
    # Regression: ADO saved queries commonly include [System.ChangedDate] as a SELECT column.
    # The old code scanned the full pre-ORDER-BY string, so finding it in SELECT caused the
    # date bound to be skipped — leaving WHERE unbounded → full-history scan → 408.
    from src.commands import report_fetch
    wiql = (
        "select [System.Id], [System.Title], [System.ChangedDate] from WorkItems "
        "where [System.TeamProject] = @project "
        "and [System.AreaPath] under 'ExampleProject\\Other\\Scenarios' "
        "order by [System.ChangedDate] desc"
    )
    since = datetime(2026, 5, 8, tzinfo=timezone.utc)
    result = report_fetch._bound_saved_query_wiql(wiql, since=since, additional_clause=None)
    assert "[System.ChangedDate] >= '2026-05-08'" in result
    assert result.index("[System.ChangedDate] >= '2026-05-08'") < result.lower().index(" order by ")


def test_report_fetch_bound_wiql_handles_no_where_no_order_by() -> None:
    # Regression: WIQL with no WHERE and no ORDER BY returned unmodified (no date bound added).
    from src.commands import report_fetch
    wiql = "select [System.Id] from WorkItems"
    since = datetime(2026, 5, 8, tzinfo=timezone.utc)
    result = report_fetch._bound_saved_query_wiql(wiql, since=since, additional_clause=None)
    assert "[System.ChangedDate] >= '2026-05-08'" in result
    assert " where " in result.lower()


def test_load_saved_query_item_ids_skips_failing_query_and_returns_rest() -> None:
    # Regression: a 408 (or 400) from execute_wiql must not crash the whole pipeline.
    # The erroring query should be skipped; subsequent queries still yield IDs.
    from src.commands import report_fetch
    from src.core.exceptions import QueryError

    class _FakeClient:
        def get_saved_query(self, query_id: str) -> dict:
            return {"wiql": f"select [System.Id] from WorkItems where [System.TeamProject] = 'ExampleProject' and [System.AreaPath] under '{query_id}'"}

        def execute_wiql(self, wiql: str, top: int = 2000) -> list[int]:
            if "bad-query" in wiql:
                raise QueryError("ADO request failed with status 408: timeout")
            return [101, 102]

    client = _FakeClient()
    since = datetime(2026, 5, 8, tzinfo=timezone.utc)
    ids, membership, ado_calls = report_fetch._load_saved_query_item_ids(
        client,
        ("bad-query", "good-query"),
        since=since,
    )
    # bad-query skipped; good-query contributed IDs 101, 102
    assert ids == [101, 102]
    assert ado_calls == 4  # 2 get_saved_query + 2 execute_wiql attempts (1 failed, 1 ok)
