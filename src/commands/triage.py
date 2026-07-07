from __future__ import annotations

import csv
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
import re
import sys
from typing import Any, cast
import uuid

import typer
import yaml

from src.ai.cost_guard import load_latest_run_state
from src.commands import report as report_command_helpers
from src.commands import vitality as vitality_command_helpers
from src.core.ask_lifecycle import DecisionAskLifecycleStage, build_decision_ask_lifecycle_proposals, format_decision_ask_lifecycle_line
from src.core.action_tracker import assess_action_staleness, load_action_resolution_candidate_ids
from src.core.analytics_store import load_contradiction_state
from src.core.assumption_tracker import check_validation_due
from src.core.cascade_detector import DependencyCascade
from src.core.archive_store import find_latest_confirmed_entry, read_archive_index, read_scorecard_history
from src.core.claim_tracker import ClaimAssessment, assess_claim_entries, load_open_claims, load_open_decision_asks
from src.core.communication_plan import load_communication_plan_entries
from src.core.coverage_gap import build_coverage_gaps
from src.core.decision_register import assess_proposed_decision_staleness
from src.core.dependency_graph import dependency_target_label, detect_cross_program_cascades
from src.core.dependency_scout import (
    DependencyProposal,
    DependencyProposalStatus,
    dependency_proposal_confidence_label,
    load_dependency_proposals,
)
from src.core.evidence_engine import build_evidence
from src.core.exceptions import ConfigError, StateError
from src.core.forecast_engine import ETAForecast
from src.core.gather_state_store import build_gather_integration_lines, load_gather_state
from src.core.incident_learning_synthesizer import IncidentClassPattern, IncidentRefPattern, build_incident_class_patterns, build_incident_ref_patterns, confidence_rank, normalize_incident_learning_summary
from src.core.incident_journal_store import read_incident_entries
from src.core.issue_projection import IssueProjection, build_issue_projection
from src.core.manifest_writer import build_run_manifest
from src.core.milestone_engine import (
    assess_milestone_health,
    build_critical_path,
    describe_milestone_schedule_variance,
    load_milestone_completion_date_history_map,
    load_milestone_target_date_history_map,
    summarize_milestone_completion_date_history,
    summarize_milestone_target_date_history,
)
from src.core.models import Confidence, EditionType, ReportData, RiskLevel, WorkItem
from src.core.models_v2 import ActionItem, ActionStatus, Assumption, AssumptionStatus, ClaimEntry, DecisionAsk, DecisionEntry, DecisionStatus, IncidentEntry, Milestone, MilestoneAssessment, MilestoneStatus, RaidChainLink, RiskEntry, RiskStatus, Scorecard, Signal, Workstream
from src.core.narrative_store import get_narratives_dir, load_narratives
from src.core.program_fact_store import (
    FactReviewState,
)
from src.core.program_reality import ProgramReality
from src.core.store_factory import build_signal_store_for_program_id, build_trajectory_store_for_program_id
from src.core.nudge_state_store import load_nudge_state
from src.core.overrides_store import load_overrides, merge_overrides
from src.core.knowledge_store import load_program_knowledge
from src.core.quality_gates import combine_gate_reports, evaluate_phase_1a_gates, evaluate_phase_1b_gates
from src.core.raid_graph import RaidChainResult, build_raid_chain_index
from src.core.response_tracker import has_response_since
from src.core.risk_register_engine import assess_risk_staleness, compute_risk_score
from src.core.review_status_store import load_review_status
from src.core.signal_review import signal_is_approved_for_evidence, signal_needs_review
from src.core.signal_normalizer import collect_unresolved_entity_refs
from src.core.signal_ranking import signal_source_family, sort_signals_for_ai_context
from src.core.snapshot_store import ARCHIVE_ROOT
from src.core.telemetry_summary import build_approved_telemetry_summary
from src.core.triage import IncidentLearning, CorrelatedTriageItem, StaleNarrativeFinding, TriageReport, detect_stale_narratives, finalize_triage_report, is_missing_narrative_content, render_triage_report
from src.core.trusted_baseline_store import load_trusted_baseline_issue
from src.core.trajectory_analyzer import analyze_trajectories
from src.core.config_loader import REPORTS_ROOT, load_bundle_with_mode
from src.core.continuation_contract import get_continuation_contract_path, load_continuation_contract
from src.core.archive_store import get_dimension_history, read_archive_index
from src.core.edition_resolver import get_nudge_paths, get_program_output_dir, resolve_edition, PROGRAMS_ROOT
from src.core.freshness_engine import build_freshness_report
from src.core.vitality_reporting import vitality_settings_from_program


WorkItemLoader = report_command_helpers.WorkItemLoader


@dataclass(frozen=True, slots=True)
class TriageArtifacts:
    report: TriageReport
    exit_code: int
    gather_integration_details: tuple[dict[str, str | bool | None], ...] = ()


def triage_command(
    edition: str = typer.Option(..., "--edition", help="Edition id, e.g. myprogram_weekly."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    artifacts = generate_triage_report(edition)
    if format == "human":
        _echo_console_safe(render_triage_report(artifacts.report))
    else:
        typer.echo(render_triage_output(artifacts, format=format), nl=False)
    raise typer.Exit(code=artifacts.exit_code)


def _echo_console_safe(text: str) -> None:
    try:
        typer.echo(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        typer.echo(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))


def render_triage_output(artifacts: TriageArtifacts, *, format: str) -> str:
    payload = _build_triage_payload(artifacts)
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow(("entry_type", "edition_name", "issue_number", "program_id", "status", "ref_id", "title", "owner_alias", "workstream_id", "detail"))
        writer.writerow(
            (
                "summary",
                payload["edition_name"],
                payload["issue_number"],
                payload["program_id"],
                payload["exit_code"],
                None,
                payload["readiness"]["summary"],
                None,
                None,
                json.dumps(payload["counts"], sort_keys=True),
            )
        )
        for section_name in ("blockers", "needs_attention", "milestones", "risks", "actions", "decisions", "assumptions", "integration_diagnostics", "telemetry", "cross_program_cascades", "ready"):
            for line in payload[section_name]:
                writer.writerow((section_name[:-1] if section_name.endswith("s") else section_name, payload["edition_name"], payload["issue_number"], payload["program_id"], None, None, line, None, None, None))
        for line in payload["contradictions"]:
            writer.writerow(("contradiction", payload["edition_name"], payload["issue_number"], payload["program_id"], None, None, line, None, None, None))
        for line in payload["decision_debt"]:
            writer.writerow(("decision_debt", payload["edition_name"], payload["issue_number"], payload["program_id"], None, None, line, None, None, None))
        for detail in payload["gather_integration_details"]:
            writer.writerow(
                (
                    "integration_detail",
                    payload["edition_name"],
                    payload["issue_number"],
                    payload["program_id"],
                    ("retryable" if detail["retryable"] else "non_retryable"),
                    detail["source"],
                    detail["message"],
                    None,
                    detail["stage"],
                    json.dumps(detail, sort_keys=True),
                )
            )
        for item in payload["active_issues"]:
            writer.writerow(("active_issue", payload["edition_name"], payload["issue_number"], payload["program_id"], item["severity"], item["work_item_id"], item["summary"], item["owner_alias"], item["workstream_id"], json.dumps({"ado_url": item["ado_url"], "confidence": item["confidence"], "linked_entity_ids": item["linked_entity_ids"]}, sort_keys=True)))
        for item in payload["correlated_items"]:
            writer.writerow(("correlated_item", payload["edition_name"], payload["issue_number"], payload["program_id"], item["work_item_state"], item["work_item_id"], item["work_item_title"], None, None, json.dumps({"confidence": item["confidence"]}, sort_keys=True)))
            for detail in item["details"]:
                writer.writerow(("correlated_detail", payload["edition_name"], payload["issue_number"], payload["program_id"], None, item["work_item_id"], None, None, None, detail))
        for gap in payload["coverage_gaps"]:
            writer.writerow(("coverage_gap", payload["edition_name"], payload["issue_number"], payload["program_id"], gap["state"], gap["work_item_id"], gap["title"], gap["assigned_to"], None, json.dumps({"confidence": gap["confidence"]}, sort_keys=True)))
        for finding in payload["stale_narratives"]:
            writer.writerow(("stale_narrative", payload["edition_name"], payload["issue_number"], payload["program_id"], None, finding["section_id"], finding["section_title"], None, None, finding["detail_with_confidence"]))
        for claim in payload["stale_claims"]:
            writer.writerow(("stale_claim", payload["edition_name"], payload["issue_number"], payload["program_id"], claim["effective_status"], claim["claim_id"], claim["text"], claim["owner_alias"], claim["workstream_id"], claim["reason_with_confidence"]))
        for proposal in payload["dependency_proposals"]:
            writer.writerow(("dependency_proposal", payload["edition_name"], payload["issue_number"], payload["program_id"], proposal["status"], proposal["id"], proposal["summary"], None, None, json.dumps({"accept_command": proposal["accept_command"], "confidence": proposal["confidence"], "detection_method": proposal["detection_method"], "from_item_id": proposal["from_item_id"], "from_workstream_id": proposal["from_workstream_id"], "occurrence_count": proposal["occurrence_count"], "to_item_id": proposal["to_item_id"], "to_workstream_id": proposal["to_workstream_id"]}, sort_keys=True)))
        for learning in payload["incident_learnings"]:
            writer.writerow(("incident_learning", payload["edition_name"], payload["issue_number"], payload["program_id"], learning["confidence"], None, learning["summary"], None, None, learning["summary_with_confidence"]))
        return buffer.getvalue()
    raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")


def _build_triage_payload(artifacts: TriageArtifacts) -> dict[str, Any]:
    report = artifacts.report
    return {
        "actions": list(report.actions),
        "active_issues": [
            {
                "ado_url": item.ado_url,
                "confidence": item.confidence.value,
                "linked_entity_ids": list(item.linked_entity_ids),
                "owner_alias": item.owner_alias,
                "severity": item.severity,
                "source_type": item.source_type,
                "summary": item.summary,
                "work_item_id": item.work_item_id,
                "workstream_id": item.workstream_id,
            }
            for item in report.active_issues
        ],
        "assumptions": list(report.assumptions),
        "blockers": list(report.blockers),
        "correlated_items": [
            {
                "confidence": item.confidence.value,
                "details": list(item.details),
                "work_item_id": item.work_item_id,
                "work_item_state": item.work_item_state,
                "work_item_title": item.work_item_title,
            }
            for item in report.correlated_items
        ],
        "contradictions": list(report.contradictions),
        "decision_debt": list(report.decision_debt),
        "counts": {
            "actions": len(report.actions),
            "active_issues": len(report.active_issues),
            "assumptions": len(report.assumptions),
            "blockers": len(report.blockers),
            "contradictions": len(report.contradictions),
            "correlated_items": len(report.correlated_items),
            "coverage_gaps": len(report.coverage_gaps),
            "cross_program_cascades": len(report.cross_program_cascades),
            "dependency_proposals": len(report.dependency_proposals),
            "decision_debt": len(report.decision_debt),
            "decisions": len(report.decisions),
            "incident_learnings": len(report.incident_learnings),
            "integration_diagnostics": len(report.integration_diagnostics),
            "milestones": len(report.milestones),
            "needs_attention": len(report.needs_attention),
            "open_decision_ask_count": report.open_decision_ask_count,
            "ready": len(report.ready),
            "risks": len(report.risks),
            "scorecard_composition": len(report.scorecard_composition),
            "section_roster": len(report.section_roster),
            "stale_claims": len(report.stale_claims),
            "stale_narratives": len(report.stale_narratives),
            "telemetry": len(report.telemetry),
        },
        "coverage_gap_window_days": report.coverage_gap_window_days,
        "coverage_gaps": [
            {
                "assigned_to": gap.assigned_to,
                "confidence": gap.confidence.value,
                "state": gap.state,
                "title": gap.title,
                "work_item_id": gap.work_item_id,
            }
            for gap in report.coverage_gaps
        ],
        "cross_program_cascades": list(report.cross_program_cascades),
        "dependency_proposals": [
            {
                "accept_command": f"vertex dependencies accept --program {report.program_id} --id {proposal.id}",
                "confidence": proposal.confidence.value,
                "detection_method": proposal.detection_method,
                "from_item_id": proposal.from_item_id,
                "from_workstream_id": proposal.from_workstream_id,
                "id": proposal.id,
                "occurrence_count": proposal.occurrence_count,
                "status": proposal.status.value,
                "summary": _format_dependency_proposal_triage_line(proposal, program_id=report.program_id or ""),
                "to_item_id": proposal.to_item_id,
                "to_workstream_id": proposal.to_workstream_id,
            }
            for proposal in report.dependency_proposals
        ],
        "decisions": list(report.decisions),
        "edition_name": report.edition_name,
        "exit_code": artifacts.exit_code,
        "gather_integration_details": list(artifacts.gather_integration_details),
        "incident_learnings": [
            {
                "confidence": learning.confidence.value,
                "summary": learning.summary,
                "summary_with_confidence": learning.summary_with_confidence,
            }
            for learning in report.incident_learnings
        ],
        "issue_number": report.issue_number,
        "integration_diagnostics": list(report.integration_diagnostics),
        "milestones": list(report.milestones),
        "needs_attention": list(report.needs_attention),
        "open_decision_ask_count": report.open_decision_ask_count,
        "program_id": report.program_id,
        "ready": list(report.ready),
        "scorecard_composition": list(report.scorecard_composition),
        "section_roster": list(report.section_roster),
        "readiness": {
            "coverage_gap_count": report.readiness.coverage_gap_count,
            "missing_narrative_count": report.readiness.missing_narrative_count,
            "missing_override_count": report.readiness.missing_override_count,
            "quality_gate_pass_rate": report.readiness.quality_gate_pass_rate,
            "quality_gate_passed": report.readiness.quality_gate_passed,
            "quality_gate_total": report.readiness.quality_gate_total,
            "score": report.readiness.score,
            "set_override_count": report.readiness.set_override_count,
            "summary": report.readiness.summary,
            "total_narrative_count": report.readiness.total_narrative_count,
            "total_override_count": report.readiness.total_override_count,
            "unreviewed_signal_count": report.readiness.unreviewed_signal_count,
            "written_narrative_count": report.readiness.written_narrative_count,
        },
        "risks": list(report.risks),
        "telemetry": list(report.telemetry),
        "stale_claims": [
            {
                "claim_id": assessment.claim.id,
                "confidence": assessment.confidence.value,
                "effective_status": assessment.effective_status,
                "issue_number": assessment.claim.issue_number,
                "owner_alias": getattr(assessment.claim, "owner_alias", None),
                "reason": assessment.reason,
                "reason_with_confidence": (
                    None
                    if assessment.reason is None
                    else f"{assessment.reason} ({assessment.confidence.value.lower()} confidence)"
                ),
                "text": assessment.claim.text,
                "workstream_id": assessment.claim.workstream_id,
            }
            for assessment in report.stale_claims
        ],
        "stale_narratives": [
            {
                "confidence": finding.confidence.value,
                "detail": finding.detail,
                "detail_with_confidence": finding.detail_with_confidence,
                "eta_changed_on": finding.eta_changed_on.isoformat(),
                "narrative_last_modified": finding.narrative_last_modified.isoformat(),
                "narrative_path": finding.narrative_path,
                "section_id": finding.section_id,
                "section_title": finding.section_title,
                "work_item_id": finding.work_item_id,
                "work_item_title": finding.work_item_title,
            }
            for finding in report.stale_narratives
        ],
        "vitality_enabled": report.vitality_enabled,
        "vitality_summary": None if report.vitality_summary is None else {
            "freshness_average_days": report.vitality_summary.freshness_average_days,
            "stale_owner_aliases": list(report.vitality_summary.stale_owner_aliases),
            "total_items": report.vitality_summary.total_items,
            "updated_this_week": report.vitality_summary.updated_this_week,
            "updated_this_week_percentage": report.vitality_summary.updated_this_week_percentage,
        },
    }


def generate_triage_report(
    edition_name: str,
    *,
    issue_number: int | None = None,
    as_of: datetime | None = None,
    reports_root: Path | None = None,
    archive_root: Path | None = None,
    output_root: Path | None = None,
    work_item_loader: WorkItemLoader | None = None,
    vitality_loader: vitality_command_helpers.VitalityLoader | None = None,
) -> TriageArtifacts:
    resolved_reports_root = reports_root or REPORTS_ROOT
    resolved_archive_root = archive_root or ARCHIVE_ROOT
    programs_root = resolved_reports_root.parent / "programs"
    current_time = as_of or datetime.now(timezone.utc)

    load_result = load_bundle_with_mode(
        edition_name,
        reports_root=resolved_reports_root,
        programs_root=programs_root,
    )
    bundle = load_result.bundle
    archive_index = read_archive_index(edition_name, archive_root=resolved_archive_root)
    resolved_issue_number = issue_number if issue_number is not None else report_command_helpers._next_issue_number(archive_index)
    trusted_baseline_issue_number = load_trusted_baseline_issue(
        edition_name,
        before_issue_number=resolved_issue_number,
        programs_root=programs_root,
    )
    previous_snapshot, previous_issue_number = report_command_helpers._load_previous_snapshot(
        edition_name,
        resolved_issue_number,
        resolved_archive_root,
        trusted_issue_number=trusted_baseline_issue_number,
    )
    resolved_edition_type = EditionType.from_string(bundle.config.edition.type)

    loader = work_item_loader or report_command_helpers._load_live_work_items
    items, ado_calls = loader(bundle, current_time)

    eta_forecasts = report_command_helpers._load_eta_forecasts(
        edition_name=edition_name,
        items=items,
        as_of=current_time,
        reports_root=resolved_reports_root,
    )
    evidence_window_start = current_time - timedelta(days=bundle.config.ado.date_window_days)
    evidence_by_item = {item.id: build_evidence(item, evidence_window_start, current_time) for item in items}
    continuity_snapshot = previous_snapshot if report_command_helpers._has_usable_continuity_baseline(previous_snapshot) else None
    continuity_previous_issue_number = previous_issue_number if continuity_snapshot is not None else None
    deltas = report_command_helpers._build_continuity_deltas(
        current_items=items,
        previous_snapshot=continuity_snapshot,
        issue_number=resolved_issue_number,
        previous_issue_number=continuity_previous_issue_number,
        evidence_by_item=evidence_by_item,
    )
    expected_scorecards = {
        scorecard.name: tuple(dimension.name for dimension in scorecard.dimensions)
        for scorecard in bundle.config.scorecards
    }
    overrides_document, _ = merge_overrides(
        issue_number=resolved_issue_number,
        expected_scorecards=expected_scorecards,
        existing=load_overrides(edition_name, reports_root=resolved_reports_root, issue_number=resolved_issue_number),
    )
    scorecard_packets = report_command_helpers._build_scorecard_packets(bundle, items, continuity_snapshot)
    scorecards, dimension_risks, _scorecard_deltas = report_command_helpers._build_scorecard_data(
        bundle=bundle,
        items=items,
        evidence_by_item=evidence_by_item,
        scorecard_packets=scorecard_packets,
        overrides_document=overrides_document,
        edition_name=edition_name,
        reports_root=resolved_reports_root,
    )
    top_items = report_command_helpers._build_top_items(overrides_document, scorecards)

    loaded_narratives = load_narratives(edition_name, resolved_issue_number, reports_root=resolved_reports_root)
    visible_section_ids, missing_narrative_ids = _resolve_missing_narratives(
        bundle=bundle,
        edition_type=resolved_edition_type,
        items=items,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        overrides_document=overrides_document,
        deltas=deltas,
        top_items=top_items,
        loaded_narratives=loaded_narratives,
    )
    freshness_report = build_freshness_report(
        current_items=items,
        issue_number=resolved_issue_number,
        as_of=current_time,
        stale_warn_days=bundle.editorial_rules.stale_warn_days,
        stale_block_days=bundle.editorial_rules.stale_block_days,
        previous_snapshot=previous_snapshot,
        previous_notification_state=None,
        program_context=bundle.program_context,
        workstream_narrative_history={},
    )
    review_status = load_review_status(edition_name, reports_root=resolved_reports_root) or report_command_helpers.ReviewStatus(
        issue_number=resolved_issue_number,
        sections=(),
    )
    workstream_blurbs = {
        section_id: (
            ""
            if is_missing_narrative_content(loaded_narratives.get(_narrative_filename(bundle, section_id), ""))
            else loaded_narratives.get(_narrative_filename(bundle, section_id), "").strip()
        )
        for section_id in visible_section_ids
    }

    triage_report = ReportData(
        issue_number=resolved_issue_number,
        edition=resolved_edition_type,
        generated_at=current_time,
        ado_data_as_of=current_time,
        program=report_command_helpers._build_model_program_context(bundle),
        items=items,
        deltas=deltas,
        scorecard=dimension_risks,
        scorecard_deltas=(),
        exec_summary_text="",
        workstream_blurbs=workstream_blurbs,
        freshness=freshness_report,
        hygiene_warnings=(),
        review_status=review_status,
        manifest_id=uuid.uuid4().hex,
    )
    snapshot = report_command_helpers._build_snapshot(triage_report, scorecard_packets)
    manifest = build_run_manifest(
        manifest_id=triage_report.manifest_id,
        issue_number=resolved_issue_number,
        edition=edition_name,
        started_at=current_time,
        ended_at=current_time,
        config_payload=bundle.config,
        snapshot=snapshot,
        html_content="",
        markdown_content="",
        ado_calls=ado_calls,
        ai_calls=0,
        ai_cost_usd=0.0,
        freshness_summary={"blocks": freshness_report.blocks, "warns": freshness_report.warns, "infos": freshness_report.infos},
        qg_results={},
        git_sha=None,
    )
    program_id: str | None = None
    vitality_enabled = False
    all_journal_signals: tuple[Signal, ...] = ()
    journal_signals: tuple[Signal, ...] = ()
    approved_signals: tuple[Signal, ...] = ()
    unreviewed_signals: tuple[Signal, ...] = ()
    resolved_workstreams: tuple[Workstream, ...] = ()
    resolved_scorecards: tuple[Scorecard, ...] = ()
    milestone_summaries: tuple[str, ...] = ()
    milestone_attention: tuple[str, ...] = ()
    risk_summaries: tuple[str, ...] = ()
    risk_attention: tuple[str, ...] = ()
    action_summaries: tuple[str, ...] = ()
    action_attention: tuple[str, ...] = ()
    decision_summaries: tuple[str, ...] = ()
    decision_attention: tuple[str, ...] = ()
    assumption_summaries: tuple[str, ...] = ()
    assumption_attention: tuple[str, ...] = ()
    integration_diagnostics: tuple[str, ...] = ()
    telemetry_summaries: tuple[str, ...] = ()
    scorecard_composition: tuple[str, ...] = ()
    section_roster: tuple[str, ...] = ()
    cross_program_cascades: tuple[str, ...] = ()
    active_issues: tuple[IssueProjection, ...] = ()
    correlated_items: tuple[CorrelatedTriageItem, ...] = ()
    contradiction_lines: tuple[str, ...] = ()
    decision_debt_lines: tuple[str, ...] = ()
    vitality_summary = None
    stale_narratives: tuple[StaleNarrativeFinding, ...] = ()
    open_claims: tuple[ClaimEntry, ...] = ()
    open_decision_asks: tuple[DecisionAsk, ...] = ()
    stale_claims: tuple[ClaimAssessment, ...] = ()
    open_decision_ask_count = 0
    # WI-3.3: Phase-3 signal quality counters
    auto_approved_signal_count = 0
    provisional_signal_count = 0
    material_conflict_count = 0
    # WI-2.6: Unresolved entity_ref count
    unresolved_entity_ref_count = 0
    if load_result.mode == "v2":
        resolved = resolve_edition(
            edition_name,
            programs_root=programs_root,
        )
        if resolved is None:
            raise ConfigError(f"Edition '{edition_name}' could not be resolved.")
        program_id = resolved.program.id
        knowledge = load_program_knowledge(program_id, programs_root=programs_root)
        resolved_workstreams = resolved.workstreams
        resolved_scorecards = resolved.scorecards
        vitality_settings = vitality_settings_from_program(resolved.raw_program)
        vitality_enabled = vitality_settings.triage
        if vitality_enabled:
            vitality_artifacts = vitality_command_helpers.generate_vitality_report(
                program_id,
                as_of=current_time,
                programs_root=programs_root,
                loader=vitality_loader,
            )
            vitality_summary = vitality_command_helpers.summarize_vitality(vitality_artifacts.scored_items)
        signal_store = build_signal_store_for_program_id(program_id, programs_root=programs_root)
        trajectory_store = build_trajectory_store_for_program_id(program_id, programs_root=programs_root)
        all_journal_signals = signal_store.read(program_id, end=current_time)
        journal_signals = tuple(signal for signal in all_journal_signals if signal.timestamp >= evidence_window_start)
        review_states = signal_store.read_reviews(program_id)
        approved_signals = tuple(
            signal
            for signal in journal_signals
            if signal_is_approved_for_evidence(signal, review_states)
        )
        approved_signals = sort_signals_for_ai_context(
            approved_signals,
            people_directory=knowledge.people_directory,
            as_of=current_time,
            source_confidence_order=resolved.program.source_confidence_order,
        )
        unreviewed_signals = tuple(
            signal
            for signal in journal_signals
            if signal_needs_review(signal, review_states)
        )
        unreviewed_signals = sort_signals_for_ai_context(
            unreviewed_signals,
            people_directory=knowledge.people_directory,
            as_of=current_time,
            source_confidence_order=resolved.program.source_confidence_order,
        )
        trajectories = {
            item.id: trajectory_store.read(program_id, item.id)
            for item in items
        }
        populated_trajectories = {
            work_item_id: points
            for work_item_id, points in trajectories.items()
            if points
        }
        drift_patterns = analyze_trajectories(populated_trajectories, as_of=current_time.date()) if populated_trajectories else ()
        _reality = ProgramReality.load(program_id, programs_root=programs_root)
        program_facts = _reality._snapshot
        dependencies = tuple(a.record for a in _reality.dependencies())
        risk_entries = tuple(a.record for a in _reality.risks())
        active_actions = tuple(
            a.record
            for a in _reality.actions()
            if a.record.status in {ActionStatus.PROPOSED, ActionStatus.OPEN, ActionStatus.IN_PROGRESS}
        )
        cross_program_cascades = tuple(
            dict.fromkeys(
                report_command_helpers._format_dependency_cascade(cascade)
                for cascade in cast(
                    tuple[DependencyCascade, ...],
                    detect_cross_program_cascades(
                        signals=approved_signals,
                        drift_patterns=drift_patterns,
                        dependencies=dependencies,
                    ),
                )
            )
        )
        cross_program_cascades = tuple(
            dict.fromkeys(
                (
                    *cross_program_cascades,
                    *_build_cross_program_dependency_understatement_lines(
                        program_id=program_id,
                        programs_root=programs_root,
                        dependencies=dependencies,
                        dimension_risks=dimension_risks,
                        scorecards=resolved_scorecards,
                    ),
                )
            )
        )
        open_claims = load_open_claims(program_id, programs_root)
        stale_claims = tuple(
            assessment
            for assessment in assess_claim_entries(
                open_claims,
                items=items,
                as_of=current_time,
                latest_statuses=None,
            )
            if assessment.effective_status in {"stale", "contradicted"}
        )
        open_decision_asks = load_open_decision_asks(program_id, programs_root)
        open_decision_ask_count = len(open_decision_asks)
        decision_debt_lines = _build_decision_debt_triage_lines(open_decision_asks, as_of=current_time)
        dependency_proposals = _build_dependency_proposal_triage_items(
            program_id=program_id,
            programs_root=programs_root,
        )
        item_urls = report_command_helpers._build_item_urls(bundle, items)
        workstream_data = report_command_helpers._build_workstream_data(
            issue_number=resolved_issue_number,
            bundle=bundle,
            edition_type=resolved_edition_type,
            items=items,
            scorecards=scorecards,
            scorecard_packets=scorecard_packets,
            overrides_document=overrides_document,
            workstream_blurbs=workstream_blurbs,
            dependency_cascades=(),
            review_status=review_status,
            evidence_by_item=evidence_by_item,
            item_urls=item_urls,
        )
        stale_narratives = detect_stale_narratives(
            program_id=program_id,
            workstream_data=workstream_data,
            narratives_dir=get_narratives_dir(edition_name, resolved_issue_number, resolved_reports_root),
            programs_root=programs_root,
        )
        milestone_summaries, milestone_attention = _build_milestone_triage_lines(
            program_id=program_id,
            programs_root=programs_root,
            items=items,
            dependencies=dependencies,
            as_of=current_time,
        )
        risk_summaries, risk_attention = _build_risk_triage_lines(
            programs_root=programs_root,
            risks=risk_entries,
            as_of=current_time,
        )
        risk_attention = risk_attention + _build_chronic_high_raci_attention(
            edition_name=edition_name,
            archive_root=resolved_archive_root,
            dimension_risks=dimension_risks,
            scorecards=resolved_scorecards,
            raw_workstreams=resolved.raw_workstreams,
        )
        action_summaries, action_attention = _build_action_triage_lines(
            program_id=program_id,
            programs_root=programs_root,
            actions=active_actions,
            as_of=current_time,
        )
        decision_summaries, decision_attention = _build_decision_triage_lines(
            decisions=tuple(a.record for a in _reality.decisions()),
            as_of=current_time,
        )
        assumption_summaries, assumption_attention = _build_assumption_triage_lines(
            program_id=program_id,
            programs_root=programs_root,
            as_of=current_time,
        )
        integration_diagnostics, gather_integration_details = _load_gather_integration_diagnostics(
            program_id=program_id,
            programs_root=programs_root,
        )
        integration_diagnostics = integration_diagnostics + _load_cost_guard_integration_diagnostics(
            edition_name=edition_name,
            programs_root=programs_root,
        )
        integration_diagnostics = integration_diagnostics + _load_persona_signal_triage_lines(
            edition_name=edition_name,
            issue_number=resolved_issue_number,
            programs_root=programs_root,
        )
        telemetry_summaries = _build_telemetry_triage_lines(approved_signals)
        assumption_attention = assumption_attention + _build_nudge_response_attention(
            program_id=program_id,
            programs_root=programs_root,
            items=items,
            as_of=current_time,
        )
        overdue_actions = assess_action_staleness(active_actions, current_time.date())
        icm_signals = tuple(signal for signal in journal_signals if signal_source_family(signal.source) == "icm")
        active_issues = build_issue_projection(
            items=items,
            freshness_report=freshness_report,
            icm_signals=icm_signals,
            open_asks=open_decision_asks,
            overdue_actions=overdue_actions,
            open_claims=open_claims,
            risk_entries=risk_entries,
            ado_item_base_url=report_command_helpers._ado_item_base_url(bundle),
        )
        correlated_items = _build_correlated_triage_items(
            items=items,
            approved_signals=approved_signals,
            unreviewed_signals=unreviewed_signals,
            claims=open_claims,
            program_id=program_id,
            programs_root=programs_root,
            risks=risk_entries,
            actions=active_actions,
            as_of=current_time,
        )
        contradiction_lines = _build_contradiction_triage_lines(
            load_contradiction_state(program_id, programs_root=programs_root)
        )
        incident_learning_lines = _build_incident_learning_triage_items(
            program_id=program_id,
            programs_root=programs_root,
            as_of=current_time,
            window_days=bundle.config.ado.date_window_days,
        )
        # WI-3.3: Compute signal quality counters from fact store
        for fact in program_facts.facts:
            if fact.fact_type == "signal.observation":
                if fact.review_state == FactReviewState.ACCEPTED:
                    auto_approved_signal_count += 1
                elif fact.review_state == FactReviewState.PROPOSED:
                    provisional_signal_count += 1
            elif fact.fact_type == "fact.conflict":
                if not fact.payload.get("resolved", False) and fact.payload.get("is_material", False):
                    material_conflict_count += 1
        # WI-2.6: Alias learning + curation list
        from src.core.entity_registry import EntityRegistry as _EntityRegistry
        from src.core.entity_alias_emitter import emit_entity_alias_facts as _emit_alias_facts
        _entity_registry = _EntityRegistry.load(program_id, programs_root=programs_root)
        _unresolved_refs = collect_unresolved_entity_refs(program_facts, _entity_registry)
        unresolved_entity_ref_count = len(_unresolved_refs)
        # Emit alias facts for all resolvable entity_refs (idempotent)
        try:
            _all_refs: set[str] = {ref for fact in program_facts.facts for ref in getattr(fact, "entity_refs", ())}
            _resolved_entities = tuple(
                e for ref in _all_refs
                if (e := _entity_registry.resolve(ref)) is not None
            )
            if _resolved_entities:
                _emit_alias_facts(program_id, _resolved_entities, programs_root=programs_root, emitted_by="triage_alias_learning")
        except Exception:
            pass  # Alias emission must never block triage
        # Write unresolved refs to alias_curation.yaml for human review (config floor preserved)
        if _unresolved_refs:
            try:
                _curation_path = programs_root / program_id / "knowledge" / "alias_curation.yaml"
                _curation_path.parent.mkdir(parents=True, exist_ok=True)
                _existing_unresolved: list[str] = []
                if _curation_path.exists():
                    _cdata = yaml.safe_load(_curation_path.read_text(encoding="utf-8")) or {}
                    _existing_unresolved = _cdata.get("unresolved_refs", [])
                _merged = sorted(set(_existing_unresolved) | _unresolved_refs)
                _curation_path.write_text(
                    yaml.dump({"unresolved_refs": _merged}, default_flow_style=False, allow_unicode=True),
                    encoding="utf-8",
                )
            except Exception:
                pass  # Curation file write must never block triage

    continuation_contract = load_continuation_contract(
        get_continuation_contract_path(get_program_output_dir(edition_name, programs_root=programs_root), resolved_issue_number)
    )
    if continuation_contract is not None:
        scorecard_composition = _build_scorecard_composition_triage_lines(continuation_contract)
        section_roster = _build_section_roster_triage_lines(continuation_contract)

    phase_1a_gate_report = evaluate_phase_1a_gates(
        ban_list_violations=(),
        verbosity_violations=(),
        manifest=manifest,
        expected_snapshot_hash=manifest.snapshot_hash,
        dimension_risks=dimension_risks,
        program_id=program_id,
        edition_name=edition_name,
        issue_number=resolved_issue_number,
        archive_root=resolved_archive_root,
        programs_root=programs_root,
    )

    quality_gate_report = combine_gate_reports(
        phase_1a_gate_report,
        evaluate_phase_1b_gates(
            freshness_report=freshness_report,
            items=items,
            as_of=current_time,
            deltas=deltas,
            edition_name=edition_name,
            issue_number=resolved_issue_number,
            workstream_blurbs=workstream_blurbs,
            program_context=bundle.program_context,
            dimension_risks=dimension_risks,
            overrides_document=overrides_document,
            approved_signals=approved_signals,
            narratives=loaded_narratives,
            journal_signals=all_journal_signals,
            program_id=program_id,
            program_maturity_level=resolved.program.maturity_level if resolved is not None else 0,
            workstreams=resolved_workstreams,
            scorecards=resolved_scorecards,
            archive_root=resolved_archive_root,
            programs_root=programs_root,
        ),
    )

    coverage_gaps = build_coverage_gaps(
        items,
        approved_signals=approved_signals,
        narratives=loaded_narratives,
        as_of=current_time,
    )
    triage = finalize_triage_report(
        edition_name=edition_name,
        issue_number=resolved_issue_number,
        program_id=program_id,
        quality_gate_report=quality_gate_report,
        unreviewed_signals=unreviewed_signals,
        milestone_summaries=milestone_summaries,
        milestone_attention=milestone_attention,
        risk_summaries=risk_summaries,
        risk_attention=risk_attention,
        action_summaries=action_summaries,
        action_attention=action_attention,
        decision_summaries=decision_summaries,
        decision_attention=decision_attention,
        assumption_summaries=assumption_summaries,
        assumption_attention=assumption_attention,
        integration_diagnostics=integration_diagnostics,
        telemetry_summaries=telemetry_summaries,
        scorecard_composition=scorecard_composition,
        section_roster=section_roster,
        cross_program_cascades=cross_program_cascades,
        active_issues=active_issues,
        correlated_items=correlated_items,
        missing_narrative_ids=missing_narrative_ids,
        total_narrative_count=len(visible_section_ids),
        missing_override_names=tuple(dimension.name for dimension in dimension_risks if dimension.risk.value == "unknown"),
        total_override_count=len(dimension_risks),
        coverage_gaps=coverage_gaps,
        eta_forecasts=eta_forecasts,
        items=items,
        coverage_gap_window_days=bundle.config.ado.date_window_days,
        vitality_enabled=vitality_enabled,
        vitality_summary=vitality_summary,
        stale_narratives=stale_narratives,
        stale_claims=stale_claims,
        open_decision_ask_count=open_decision_ask_count,
        contradictions=contradiction_lines,
        decision_debt=decision_debt_lines,
        dependency_proposals=dependency_proposals,
        incident_learnings=incident_learning_lines,
        auto_approved_signal_count=auto_approved_signal_count,
        provisional_signal_count=provisional_signal_count,
        material_conflict_count=material_conflict_count,
        unresolved_entity_ref_count=unresolved_entity_ref_count,
    )
    return TriageArtifacts(
        report=triage,
        exit_code=triage.exit_code,
        gather_integration_details=gather_integration_details,
    )


def _load_persona_signal_triage_lines(
    *,
    edition_name: str,
    issue_number: int,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[str, ...]:
    """P3-3: Surface failing persona signal checks at triage session start."""
    artifact_path = get_program_output_dir(edition_name, programs_root=programs_root) / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.persona_signal_coverage.json"
    if not artifact_path.exists():
        return ()
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ()
    if not isinstance(payload, dict):
        return ()

    results = payload.get("results", [])
    failing = [r for r in results if r.get("status") == "failed"]
    if not failing:
        return ()

    block_count = sum(1 for r in failing if r.get("effective_severity") == "block")
    warn_count = sum(1 for r in failing if r.get("effective_severity") == "warn")
    mode = payload.get("enforcement_mode", "warn")

    lines: list[str] = []
    if block_count:
        lines.append(
            f"Persona signal [{mode}]: {block_count} check(s) failing at block severity — "
            + ", ".join(
                f"{r.get('persona_id', '?')}/{r.get('check_id', '?')}"
                for r in failing
                if r.get("effective_severity") == "block"
            )
        )
    if warn_count:
        lines.append(
            f"Persona signal [{mode}]: {warn_count} check(s) failing at warn severity"
        )
    return tuple(lines)


def _load_cost_guard_integration_diagnostics(
    *,
    edition_name: str,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[str, ...]:
    try:
        state = load_latest_run_state(edition_name, programs_root=programs_root)
    except StateError as error:
        return (f"AI cost guard state invalid for {edition_name}: {error}",)
    if state is None or state.within_budget:
        return ()
    return (
        f"AI cost ceiling exceeded for {edition_name}: ${state.spent_usd:.3f} / ${state.budget_usd:.2f} across {state.ai_calls} AI call(s) (run {state.run_id}).",
    )


def _build_incident_learning_triage_items(
    *,
    program_id: str,
    programs_root: Path,
    as_of: datetime,
    window_days: int,
) -> tuple[IncidentLearning, ...]:
    incident_entries = read_incident_entries(
        program_id,
        start=as_of - timedelta(days=window_days),
        end=as_of,
        programs_root=programs_root,
    )
    if not incident_entries:
        return ()

    items: list[IncidentLearning] = []
    covered_signal_ids: set[str] = set()
    for pattern in build_incident_class_patterns(incident_entries):
        covered_signal_ids.update(pattern.signal_ids)
        items.append(IncidentLearning(summary=_render_incident_class_pattern_line(pattern), confidence=pattern.confidence))

    for ref_pattern in build_incident_ref_patterns(incident_entries):
        covered_signal_ids.update(ref_pattern.signal_ids)
        items.append(IncidentLearning(summary=_render_incident_pattern_line(ref_pattern), confidence=ref_pattern.confidence))

    for entry in sorted(
        incident_entries,
        key=lambda value: (value.observed_at, value.incident_id, value.signal_id),
        reverse=True,
    ):
        if entry.signal_id in covered_signal_ids:
            continue
        items.append(IncidentLearning(summary=_render_incident_entry_line(entry), confidence=entry.confidence))
    return tuple(items)


def _render_incident_pattern_line(pattern: IncidentRefPattern) -> str:
    workstream_label = f" ({pattern.workstream_id})" if pattern.workstream_id else ""
    incident_refs = ", ".join(pattern.incident_refs)
    if pattern.entry_count == 1:
        return f"Incident learning {pattern.ref}{workstream_label}: {pattern.summary_text}. Source: {incident_refs}."
    return (
        f"Incident learning {pattern.ref}{workstream_label}: Recurred across {pattern.entry_count} incident learnings. "
        f"{pattern.summary_text}. Source: {incident_refs}."
    )


def _render_incident_class_pattern_line(pattern: IncidentClassPattern) -> str:
    workstream_label = f" ({', '.join(pattern.workstream_ids)})" if pattern.workstream_ids else ""
    incident_refs = ", ".join(pattern.incident_refs)
    linked_refs = f" Refs: {', '.join(pattern.linked_refs)}." if pattern.linked_refs else ""
    return (
        f"Incident class {pattern.class_label}{workstream_label}: Recurred across {pattern.entry_count} incident learnings. "
        f"{pattern.summary_text}. Source: {incident_refs}.{linked_refs}"
    )


def _render_incident_entry_line(entry: IncidentEntry) -> str:
    workstream_label = f" ({entry.workstream_id})" if entry.workstream_id else ""
    summary = normalize_incident_learning_summary(entry.belief_change_summary)
    return f"Incident learning IcM {entry.incident_id}{workstream_label}: {summary}."


def _load_gather_integration_diagnostics(
    *,
    program_id: str,
    programs_root: Path,
) -> tuple[tuple[str, ...], tuple[dict[str, str | bool | None], ...]]:
    gather_state = load_gather_state(program_id, programs_root=programs_root)
    details = tuple(
        {
            "source": detail.source,
            "stage": detail.stage,
            "retryable": detail.retryable,
            "message": detail.message,
            "operator_action": detail.operator_action,
        }
        for detail in (gather_state.integration_error_details if gather_state is not None else ())
    )
    return build_gather_integration_lines(gather_state), details


def _build_contradiction_triage_lines(packets) -> tuple[str, ...]:
    lines: list[str] = []
    for packet in packets:
        if not packet.contradictions:
            continue
        primary = packet.contradictions[0]
        workstream_label = packet.workstream_id or "unmapped"
        line = f"WI:{packet.work_item_id} ({workstream_label}) - {primary.summary}"
        if packet.recommended_resolution is not None:
            line = (
                f"{line}. Prefer {packet.recommended_resolution.winning_source.value} "
                f"({packet.recommended_resolution.confidence.value})"
            )
        lines.append(line)
    return tuple(lines)


def _build_decision_debt_triage_lines(
    decision_asks: tuple[DecisionAsk, ...],
    *,
    as_of: datetime,
) -> tuple[str, ...]:
    proposals = build_decision_ask_lifecycle_proposals(
        decision_asks,
        as_of=as_of,
        minimum_stage=DecisionAskLifecycleStage.NUDGE,
    )
    return tuple(
        format_decision_ask_lifecycle_line(proposal, include_command=True)
        for proposal in proposals
    )


def _format_dependency_proposal_triage_line(
    proposal: DependencyProposal,
    *,
    program_id: str,
) -> str:
    return (
        f"{proposal.id} | {proposal.from_workstream_id}:{proposal.from_item_id} -> "
        f"{proposal.to_workstream_id}:{proposal.to_item_id} | {proposal.detection_method} | "
        f"{proposal.occurrence_count} signal(s) | {dependency_proposal_confidence_label(proposal)} | "
        f"Accept: vertex dependencies accept --program {program_id} --id {proposal.id}"
    )


def _build_dependency_proposal_triage_items(
    *,
    program_id: str,
    programs_root: Path,
) -> tuple[DependencyProposal, ...]:
    return tuple(
        proposal
        for proposal in load_dependency_proposals(program_id, programs_root=programs_root)
        if proposal.status == DependencyProposalStatus.PROPOSED
    )


def _build_cross_program_dependency_understatement_lines(
    *,
    program_id: str,
    programs_root: Path,
    dependencies: tuple,
    dimension_risks,
    scorecards: tuple[Scorecard, ...],
) -> tuple[str, ...]:
    low_risk_dimensions = tuple(
        dimension
        for dimension in dimension_risks
        if dimension.risk == RiskLevel.LOW
    )
    if not low_risk_dimensions:
        return ()

    workstream_ids_by_dimension = _scorecard_dimension_workstream_ids(scorecards)
    if not workstream_ids_by_dimension:
        return ()

    foreign_program_risk: dict[str, RiskLevel | None] = {}
    lines: list[str] = []
    for dependency in dependencies:
        if dependency.from_program_id != program_id or dependency.to_program_id == program_id:
            continue
        if not dependency.from_workstream_id:
            continue
        matching_dimension_names = tuple(
            dimension.name
            for dimension in low_risk_dimensions
            if dependency.from_workstream_id in workstream_ids_by_dimension.get(dimension.name, ())
        )
        if not matching_dimension_names:
            continue
        if dependency.to_program_id not in foreign_program_risk:
            foreign_program_risk[dependency.to_program_id] = _load_latest_program_overall_risk(
                dependency.to_program_id,
                programs_root=programs_root,
            )
        counterpart_risk = foreign_program_risk[dependency.to_program_id]
        if counterpart_risk != RiskLevel.HIGH:
            continue
        for dimension_name in matching_dimension_names:
            prefix = "LOW risk may be understated - "
            if (dependency.resolution_path or "").startswith("cross_org"):
                prefix = "LOW risk may be understated - cross-org dependency pressure: "
            lines.append(
                f"{prefix}{dimension_name} depends on {dependency_target_label(dependency)}, "
                f"and {dependency.to_program_id}'s latest confirmed issue is HIGH"
            )
    return tuple(dict.fromkeys(lines))


def _load_latest_program_overall_risk(program_id: str, *, programs_root: Path) -> RiskLevel | None:
    program_path = programs_root / program_id / "program.yaml"
    if not program_path.exists():
        return None
    try:
        program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(program_document, dict):
        return None

    archive_root = programs_root / program_id / "archive"
    primary_edition = _select_primary_program_edition(program_document, archive_root)
    if primary_edition is None:
        return None

    latest_confirmed = find_latest_confirmed_entry(read_archive_index(primary_edition, archive_root=archive_root))
    if latest_confirmed is None:
        return None

    current_risks = [
        RiskLevel.from_string(str(entry.get("risk") or ""))
        for entry in read_scorecard_history(primary_edition, archive_root=archive_root)
        if _triage_issue_number(entry.get("issue_number")) == latest_confirmed.issue_number
        and str(entry.get("risk") or "").strip()
    ]
    if not current_risks:
        return None
    return max(current_risks, key=_triage_risk_rank)


def _select_primary_program_edition(program_document: dict[str, object], archive_root: Path) -> str | None:
    communication_plan_entries = tuple(load_communication_plan_entries(program_document))
    if not archive_root.exists():
        return communication_plan_entries[0].edition if communication_plan_entries else None

    confirmed_editions = {
        edition_dir.name
        for edition_dir in archive_root.iterdir()
        if edition_dir.is_dir()
        and find_latest_confirmed_entry(read_archive_index(edition_dir.name, archive_root=archive_root)) is not None
    }
    if not confirmed_editions:
        return communication_plan_entries[0].edition if communication_plan_entries else None

    for entry in communication_plan_entries:
        if entry.edition in confirmed_editions:
            return entry.edition
    return min(confirmed_editions)


def _triage_issue_number(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _triage_risk_rank(level: RiskLevel) -> int:
    return {
        RiskLevel.UNKNOWN: 0,
        RiskLevel.DONE: 1,
        RiskLevel.LOW: 2,
        RiskLevel.MEDIUM: 3,
        RiskLevel.HIGH: 4,
    }[level]


def _format_decision_debt_line(ask: DecisionAsk, *, as_of: datetime) -> str:
    age_days = max(0, (as_of.date() - ask.ask_date).days)
    refs = ", ".join(ask.entity_refs) if ask.entity_refs else "no linked refs"
    return (
        f"{age_days} day(s) open | Issue #{ask.issue_number:03d} {ask.id} | "
        f"owner {ask.owner_alias} | refs {refs} | {ask.text}"
    )


def _build_scorecard_composition_triage_lines(continuation_contract) -> tuple[str, ...]:
    composition = continuation_contract.scorecard_composition
    lines: list[str] = []
    if composition.proposed_additions:
        lines.append(
            f"{len(composition.proposed_additions)} proposed addition(s) vs trusted issue #{composition.frozen_from_issue}"
        )
        lines.extend(
            f"Add: {scorecard_name} :: {dimension_name}"
            for scorecard_name, dimension_name in composition.proposed_additions
        )
    if composition.proposed_removals:
        lines.append(
            f"{len(composition.proposed_removals)} proposed removal(s) vs trusted issue #{composition.frozen_from_issue}"
        )
        lines.extend(
            f"Missing current evidence: {scorecard_name} :: {dimension_name}"
            for scorecard_name, dimension_name in composition.proposed_removals
        )
    if composition.removed_by_override:
        lines.append(f"{len(composition.removed_by_override)} dimension removal(s) explicitly approved in overrides")
        lines.extend(
            f"Removed by override: {scorecard_name} :: {dimension_name}"
            for scorecard_name, dimension_name in composition.removed_by_override
        )
    return tuple(lines)


def _build_section_roster_triage_lines(continuation_contract) -> tuple[str, ...]:
    roster = continuation_contract.section_roster
    lines: list[str] = []
    if roster.added_sections:
        lines.append(f"{len(roster.added_sections)} section addition(s) vs trusted issue #{continuation_contract.prior_trusted_issue}")
        lines.extend(f"Add section: {section_id}" for section_id in roster.added_sections)
    if roster.removed_sections:
        lines.append(f"{len(roster.removed_sections)} section(s) missing from the current draft roster")
        lines.extend(f"Missing prior section: {section_id}" for section_id in roster.removed_sections)
    return tuple(lines)


def _resolve_missing_narratives(
    *,
    bundle,
    edition_type: EditionType,
    items: tuple[WorkItem, ...],
    scorecards,
    scorecard_packets,
    overrides_document,
    deltas,
    top_items,
    loaded_narratives: dict[str, str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if report_command_helpers._is_continuity_layout(bundle):
        visible_ids = tuple(chapter.id for chapter in report_command_helpers._visible_continuity_chapters(bundle, edition_type))
        missing_ids = tuple(
            chapter_id
            for chapter_id in visible_ids
            if is_missing_narrative_content(loaded_narratives.get(f"chapter_{chapter_id}.md", ""))
        )
        return visible_ids, missing_ids

    if bundle.chapter_contract is not None:
        visible_ids = tuple(
            chapter.id
            for chapter in bundle.chapter_contract.chapters_for(edition_type.value)
            if not chapter.chapter_exempt
        )
        missing_ids = tuple(
            chapter_id
            for chapter_id in visible_ids
            if is_missing_narrative_content(loaded_narratives.get(f"chapter_{chapter_id}.md", ""))
        )
        return visible_ids, missing_ids

    visible_id_set = tuple(
        sorted(
            report_command_helpers._visible_detail_section_ids(
                bundle,
                overrides_document,
                edition_type=edition_type,
                items=items,
                scorecards=scorecards,
                scorecard_packets=scorecard_packets,
                deltas=deltas,
                scorecard_deltas=(),
                top_items=top_items,
            )
        )
    )
    missing_ids = tuple(
        section_id
        for section_id in visible_id_set
        if is_missing_narrative_content(loaded_narratives.get(f"ws_{section_id}.md", ""))
    )
    return visible_id_set, missing_ids


def _build_milestone_triage_lines(
    *,
    program_id: str,
    programs_root: Path,
    items: tuple[WorkItem, ...],
    dependencies: tuple,
    as_of: datetime,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    try:
        milestones = tuple(
            a.record
            for a in ProgramReality.load(program_id, programs_root=programs_root).milestones()
        )
    except ConfigError as exc:
        return (), (f"Milestones skipped: {exc}",)

    if not milestones:
        return (), ()

    trajectory_store = build_trajectory_store_for_program_id(program_id, programs_root=programs_root)
    trajectories = {
        item_id: trajectory_store.read(program_id, item_id)
        for milestone in milestones
        for item_id in milestone.linked_work_item_ids
    }
    target_date_history = load_milestone_target_date_history_map(
        program_id,
        milestones,
        programs_root=programs_root,
    )
    critical_path_ids = {milestone.id for milestone in build_critical_path(milestones, dependencies)}
    assessments = tuple(
        replace(
            assess_milestone_health(
                milestone,
                items,
                trajectories,
                as_of,
            ),
            critical_path=milestone.id in critical_path_ids,
        )
        for milestone in milestones
    )
    completion_date_history = load_milestone_completion_date_history_map(
        program_id,
        milestones,
        current_completion_dates={assessment.milestone_id: assessment.completion_date for assessment in assessments},
        programs_root=programs_root,
    )
    summaries = tuple(
        _format_milestone_summary(
            milestone,
            assessment,
            schedule_summary=describe_milestone_schedule_variance(milestone, items, trajectories, as_of),
            target_history_summary=summarize_milestone_target_date_history(
                target_date_history.get(milestone.id, ()),
                prefix="target history",
            ),
            completion_history_summary=summarize_milestone_completion_date_history(
                completion_date_history.get(milestone.id, ()),
                prefix="completion history",
            ),
        )
        for milestone, assessment in zip(milestones, assessments, strict=False)
    )
    attention_count = sum(
        1
        for assessment in assessments
        if assessment.computed_health in {MilestoneStatus.AT_RISK, MilestoneStatus.MISSED}
    )
    attention: tuple[str, ...] = ()
    if attention_count:
        attention = (f"{attention_count} milestone at risk or missed (see MILESTONE HEALTH)",)
    return summaries, attention


def _build_risk_triage_lines(
    *,
    programs_root: Path,
    risks: tuple[RiskEntry, ...],
    as_of: datetime,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if not risks:
        return (), ()
    program_id = risks[0].program_id
    raid_analyses = build_raid_chain_index(program_id, programs_root=programs_root)

    summaries = tuple(
        _format_risk_triage_line(risk, as_of=as_of, raid_analysis=raid_analyses.get(risk.id))
        for risk in sorted(
            risks,
            key=lambda entry: (
                0 if entry.status in {RiskStatus.OPEN, RiskStatus.ESCALATED} else 1,
                -compute_risk_score(entry),
                entry.title.lower(),
            ),
        )
    )
    stale_count = sum(1 for risk in risks if assess_risk_staleness(risk, as_of.date()))
    open_count = sum(1 for risk in risks if risk.status in {RiskStatus.OPEN, RiskStatus.ESCALATED})
    attention: tuple[str, ...] = ()
    if stale_count:
        attention = (f"{stale_count} stale risk register entr{'y' if stale_count == 1 else 'ies'} (see RISK REGISTER)",)
    elif open_count:
        attention = (f"{open_count} open risk register entr{'y' if open_count == 1 else 'ies'} tracked",)
    return summaries, attention


def _format_risk_triage_line(risk: RiskEntry, *, as_of: datetime, raid_analysis: RaidChainResult | None = None) -> str:
    stale_label = "stale" if assess_risk_staleness(risk, as_of.date()) else "current"
    line = (
        f"{risk.status.value.upper()} | score {compute_risk_score(risk)} | {stale_label} | "
        f"{risk.title} (owner {risk.owner_alias})"
    )
    if raid_analysis is None or len(raid_analysis.links) <= 1:
        return line
    mitigation = "mitigating action present" if raid_analysis.has_mitigating_action else "no linked in-progress/done action"
    return f"{line} | RAID {_render_raid_chain(raid_analysis.links)} | {mitigation}"


def _render_raid_chain(links: tuple[RaidChainLink, ...]) -> str:
    return " -> ".join(f"{link.node_type}:{link.node_id}[{link.status}]" for link in links)


def _build_chronic_high_raci_attention(
    *,
    edition_name: str,
    archive_root: Path,
    dimension_risks,
    scorecards: tuple[Scorecard, ...],
    raw_workstreams: dict[str, object],
) -> tuple[str, ...]:
    accountable_by_workstream = _accountable_aliases_by_workstream(raw_workstreams)
    if not accountable_by_workstream:
        return ()

    dimension_workstream_ids = _scorecard_dimension_workstream_ids(scorecards)
    attention: list[str] = []
    for dimension in dimension_risks:
        if dimension.risk != RiskLevel.HIGH:
            continue
        linked_workstream_ids = dimension_workstream_ids.get(dimension.name, ())
        if not linked_workstream_ids:
            continue
        accountable_aliases = tuple(
            dict.fromkeys(
                alias
                for workstream_id in linked_workstream_ids
                if (alias := accountable_by_workstream.get(workstream_id)) is not None
            )
        )
        if not accountable_aliases:
            continue
        history = _dimension_history_levels(
            edition_name,
            dimension.name,
            archive_root=archive_root,
        )
        if _consecutive_high_count((*history, dimension.risk)) < 4:
            continue
        attention.append(
            f"Chronic High dimension {dimension.name} - Escalate to: {', '.join(accountable_aliases)}"
        )
    return tuple(attention)


def _scorecard_dimension_workstream_ids(scorecards: tuple[Scorecard, ...]) -> dict[str, tuple[str, ...]]:
    workstream_ids_by_dimension: dict[str, list[str]] = {}
    for scorecard in scorecards:
        for dimension in scorecard.dimensions:
            workstream_id = dimension.workstream_id.strip()
            if not workstream_id:
                continue
            workstream_ids_by_dimension.setdefault(dimension.name, []).append(workstream_id)
    return {
        name: tuple(dict.fromkeys(workstream_ids))
        for name, workstream_ids in workstream_ids_by_dimension.items()
    }


def _accountable_aliases_by_workstream(raw_workstreams: dict[str, object]) -> dict[str, str]:
    workstreams_payload = raw_workstreams.get("workstreams")
    if not isinstance(workstreams_payload, list):
        return {}

    accountable_by_workstream: dict[str, str] = {}
    for entry in workstreams_payload:
        if not isinstance(entry, dict):
            continue
        workstream_id = entry.get("id")
        if not isinstance(workstream_id, str) or not workstream_id.strip():
            continue
        raci = entry.get("raci")
        if not isinstance(raci, dict):
            continue
        accountable = raci.get("accountable")
        normalized_accountable = _normalize_alias(accountable if isinstance(accountable, str) else None)
        if normalized_accountable == "unassigned":
            continue
        accountable_by_workstream[workstream_id.strip()] = normalized_accountable
    return accountable_by_workstream


def _dimension_history_levels(
    edition_name: str,
    dimension_name: str,
    *,
    archive_root: Path,
) -> tuple[RiskLevel, ...]:
    history: list[RiskLevel] = []
    for entry in get_dimension_history(edition_name, dimension_name, archive_root=archive_root):
        raw_risk = entry.get("risk")
        if not isinstance(raw_risk, str):
            continue
        try:
            history.append(RiskLevel.from_string(raw_risk))
        except ValueError:
            continue
    return tuple(history)


def _consecutive_high_count(history: tuple[RiskLevel, ...]) -> int:
    count = 0
    for risk in reversed(history):
        if risk != RiskLevel.HIGH:
            break
        count += 1
    return count


def _normalize_alias(value: str | None) -> str:
    if value is None:
        return "unassigned"
    normalized = value.strip().lower()
    if "@" in normalized:
        normalized = normalized.split("@", 1)[0]
    return normalized or "unassigned"


def _build_action_triage_lines(
    *,
    program_id: str,
    programs_root: Path,
    actions: tuple[ActionItem, ...],
    as_of: datetime,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if not actions:
        return (), ()

    overdue_ids = {action.id for action in assess_action_staleness(actions, as_of.date())}
    resolution_candidate_ids = _load_action_resolution_candidate_ids(
        program_id=program_id,
        programs_root=programs_root,
        actions=actions,
    )
    summaries = tuple(
        _format_action_triage_line(
            action,
            overdue=action.id in overdue_ids,
            resolution_candidate=action.id in resolution_candidate_ids,
        )
        for action in sorted(
            actions,
            key=lambda entry: (
                0 if entry.id in overdue_ids else 1,
                entry.due_date or datetime.max.date(),
                entry.created_at,
                entry.text.lower(),
            ),
        )
    )
    open_count = sum(1 for action in actions if action.status in {ActionStatus.OPEN, ActionStatus.IN_PROGRESS})
    proposed_count = sum(1 for action in actions if action.status is ActionStatus.PROPOSED)
    overdue_count = len(overdue_ids)
    resolution_candidate_count = len(resolution_candidate_ids)
    attention: list[str] = []
    if overdue_count:
        attention.append(f"{overdue_count} overdue action entr{'y' if overdue_count == 1 else 'ies'} (see ACTIONS)")
    if resolution_candidate_count:
        attention.append(
            f"{resolution_candidate_count} open action entr{'y' if resolution_candidate_count == 1 else 'ies'} "
            f"candidate for resolution after linked ADO update "
            f"(vertex actions resolve --program {program_id} --id <action-id>)"
        )
    if proposed_count:
        attention.append(f"{proposed_count} proposed action entr{'y' if proposed_count == 1 else 'ies'} pending review (vertex actions review --program {program_id})")
    elif open_count:
        attention.append(f"{open_count} open action entr{'y' if open_count == 1 else 'ies'} tracked")
    return summaries, tuple(attention)


def _build_telemetry_triage_lines(approved_signals: tuple[Signal, ...]) -> tuple[str, ...]:
    summary = build_approved_telemetry_summary(approved_signals)
    if summary is None:
        return ()
    return (f"Latest approved telemetry: {summary}",)


def _load_action_resolution_candidate_ids(
    *,
    program_id: str,
    programs_root: Path,
    actions: tuple[ActionItem, ...],
) -> set[str]:
    return set(load_action_resolution_candidate_ids(program_id, actions, programs_root=programs_root))


def _build_decision_triage_lines(
    *,
    decisions: tuple[DecisionEntry, ...],
    as_of: datetime,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if not decisions:
        return (), ()

    summaries = tuple(
        _format_decision_triage_line(entry, as_of=as_of)
        for entry in sorted(
            decisions,
            key=lambda entry: (
                0 if entry.status is DecisionStatus.PROPOSED else 1,
                0 if assess_proposed_decision_staleness(entry, as_of.date()) else 1,
                entry.decision_date,
                entry.title.lower(),
            ),
        )
    )
    stale_count = sum(1 for entry in decisions if assess_proposed_decision_staleness(entry, as_of.date()))
    proposed_count = sum(1 for entry in decisions if entry.status is DecisionStatus.PROPOSED)
    attention: tuple[str, ...] = ()
    if stale_count:
        attention = (f"{stale_count} proposed decision entr{'y' if stale_count == 1 else 'ies'} pending >14 days (see DECISIONS)",)
    elif proposed_count:
        attention = (f"{proposed_count} proposed decision entr{'y' if proposed_count == 1 else 'ies'} tracked",)
    return summaries, attention


def _format_decision_triage_line(entry: DecisionEntry, *, as_of: datetime) -> str:
    stale_label = "stale" if assess_proposed_decision_staleness(entry, as_of.date()) else "current"
    return (
        f"{entry.status.value.upper()} | {stale_label} | {entry.title} "
        f"(owner {entry.decided_by}, date {entry.decision_date.isoformat()})"  # type: ignore[union-attr]
    )  # type: ignore[union-attr]


def _build_assumption_triage_lines(
    *,
    program_id: str,
    programs_root: Path,
    as_of: datetime,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    assumptions = tuple(
        a.record
        for a in ProgramReality.load(
            program_id,
            programs_root=programs_root,
        ).assumptions()
    )
    if not assumptions:
        return (), ()

    overdue_ids = {entry.id for entry in check_validation_due(assumptions, as_of.date())}
    summaries = tuple(
        _format_assumption_triage_line(entry, overdue=entry.id in overdue_ids)
        for entry in sorted(
            assumptions,
            key=lambda entry: (
                0 if entry.id in overdue_ids else 1,
                0 if entry.status is AssumptionStatus.UNVALIDATED else 1,
                entry.validation_due or date.max,
                entry.text.lower(),
            ),
        )
    )
    overdue_count = len(overdue_ids)
    unvalidated_count = sum(1 for entry in assumptions if entry.status is AssumptionStatus.UNVALIDATED)
    attention: tuple[str, ...] = ()
    if overdue_count:
        attention = (f"{overdue_count} assumption{' is' if overdue_count == 1 else 's are'} overdue for validation (see ASSUMPTIONS)",)
    elif unvalidated_count:
        attention = (f"{unvalidated_count} unvalidated assumption{' tracked' if unvalidated_count == 1 else 's tracked'}",)
    return summaries, attention


def _format_assumption_triage_line(entry: Assumption, *, overdue: bool) -> str:
    due_label = entry.validation_due.isoformat() if entry.validation_due is not None else "-"
    owner_label = entry.owner_alias or "-"
    recency_label = "overdue" if overdue else "current"
    linked_risk_label = f" | risk {entry.linked_risk_id}" if entry.linked_risk_id is not None else ""
    return (
        f"{entry.status.value.upper()} | {recency_label} | due {due_label} | owner {owner_label} | "
        f"{entry.text}{linked_risk_label}"
    )


def _build_nudge_response_attention(
    *,
    program_id: str,
    programs_root: Path,
    items: tuple[WorkItem, ...],
    as_of: datetime,
) -> tuple[str, ...]:
    _np = get_nudge_paths(program_id, programs_root=programs_root)
    _legacy_state = programs_root / program_id / "nudge_state.json"
    state_path = _np.state_path if _np.state_path.exists() else _legacy_state
    try:
        entries = load_nudge_state(state_path)
    except ConfigError as exc:
        return (f"Nudge response tracking skipped: {exc}",)

    if not entries:
        return ()

    item_by_id = {item.id: item for item in items}
    response_window = timedelta(hours=48)
    attention: list[str] = []
    for entry in entries:
        item = item_by_id.get(entry.work_item_id)
        if item is None:
            continue
        if as_of < entry.nudged_at + response_window:
            continue
        if has_response_since(item, entry.nudged_at):
            continue
        attention.append(
            "Nudged item "
            f"WI:{item.id} has no ADO response 48h after the last nudge "
            f"({entry.nudged_at.date().isoformat()}) - {item.title}"
        )
    return tuple(attention)


def _build_correlated_triage_items(
    *,
    items: tuple[WorkItem, ...],
    approved_signals: tuple[Signal, ...],
    unreviewed_signals: tuple[Signal, ...],
    claims: tuple[ClaimEntry, ...],
    program_id: str,
    programs_root: Path,
    risks: tuple[RiskEntry, ...],
    actions: tuple[ActionItem, ...],
    as_of: datetime,
) -> tuple[CorrelatedTriageItem, ...]:
    item_lookup = {item.id: item for item in items}
    correlated_lines: dict[int, list[str]] = {item.id: [] for item in items}
    correlated_confidence: dict[int, Confidence] = {item.id: Confidence.NONE for item in items}

    for signal in approved_signals:
        for work_item_id in _extract_correlated_work_item_ids(signal.entity_refs):
            if work_item_id in item_lookup:
                correlated_lines[work_item_id].append(
                    f"Signal (approved): {signal.source} | {signal.text}"
                )
                correlated_confidence[work_item_id] = _stronger_confidence(correlated_confidence[work_item_id], signal.confidence)

    for signal in unreviewed_signals:
        for work_item_id in _extract_correlated_work_item_ids(signal.entity_refs):
            if work_item_id in item_lookup:
                correlated_lines[work_item_id].append(
                    f"Signal (needs review): {signal.source} | {signal.text}"
                )
                correlated_confidence[work_item_id] = _stronger_confidence(correlated_confidence[work_item_id], signal.confidence)

    for claim in sorted(claims, key=lambda entry: (entry.issue_number, entry.id)):
        due_label = claim.due_date.isoformat() if claim.due_date is not None else "-"
        for work_item_id in _extract_correlated_work_item_ids(claim.entity_refs):
            if work_item_id in item_lookup:
                correlated_lines[work_item_id].append(
                    f"Claim: issue #{claim.issue_number} | due {due_label} | {claim.text}"
                )
                correlated_confidence[work_item_id] = _stronger_confidence(correlated_confidence[work_item_id], Confidence.HIGH)

    for risk in sorted(
        risks,
        key=lambda entry: (
            0 if entry.status in {RiskStatus.OPEN, RiskStatus.ESCALATED} else 1,
            -compute_risk_score(entry),
            entry.title.lower(),
        ),
    ):
        for work_item_id in risk.linked_work_item_ids:
            if work_item_id in item_lookup:
                correlated_lines[work_item_id].append(
                    f"Risk: {_format_risk_triage_line(risk, as_of=as_of)}"
                )
                correlated_confidence[work_item_id] = _stronger_confidence(correlated_confidence[work_item_id], Confidence.HIGH)

    active_actions = tuple(
        action
        for action in actions
        if action.status in {ActionStatus.PROPOSED, ActionStatus.OPEN, ActionStatus.IN_PROGRESS}
    )
    overdue_ids = {action.id for action in assess_action_staleness(active_actions, as_of.date())}
    resolution_candidate_ids = _load_action_resolution_candidate_ids(
        program_id=program_id,
        programs_root=programs_root,
        actions=active_actions,
    )
    for action in sorted(
        active_actions,
        key=lambda entry: (
            0 if entry.id in overdue_ids else 1,
            entry.due_date or datetime.max.date(),
            entry.created_at,
            entry.text.lower(),
        ),
    ):
        for work_item_id in action.linked_work_item_ids:
            if work_item_id in item_lookup:
                correlated_lines[work_item_id].append(
                    f"Action: {_format_action_triage_line(action, overdue=action.id in overdue_ids, resolution_candidate=action.id in resolution_candidate_ids)}"
                )
                correlated_confidence[work_item_id] = _stronger_confidence(correlated_confidence[work_item_id], Confidence.HIGH)

    correlated_items: list[CorrelatedTriageItem] = []
    for work_item_id in sorted(item_lookup):
        details = tuple(correlated_lines[work_item_id])
        if not details:
            continue
        item = item_lookup[work_item_id]
        correlated_items.append(
            CorrelatedTriageItem(
                work_item_id=item.id,
                work_item_title=item.title,
                work_item_state=item.state,
                details=details,
                confidence=correlated_confidence[work_item_id],
            )
        )
    return tuple(correlated_items)


def _stronger_confidence(current: Confidence, candidate: Confidence) -> Confidence:
    ranking = {
        Confidence.NONE: 0,
        Confidence.LOW: 1,
        Confidence.MEDIUM: 2,
        Confidence.HIGH: 3,
    }
    return candidate if ranking[candidate] > ranking[current] else current


def _extract_correlated_work_item_ids(entity_refs: tuple[str, ...]) -> tuple[int, ...]:
    ids: list[int] = []
    for entity_ref in entity_refs:
        candidate = str(entity_ref).strip()
        if not candidate.upper().startswith("WI:"):
            continue
        try:
            work_item_id = int(candidate.split(":", 1)[1])
        except ValueError:
            continue
        if work_item_id not in ids:
            ids.append(work_item_id)
    return tuple(ids)


def _format_action_triage_line(action: ActionItem, *, overdue: bool, resolution_candidate: bool) -> str:
    due_label = action.due_date.isoformat() if action.due_date is not None else "-"
    overdue_label = "overdue" if overdue else "current"
    candidate_label = " | candidate for resolution" if resolution_candidate else ""
    return (
        f"{action.status.value.upper()} | due {due_label} | {overdue_label} | {action.text} "
        f"(owner {action.owner_alias}){candidate_label}"
    )


def _format_milestone_summary(
    milestone: Milestone,
    assessment: MilestoneAssessment,
    *,
    schedule_summary: str | None = None,
    target_history_summary: str | None = None,
    completion_history_summary: str | None = None,
) -> str:
    blocked_count = len(assessment.blocked_criteria)
    status_label = assessment.computed_health.value.replace("_", " ")
    declared_label = milestone.status.value.replace("_", " ")
    blocked_label = "no blocked signals" if blocked_count == 0 else f"{blocked_count} blocked signal{'s' if blocked_count != 1 else ''}"
    critical_path_label = " | critical path" if assessment.critical_path else ""
    summary = (
        f"{milestone.name} — computed {status_label}, declared {declared_label}; "
        f"{blocked_label}; {round(assessment.slip_probability * 100)}% slip probability{critical_path_label}"
    )
    if schedule_summary:
        summary += f"; {schedule_summary}"
    if target_history_summary:
        summary += f"; {target_history_summary}"
    if completion_history_summary:
        summary += f"; {completion_history_summary}"
    return summary


def _narrative_filename(bundle, section_id: str) -> str:
    if report_command_helpers._is_continuity_layout(bundle):
        return f"chapter_{section_id}.md"
    if bundle.chapter_contract is not None and any(chapter.id == section_id for chapter in bundle.chapter_contract.chapters):
        return f"chapter_{section_id}.md"
    return f"ws_{section_id}.md"
