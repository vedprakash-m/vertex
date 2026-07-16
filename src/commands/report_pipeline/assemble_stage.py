"""Assembly-stage helpers extracted from report.py (WI-6.2).

Contains the core report/lookback assembly functions and all DI-injection
wrapper functions that the stage classes inject into StageContext.
External callers should continue importing from src.commands.report (which
re-exports every name from this module) so no import sites need updating.
"""
from __future__ import annotations

"""Top-level report command facade.

Decomposition note (rev. 323 + 329, D-14 closure)
--------------------------------------------------
Most leaf logic lives in extracted submodules under
`src/commands/report_<concern>.py` (e.g. `report_ai`, `report_continuity`,
`report_detail`, `report_review`, `report_scorecards`, `report_output`,
`report_fetch`, `report_health`, `report_history`, `report_deck`, `report_email`,
`report_cascade`, `report_narratives`, `report_top_items`, `report_snapshot`,
`report_context`, `report_diff`, `report_lookback`, `report_continuity`).

A subset of those submodules is reached through thin `def <name>(...)` wrappers
in this file that import the underlying implementation as `<name>_impl` and
re-inject one or more report.py-local helpers (bare names, computed values, or
lambdas) into the call. This is the `_impl` dependency-injection adapter
pattern: the wrappers exist so the extracted submodule never has to import
`report.py` directly (circular-import avoidance), while the orchestrator can
still hand the submodule its report-local dependencies (e.g.
`_item_trajectory_points`, `_load_report_signal_context`,
`_build_workstream_citations`).

The 21 retained `_impl` aliases are codified as the legitimate DI surface by
`tests/contracts/test_d14_report_impl_adapters.py`. A wrapper that fails to
inject ≥1 module-local value (bare name / computed value / lambda) is by
contract a redundant re-export and must be collapsed to a direct import
(rev. 323 + rev. 329 history). Adding a new `_impl` import without a
DI-injecting wrapper fails the contract test.

The 3 wrappers that were collapsed in rev. 329
(`_ensure_review_status`, `_build_continuity_deltas`,
`_build_detail_workstream_data`) were 1:1 pass-throughs with no DI; they
now bind directly to the report_<concern> module's implementation.

This file is the orchestrator: it routes the CLI, composes the stages, owns
the report-local state shape, and applies overrides. Domain rules live in the
extracted submodules, not here.
"""

import json
import os
import re
import uuid
import webbrowser
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from time import perf_counter
from typing import Any, Callable
from collections.abc import Mapping
import typer

from src.ai.ai_stage import AIStage
from src.ai.ai_mode import AIMode, get_ai_mode
from src.ai._pipeline import AIPipelineError
from src.ai.blurb_generator import generate_workstream_blurb
from src.ai.client import AIClient
from src.ai.client import AIClientError
from src.ai.llm_trace import AITraceContext, use_trace_context
from src.ai.exec_summary_drafter import draft_exec_summary
from src.ai.draft_reviewer import build_review_artifact, render_review_markdown, render_review_summary, review_draft
from src.commands.report_ai import _AIGeneratedSection, _AISynthesisResult, _DraftAIContext
from src.commands.report_ai import _GuardedReviewEvidence
from src.commands.report_ai import _ReportSignalContext
from src.commands.report_ai import _build_draft_readiness
from src.commands.report_ai import _edit_pattern_context_lines
from src.commands.report_ai import _build_newsletter_narrative_covered_item_ids as _build_newsletter_narrative_covered_item_ids_impl
from src.commands.report_ai import _build_newsletter_scoped_items as _build_newsletter_scoped_items_impl
from src.commands.report_ai import _item_trajectory_points
from src.commands.report_ai import _iter_ai_generated_sections as _iter_ai_generated_sections_impl
from src.commands.report_ai import _load_draft_ai_context as _load_draft_ai_context_impl
from src.commands.report_ai import _load_eta_forecasts as _load_eta_forecasts_impl
from src.commands.report_ai import _load_guarded_review_evidence as _load_guarded_review_evidence_impl
from src.commands.report_ai import _load_report_signal_context as _load_report_signal_context_impl
from src.commands.report_ai import _relevant_item_deltas
from src.commands.report_ai import _report_ai_usage
from src.commands.report_ai import _build_report_ai_trace_run_id, _resolve_report_ai_deployments, _REPORT_AI_EXEC_PRIMARY_FALLBACK_ENVS, _REPORT_AI_EXEC_BACKUP_FALLBACK_ENVS
from src.core.trusted_baseline_store import load_trusted_baseline_issue
from src.commands.report_ai import _signal_context_lines
from src.commands.report_ai import _synthesize_v2_ai_content as _synthesize_v2_ai_content_impl
from src.commands.report_health import _build_health_summary
from src.commands.report_health import _build_milestone_health_summary
from src.commands.report_health import _build_risk_register_summary
from src.commands.report_health import _compute_prior_risk_load
from src.commands.report_health import _compute_risk_load
from src.commands.report_health import _compute_trajectory
from src.commands.report_health import _edition_label
from src.commands.report_health import _resolve_health_bluf
from src.commands.report_health import _resolve_leadership_ask
from src.commands.report_output import _to_jsonable
from src.commands.report_output import _ado_item_base_url
from src.commands.report_output import _ado_saved_query_base_url
from src.commands.report_output import _artifact_base_url
from src.commands.report_output import _artifact_url
from src.commands.report_output import _build_item_urls
from src.commands.report_output import _write_report_adaptive_cards as _write_report_adaptive_cards_impl
from src.commands.report_output import _write_output_json
from src.commands.report_output import _write_output_text
from src.commands.report_output import _write_workstream_snapshot_artifacts
from src.commands.report_scorecards import _apply_scorecard_trend_annotation as _apply_scorecard_trend_annotation_impl
from src.commands.report_scorecards import _build_scorecard_data as _build_scorecard_data_impl
from src.commands.report_scorecards import _build_scorecard_packets
from src.commands.report_scorecards import _confidence_rank
from src.commands.report_scorecards import _dimension_sort_rank
from src.commands.report_scorecards import _effective_scorecard_settings
from src.commands.report_scorecards import _is_blocked_dimension
from src.commands.report_scorecards import _is_effective_dimension_override
from src.commands.report_scorecards import _order_dimensions_by_risk
from src.commands.report_scorecards import _resolve_dimension_risk
from src.commands.report_scorecards import _resolve_dimension_summary
from src.commands.report_scorecards import _select_dimension_evidence
from src.commands.report_snapshot import _build_snapshot
from src.commands.report_snapshot import _derive_qg_status
from src.commands.report_snapshot import _format_ban_violation
from src.commands.report_snapshot import _format_edition_title
from src.commands.report_snapshot import _group_scorecard_deltas
from src.commands.report_snapshot import _scorecard_delta_kind
from src.commands.report_review import _default_review_section
from src.commands.report_review import _ensure_review_status
from src.commands.report_review import _merge_review_section
from src.commands.report_review import _skipped_review_sections as _skipped_review_sections_impl
from src.commands.report_diff import build_override_snapshot, build_report_diff_summary
from src.commands.report_cascade import _build_cascade_exec_summary_text
from src.commands.report_cascade import _cascade_exec_summary_entry
from src.commands.report_cascade import _cascade_messages_for_section
from src.commands.report_cascade import _format_dependency_cascade
from src.commands.report_context import _build_model_program_context
from src.commands.report_narratives import _active_workstream_blurbs
from src.commands.report_narratives import _workstream_narrative_warnings
from src.commands.report_top_items import _build_auto_suggested_top_items
from src.commands.report_top_items import _build_top_items
from src.commands.report_top_items import _humanize_anchor
from src.commands.report_top_items import _resolve_forwarding_context
from src.commands.report_top_items import _risk_from_top_item_type
from src.commands.report_top_items import _subject_signal
from src.commands.report_top_items import _top_item_label
from src.commands.report_history import _OfflineSnapshotCache
from src.commands.report_history import _build_draft_ado_diff_lines
from src.commands.report_history import _build_draft_snapshot_from_items
from src.commands.report_history import _build_offline_work_items
from src.commands.report_history import _deserialize_comment
from src.commands.report_history import _deserialize_revision
from src.commands.report_history import _deserialize_work_item
from src.commands.report_history import _find_offline_snapshot_cache
from src.commands.report_history import _format_draft_item_delta
from src.commands.report_history import _load_offline_snapshot_cache
from src.commands.report_history import _load_previous_dry_run_state
from src.commands.report_history import _load_previous_snapshot
from src.commands.report_history import _ordered_item_deltas
from src.commands.report_history import _snapshot_issue_number
from src.commands.report_deck import _build_deck_ask_rows
from src.commands.report_deck import _build_deck_assumption_rows
from src.commands.report_deck import _build_deck_change_rows
from src.commands.report_deck import _build_deck_charter_lines
from src.commands.report_deck import _build_deck_decision_rows
from src.commands.report_deck import _build_deck_issue_rows
from src.commands.report_deck import _build_deck_milestone_rows
from src.commands.report_deck import _build_deck_render_context
from src.commands.report_deck import _build_deck_risk_rows
from src.commands.report_deck import _build_item_delta_lookup
from src.commands.report_deck import _build_report_milestone_rows
from src.commands.report_deck import _count_label
from src.commands.report_deck import _extract_work_item_id
from src.commands.report_deck import _format_deck_assumption_detail
from src.commands.report_deck import _format_deck_closed_ask_row
from src.commands.report_deck import _format_deck_decision_detail
from src.commands.report_deck import _format_deck_delta_counts_summary
from src.commands.report_deck import _format_deck_generated_at
from src.commands.report_deck import _format_deck_issue_date
from src.commands.report_deck import _format_deck_issue_detail
from src.commands.report_deck import _format_deck_item_delta
from src.commands.report_deck import _format_deck_milestone_detail
from src.commands.report_deck import _format_deck_open_ask_row
from src.commands.report_deck import _format_deck_risk_change
from src.commands.report_deck import _format_deck_risk_detail
from src.commands.report_deck import _format_milestone_status_label
from src.commands.report_deck import _sort_deck_assumptions
from src.commands.report_deck import _sort_deck_decisions
from src.commands.report_deck import _truncate_deck_words
from src.commands.report_detail import _build_workstream_templates as _build_workstream_templates_impl
from src.commands.report_detail import _build_detail_workstream_data
from src.commands.report_detail import _detail_section_id
from src.commands.report_detail import _focused_delta_item_ids
from src.commands.report_detail import _hidden_detail_dimensions
from src.commands.report_detail import _iter_detail_sections as _iter_detail_sections_impl
from src.commands.report_detail import _normalize_workstream_blurb
from src.commands.report_detail import _resolve_workstream_blurb
from src.commands.report_detail import _slice_contract_map
from src.commands.report_detail import _visible_detail_section_ids as _visible_detail_section_ids_impl
from src.commands.report_email import _build_draft_email_message, _build_draft_email_sender, _build_email_preheader, _build_email_subject, _build_preview_eml_bytes, _distribution_to, _resolve_email_subject
from src.commands.report_continuity import _continuity_chapter_title
from src.commands.report_continuity import _build_continuity_deltas
from src.commands.report_continuity import _active_chapter_notes, _build_chapter_templates, _build_continuity_exec_summary_template as _build_continuity_exec_summary_template_impl
from src.commands.report_continuity import _build_continuity_render_data as _build_continuity_render_data_impl
from src.commands.report_continuity import _build_continuity_workstream_data as _build_continuity_workstream_data_impl
from src.commands.report_continuity import _build_exec_summary_severe_signal_seeds as _build_exec_summary_severe_signal_seeds_impl
from src.commands.report_continuity import _build_exec_summary_signal_seeds, _build_exec_summary_state_seed, _build_exec_summary_template
from src.commands.report_continuity import _build_exec_summary_trajectory_seed, _dimension_included_in_continuity_membership
from src.commands.report_continuity import _has_usable_continuity_baseline
from src.commands.report_continuity import _is_continuity_layout, _sanitize_scaffold_seed, _visible_continuity_chapters
from src.commands.report_continuity import _build_workstream_citations, _continuity_packet_eta_label, _workstream_significant_findings
from src.commands.report_fetch import _bound_saved_query_wiql, _extract_saved_query_wiql, _infer_risk_level, _load_live_work_items as _load_live_work_items_impl
from src.commands.report_fetch import _load_saved_query_item_ids, _merge_item_ids, _optional_string, _parse_date, _parse_datetime, _parse_identity, _parse_tags, _query_work_item_batch_rows, _raw_identity, _render_saved_query_filter_clause
from src.commands.report_fetch import _slice_contract_explicit_work_item_ids, _slice_contract_saved_query_clauses, _slice_contract_saved_query_ids, _work_item_from_raw, _work_item_from_sources
from src.commands.report_lookback import _build_lookback_assumption_lifecycle, _build_lookback_charter_review, _build_lookback_evidence, _build_lookback_exec_summary, _build_lookback_incident_learning_summary, _build_lookback_items
from src.commands.report_lookback import _build_lookback_ai_retrospective_rows, _build_lookback_retrospective_intelligence, _build_lookback_scorecard_data, _load_lookback_snapshots
from src.commands.report_lookback import build_lookback_ban_list_inputs
from src.commands.stdout import build_compact_manifest, build_verbose_evidence_payload, render_stdout_payload
from src.core.ado_enrichment import ADO_ANALYTICS_HISTORY_FIELDS, ADO_CHILD_BATCH_FIELDS, ADO_RISK_ASSESSMENT_COMMENT_FIELD, ADO_RISK_ASSESSMENT_FIELD, build_analytics_history, build_child_work_items, build_significant_findings, extract_child_ids_by_parent, infer_ado_risk_level, normalize_risk_assessment, serialize_trajectory_points
from src.core.ado_client import ADOClient
from src.core.assumption_tracker import check_validation_due
from src.core.archive_store import archive_integrity_waived, find_archive_index_inconsistencies, get_all_green_streak, read_archive_index, read_vitality_history, verify_archive_integrity
from src.core.attribution_engine import build_inline_citations, build_section_citations
from src.core.ban_list_validator import find_ban_list_violations
from src.core.cascade_detector import DependencyCascade, detect_dependency_cascades
from src.core.chapter_contract_loader import ChapterDefinition, canonical_dimension_binding_id
from src.core.charter import CharterSuccessCriterion, DimensionMaxRiskMetric, ItemCountMaxMetric, normalize_charter_values, parse_charter_success_criteria
from src.core.claim_tracker import load_decision_asks, load_latest_claim_statuses, load_open_decision_asks
from src.core.config_loader import PROGRAMS_ROOT, REPORTS_ROOT, NarrativeProgramContext as NarrativeProgramContext, ReportBundle, ScorecardDimensionSettings, ScorecardSettings, load_bundle, load_bundle_with_mode
from src.core.continuation_contract import build_bridge_section_roster_ids, build_continuation_contract, get_continuation_contract_path, load_inherited_scorecard_dimensions
from src.core.decision_register import assess_proposed_decision_staleness
from src.core.delta_engine import build_deltas
from src.core.edition_resolver import filter_workstreams, get_program_output_dir, resolve_edition
from src.core.eml_writer import write_eml
from src.core.evidence_engine import build_evidence
from src.core.exceptions import AuthError, ConfigError, QueryError, QueryTimeoutError
from src.core.forecast_engine import ETAForecast, ForecastAssessment, build_forecast_assessment
from src.core.freshness_engine import build_freshness_report
from src.core.hygiene_engine import evaluate_hygiene
from src.core.issue_projection import IssueProjection
from src.core.leakage_detector import detect_leakage, load_approved_workiq_signals
from src.core.deck_renderer import DeckAskRow, DeckAssumptionRow, DeckChangeRow, DeckDataRow, DeckDecisionRow, DeckHealthRow, DeckIssueRow, DeckMilestoneRow, DeckRenderContext, DeckRenderer, DeckRiskRow, DeckTopRiskRow
from src.core.html_renderer import HTMLRenderer, RenderContext
from src.core.journal import load_latest_review_decisions, read_signals
from src.core.jinja_filters import build_anchor, configure_ado_web_url, configure_scorecard_labels, risk_label
from src.core.kusto_client import build_live_kusto_query_executor
from src.core.kusto_query_loader import load_kpi_queries
from src.core.kusto_rendering import KustoQueryExecutor, build_kusto_sections
from src.core.manifest_writer import build_run_manifest, write_run_manifest
from src.core.narrative_store import build_workstream_narrative_history
from src.core.notification_state_store import load_latest_notification_state
from src.core.observability import RunLoggerAdapter, configure_file_logging, get_command_trace_path
from src.core.models import AttributionTier, Comment, Confidence, ConfirmedDimension, DeltaKind, DeltaSet, DimensionRisk, EditionType, ItemDelta
from src.core.models import EvidencePacket, FreshnessReport, ProgramContext, ReportData, ReviewSection, ReviewState
from src.core.models import ReviewStatus, Revision, RiskLevel, RunManifest, ScorecardDelta, ScorecardEvidencePacket, Snapshot, SnapshotItem, WorkItem
from src.core.narrative_store import REMOVED_SECTION_MARKER, get_narratives_dir, load_narratives, merge_narratives
from src.core.overrides_store import DecisionStripAck, DimensionOverride, OverridesDocument, ScorecardOverrides, Top3NowEntry
from src.core.overrides_store import apply_pending_overrides, get_overrides_path, load_overrides, merge_overrides
from src.core.pipeline import StageContext, run_pipeline
from src.core.program_fact_store import ProgramFactStore
from src.core.quality_gates import QualityGateReport, combine_gate_reports, evaluate_continuity_gates, evaluate_phase_1a_gates, evaluate_phase_1b_gates, evaluate_phase_1c_gates
from src.core.quality_gates import evaluate_workiq_budget_gate
from src.core.quality_matrix_engine import QualityMatrix, build_quality_matrix, render_quality_matrix_markdown
from src.core.query_builder import build_odata_filter
from src.core.remediation_engine import RemediationReport, build_remediation_report, render_remediation_markdown
from src.core.store_factory import build_signal_store_for_program_id, build_trajectory_store_for_program_id
from src.core.review_status_store import get_review_status_path, load_review_status, save_review_status
from src.core.section_proposal_store import load_stale_claim_ids
from src.core.signal_review import signal_is_approved_for_evidence
from src.core.risk_register_engine import assess_risk_staleness, compute_risk_score
from src.core.scorecard_engine import assign_dimension_items, build_scorecard
from src.core.scorecard_trends import load_scorecard_trends
from src.core.summary_store import load_summary
from src.core.knowledge_store import load_program_knowledge
from src.core.milestone_engine import (
    describe_milestone_schedule_variance,
    load_milestone_completion_date_history_map,
    load_milestone_target_date_history_map,
    summarize_milestone_completion_date_history,
    summarize_milestone_target_date_history,
)
from src.core.vitality_reporting import build_vitality_section, build_vitality_snapshot, effective_vitality_exempt_aliases, vitality_settings_from_program
from src.core.vitality_scorer import aggregate_vitality, score_vitality
from src.core.velocity_metrics import build_velocity_kusto_section
from src.core.slice_contract_loader import SliceContract
from src.core.snapshot_store import ARCHIVE_ROOT, get_archive_root
from src.core.stages.action_stage import ActionStage
from src.core.stages.compute_stage import ComputeStage
from src.core.stages.fetch_stage import FetchStage
from src.core.stages.milestone_stage import MilestoneStage
from src.core.stages.risk_stage import RiskStage
from src.core.stages.narrative_stage import NarrativeStage
from src.core.stages.render_stage import RenderStage
from src.core.stages.resolution_stage import ResolutionStage
from src.core.stages.validation_stage import ValidationStage
from src.core.trajectory_analyzer import analyze_trajectories
from src.core.triage import ReadinessAssessment, StaleNarrativeFinding, detect_stale_narratives
from src.core.teams_renderer import TeamsRenderer
from src.core.telemetry_summary import build_program_telemetry_summary
from src.core.workstream_registry import build_workstream_issue_snapshot
from src.core.verbosity_enforcer import enforce_verbosity
from src.core.view_models import AdoVitalitySectionData, AssumptionLifecycleRow, AssumptionLifecycleSummary, CharterReviewRow, CharterReviewSummary, ContinuityBandCell, ContinuityBandData, ContinuityChapterData, ContinuityChapterRowData
from src.core.view_models import Citation, ContinuityJumpLink, ContinuityRenderData, EditionMeta, HealthSummary, KpiTile, KustoSectionData, MilestoneSummaryRow
from src.core.view_models import RetrospectiveIntelligenceRow, RetrospectiveIntelligenceSummary, ScorecardData, Top3Item, WorkstreamData
from src.core.voice_validator import find_voice_violations
from src.core.models_v2 import Assumption, AssumptionStatus, DecisionEntry, DecisionStatus, Milestone, MilestoneAssessment, MilestoneStatus, RiskEntry, RiskStatus, Signal, Workstream
from src.m365.graph_send_client import GraphMailMessage



@dataclass(frozen=True, slots=True)
class DraftArtifacts:
    issue_number: int
    exit_code: int
    report: ReportData
    snapshot: Snapshot
    manifest: RunManifest
    quality_matrix: QualityMatrix
    remediation_report: RemediationReport
    html_body: str
    markdown_body: str
    eml_path: Path | None
    html_path: Path | None
    md_path: Path | None
    manifest_path: Path | None
    snapshot_path: Path | None
    quality_matrix_md_path: Path | None
    quality_matrix_json_path: Path | None
    remediation_md_path: Path | None
    remediation_json_path: Path | None
    overrides_path: Path
    narratives_dir: Path
    review_status_path: Path
    kusto_sections: tuple[KustoSectionData, ...]
    warnings: tuple[str, ...]
    draft_readiness: ReadinessAssessment | None = None
    email_subject: str = ""
    email_preheader: str = ""
    subject_signal: str = ""
    diff_summary: str | None = None
    adaptive_card_paths: tuple[Path, ...] = ()
    persona_signal_coverage_path: Path | None = None
    workstream_snapshot_md_path: Path | None = None
    workstream_snapshot_json_path: Path | None = None
    workstream_associations_json_path: Path | None = None
    continuation_contract_path: Path | None = None
    title: str = ""


@dataclass(frozen=True, slots=True)
class DraftState:
    issue_number: int
    generated_at: datetime
    ado_data_as_of: datetime
    edition_type: EditionType
    items: tuple[WorkItem, ...]
    workstream_blurbs: dict[str, str] = field(default_factory=dict)
    ai_prompt_versions: dict[str, str] = field(default_factory=dict)
    ai_confidences: dict[str, str] = field(default_factory=dict)
    ai_trace_run_id: str | None = None
    kusto_sections: tuple[KustoSectionData, ...] = ()
    override_snapshot: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    program_fact_snapshot: ProgramFactSnapshotDraftState | None = None
    top_3_now: tuple[str, ...] = ()
    exec_summary_text: str = ""


@dataclass(frozen=True, slots=True)
class ProgramFactSnapshotDraftState:
    snapshot_id: str
    program_id: str
    pinned_recorded_at: datetime | None
    pinned_revision_count: int

def _pin_program_fact_snapshot(
    program_id: str | None,
    *,
    edition_name: str,
    issue_number: int,
    generated_at: datetime,
    db_root: Path | None = None,
) -> ProgramFactSnapshotDraftState | None:
    if program_id is None:
        return None
    store = ProgramFactStore(program_id, db_root=db_root)
    pin = store.pin_snapshot(
        metadata={
            "edition_name": edition_name,
            "issue_number": issue_number,
        },
        created_at=generated_at,
    )
    return ProgramFactSnapshotDraftState(
        snapshot_id=pin.snapshot_id,
        program_id=pin.program_id,
        pinned_recorded_at=pin.pinned_recorded_at,
        pinned_revision_count=pin.pinned_revision_count,
    )


def _runtime_db_root_for_reports(reports_root: Path | None) -> Path | None:
    if reports_root is None:
        return None
    return reports_root.parent / "vertex-db"


_RISK_LOAD_WEIGHTS = {
    RiskLevel.HIGH: 3,
    RiskLevel.MEDIUM: 1,
    RiskLevel.LOW: 0,
    RiskLevel.DONE: 0,
    RiskLevel.UNKNOWN: 2,
}
_HTML_TAG_RE = re.compile(r"<[^>]+>")






def _is_decision_type(item_type: str) -> bool:
    return item_type.strip().lower() in {"decision", "ask"}


def _is_risk_type(item_type: str) -> bool:
    return item_type.strip().lower() in {"decision", "ask", "risk", "watch"}




def _normalize_section_filter_ids(values: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    if not values:
        return ()
    normalized_ids: list[str] = []
    seen: set[str] = set()
    for raw_value in values:
        for part in str(raw_value).split(","):
            section_id = part.strip()
            if not section_id:
                continue
            if section_id.startswith("ws:"):
                section_id = section_id.removeprefix("ws:")
            elif section_id.startswith("ws_") and section_id.endswith(".md"):
                section_id = section_id.removeprefix("ws_").removesuffix(".md")
            if section_id in seen:
                continue
            seen.add(section_id)
            normalized_ids.append(section_id)
    if not normalized_ids:
        raise typer.BadParameter("--sections requires at least one non-empty section id.")
    return tuple(normalized_ids)




def _compute_healthy_streak(
    edition_name: str,
    issue_number: int,
    dimension_risks: tuple[DimensionRisk, ...],
    archive_root: Path,
) -> int:
    if not dimension_risks:
        return 0
    if any(dimension.risk not in {RiskLevel.LOW, RiskLevel.DONE} for dimension in dimension_risks):
        return 0
    return 1 + get_all_green_streak(
        edition_name,
        archive_root=archive_root,
        before_issue_number=issue_number,
    )


def _compute_read_time_minutes(exec_summary_text: str, workstream_blurbs: dict[str, str], edition_type: EditionType) -> int:
    words = len(exec_summary_text.split()) + sum(len(blurb.split()) for blurb in workstream_blurbs.values())
    baseline = max(1, round(words / 250))
    if edition_type == EditionType.DETAILED:
        return max(2, baseline)
    if edition_type == EditionType.FOCUSED:
        return max(2, baseline)
    return baseline




def _format_prior_date_label(snapshot: Snapshot | None) -> str | None:
    if snapshot is None:
        return None
    return snapshot.generated_at.strftime("%b %d")


def _build_v2_vitality_snapshot(
    *,
    resolved_v2,
    items: tuple[WorkItem, ...],
    as_of: datetime,
    programs_root: Path,
) -> tuple[Any, Any]:
    settings = vitality_settings_from_program(resolved_v2.raw_program)
    knowledge = load_program_knowledge(resolved_v2.paths.program_id, programs_root=programs_root)
    exempt_aliases = effective_vitality_exempt_aliases(settings, knowledge.people_directory)
    filtered_workstreams = filter_workstreams(resolved_v2.workstreams, resolved_v2.edition.workstream_filter)
    eligible_items = tuple(
        item
        for item in items
        if _vitality_owner_alias(item) not in exempt_aliases
    )
    trajectory_store = build_trajectory_store_for_program_id(
        resolved_v2.program.id,
        programs_root=programs_root,
    )
    leakage = detect_leakage(
        eligible_items,
        load_approved_workiq_signals(
            resolved_v2.program.id,
            as_of=as_of,
            programs_root=programs_root,
        ),
        trajectory_loader=lambda work_item_id: trajectory_store.read(
            resolved_v2.program.id,
            work_item_id,
        ),
    )
    scores = score_vitality(
        eligible_items,
        as_of=as_of,
        workstream_resolver=lambda item: _resolve_vitality_workstream_id(item.area_path, filtered_workstreams),
        leakage=leakage,
        leakage_signal_threshold=settings.sparse_workiq_threshold,
    )
    workstream_aggregates = aggregate_vitality(
        scores,
        scope_type="workstream",
        leakage_signal_threshold=settings.sparse_workiq_threshold,
    )
    return build_vitality_snapshot(
        scores,
        workstream_aggregates,
        leakage_signal_threshold=settings.sparse_workiq_threshold,
    ), settings


def _resolve_vitality_workstream_id(area_path: str, workstreams: tuple[Any, ...]) -> str | None:
    normalized_area = area_path.lower()
    matches = [
        workstream
        for workstream in workstreams
        if any(normalized_area.startswith(prefix.lower()) for prefix in workstream.area_paths)
    ]
    if not matches:
        return None
    matches.sort(key=lambda workstream: max(len(prefix) for prefix in workstream.area_paths), reverse=True)
    return getattr(matches[0], 'id', None) or matches[0].name


def _vitality_owner_alias(item: WorkItem) -> str | None:
    owner_value = item.assigned_to_email or item.assigned_to
    if owner_value is None:
        return None
    alias = owner_value.strip().lower()
    if not alias:
        return None
    if "@" in alias:
        alias = alias.split("@", 1)[0]
    return alias or None


def _count_new_high_dimensions(scorecard_deltas: tuple[ScorecardDelta, ...]) -> int:
    return sum(
        1
        for delta in scorecard_deltas
        if delta.new_risk in {RiskLevel.BLOCKED, RiskLevel.HIGH} and delta.old_risk not in {RiskLevel.BLOCKED, RiskLevel.HIGH}
    )


def _has_severe_freshness_signals(freshness_report: FreshnessReport) -> bool:
    return any(item.rule_id in {"FR-44", "FR-45", "FR-47"} for item in freshness_report.items)


def _truncate_words(text: str, word_limit: int) -> str:
    words = text.split()
    if len(words) <= word_limit:
        return text.strip()
    return " ".join(words[:word_limit]).rstrip(".,;:") + "."
















def _decision_strip_ack_required(
    top_items: tuple[Top3Item, ...],
    new_high_count: int,
    freshness_report: FreshnessReport,
) -> bool:
    return not top_items and (new_high_count > 0 or _has_severe_freshness_signals(freshness_report))




def _derive_vector_label(risk: RiskLevel, prior_risk: RiskLevel | None, packet: ScorecardEvidencePacket) -> str:
    if risk == RiskLevel.HIGH and prior_risk != RiskLevel.HIGH:
        return "New High"
    if risk == RiskLevel.HIGH and packet.streak_count >= 3:
        return f"High {packet.streak_count}w"
    if prior_risk is not None:
        if _risk_rank(risk) > _risk_rank(prior_risk):
            return f"{risk_label(risk)} (up)"
        if _risk_rank(risk) < _risk_rank(prior_risk):
            return f"{risk_label(risk)} (down)"
    if packet.stale_count and risk in {RiskLevel.MEDIUM, RiskLevel.HIGH}:
        return f"{risk_label(risk)} (stale)"
    return risk_label(risk)


def _risk_rank(level: RiskLevel) -> int:
    return {
        RiskLevel.UNKNOWN: 0,
        RiskLevel.DONE: 1,
        RiskLevel.LOW: 2,
        RiskLevel.MEDIUM: 3,
        RiskLevel.HIGH: 4,
    }[level]


def _spark_char(level: RiskLevel) -> str:
    mapping = {
        RiskLevel.DONE: "▁",
        RiskLevel.LOW: "▂",
        RiskLevel.UNKNOWN: "▅",
        RiskLevel.MEDIUM: "▄",
        RiskLevel.HIGH: "▇",
    }
    return mapping[level]


def _derive_risk_sparkline(risk: RiskLevel, prior_risk: RiskLevel | None) -> tuple[str | None, str | None]:
    if prior_risk is None:
        return None, None
    history = (_spark_char(prior_risk), _spark_char(prior_risk), _spark_char(risk), _spark_char(risk))
    if _risk_rank(risk) > _risk_rank(prior_risk):
        return "".join(history), "rising 4w"
    if _risk_rank(risk) < _risk_rank(prior_risk):
        return "".join(history), "falling 4w"
    if risk == RiskLevel.HIGH:
        return _spark_char(risk) * 4, "chronic high"
    return _spark_char(risk) * 4, "stable"


WorkItemLoader = Callable[[ReportBundle, datetime], tuple[tuple[WorkItem, ...], int]]
def _generate_report_draft_from_context(ctx: StageContext) -> DraftArtifacts:
    if (
        ctx.started_at is None
        or ctx.data_as_of is None
        or ctx.reports_root is None
        or ctx.archive_root is None
        or ctx.programs_root is None
        or ctx.repo_root is None
        or ctx.editions_root is None
        or ctx.programs_root is None
        or ctx.bundle is None
        or ctx.archive_index is None
        or ctx.resolved_issue_number is None
        or ctx.resolved_edition_type is None
    ):
        raise RuntimeError("ResolutionStage must execute before generating a report draft.")

    edition_name = ctx.edition_name
    offline = ctx.offline
    diff_mode = ctx.diff_mode
    lookback_range = ctx.lookback_range
    resolved_reports_root = ctx.reports_root
    resolved_archive_root = ctx.archive_root
    editions_root = ctx.editions_root
    programs_root = ctx.programs_root
    bundle = ctx.bundle
    resolved_v2 = ctx.resolved_v2
    archive_index = ctx.archive_index
    latest_confirmed_entry = ctx.latest_confirmed_entry
    resolved_issue_number = ctx.resolved_issue_number
    previous_dry_run_state = ctx.previous_dry_run_state
    previous_snapshot = ctx.previous_snapshot
    previous_issue_number = ctx.previous_issue_number
    trusted_baseline_issue_number = ctx.trusted_baseline_issue_number
    resolved_edition_type = ctx.resolved_edition_type
    started_at = ctx.started_at
    data_as_of = ctx.data_as_of
    work_item_loader = ctx.work_item_loader
    kusto_query_executor = ctx.kusto_query_executor
    open_browser = ctx.open_browser
    items = ctx.items
    ado_calls = ctx.ado_calls
    offline_source_label = ctx.offline_source_label
    eta_forecasts = ctx.eta_forecasts
    evidence_by_item = ctx.evidence_by_item
    continuity_snapshot = ctx.continuity_snapshot
    continuity_previous_issue_number = ctx.continuity_previous_issue_number
    deltas = ctx.deltas
    overrides_document = ctx.overrides_document
    override_snapshot = ctx.override_snapshot
    top_3_now = ctx.top_3_now
    overrides_path = ctx.overrides_path
    scorecard_packets = ctx.scorecard_packets
    scorecards = ctx.scorecards
    dimension_risks = ctx.dimension_risks
    scorecard_deltas = ctx.scorecard_deltas
    signal_context = ctx.signal_context
    default_exec_summary = ctx.default_exec_summary
    top_items = ctx.top_items
    auto_suggestions = ctx.auto_suggestions
    continuity_chapters = ctx.continuity_chapters
    narratives_dir = ctx.narratives_dir
    loaded_narratives = ctx.loaded_narratives
    visible_section_ids = ctx.visible_section_ids
    loaded_exec_summary_text = ctx.loaded_exec_summary_text
    exec_summary_text = ctx.exec_summary_text
    workstream_blurbs = ctx.workstream_blurbs
    workstream_narrative_history = ctx.workstream_narrative_history
    ai_synthesis = ctx.ai_synthesis
    render_exec_summary_text = ctx.render_exec_summary_text
    render_workstream_blurbs = ctx.render_workstream_blurbs
    render_state = ctx.render_state
    validation_state = ctx.validation_state

    if resolved_edition_type == EditionType.LOOKBACK:
        raise RuntimeError("LookbackStage must execute before generating lookback draft artifacts.")

    # Configure ADO# hyperlink base URL from program config so Jinja filters
    # render clickable links using the program's actual org/project.
    if bundle.config.ado is not None:
        configure_ado_web_url(
            org=bundle.config.ado.organization,
            project=bundle.config.ado.project,
        )
    raw_rendering = (
        ctx.resolved_v2.raw_program.get("rendering")
        if ctx.resolved_v2 is not None and isinstance(ctx.resolved_v2.raw_program, dict)
        else None
    )
    configure_scorecard_labels(raw_rendering.get("scorecard_labels") if isinstance(raw_rendering, dict) else None)

    if ado_calls is None:
        if offline:
            offline_cache = _load_offline_snapshot_cache(
                edition_name=edition_name,
                issue_number=resolved_issue_number,
                archive_root=resolved_archive_root,
            )
            data_as_of = offline_cache.snapshot.ado_data_as_of
            started_at = offline_cache.snapshot.ado_data_as_of
            items = _build_offline_work_items(offline_cache.snapshot)
            ado_calls = 0
            offline_source_label = offline_cache.source_label
        else:
            loader = work_item_loader or _load_live_work_items
            try:
                items, ado_calls = loader(bundle, data_as_of)
            except QueryTimeoutError as error:
                guidance = f"ADO fetch timed out after {bundle.config.ado_fetch_timeout_seconds}s. Run vertex doctor to diagnose."
                cached_snapshot = _find_offline_snapshot_cache(
                    edition_name=edition_name,
                    issue_number=resolved_issue_number,
                    archive_root=resolved_archive_root,
                )
                if cached_snapshot is not None:
                    guidance += (
                        " Re-run with --offline to use cached data"
                        f" (last gathered: {cached_snapshot.snapshot.ado_data_as_of.strftime('%Y-%m-%d %H:%M UTC')})."
                    )
                raise QueryTimeoutError(guidance) from error

    if default_exec_summary is None or evidence_by_item is None:
        eta_forecasts = _load_eta_forecasts(
            edition_name=edition_name,
            items=items,
            as_of=data_as_of,
            reports_root=resolved_reports_root,
        )

        evidence_window_start = data_as_of - timedelta(days=bundle.config.ado.date_window_days)
        evidence_by_item = {
            item.id: build_evidence(item, evidence_window_start, data_as_of)
            for item in items
        }
        continuity_snapshot = previous_snapshot if _has_usable_continuity_baseline(previous_snapshot) else None
        continuity_previous_issue_number = previous_issue_number if continuity_snapshot is not None else None
        deltas = _build_continuity_deltas(
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
        override_snapshot = build_override_snapshot(overrides_document)
        top_3_now = tuple(entry.text.strip() for entry in overrides_document.top_3_now if entry.text.strip())
        overrides_path = get_overrides_path(
            edition_name,
            resolved_reports_root,
            issue_number=resolved_issue_number,
        )

        scorecard_packets = _build_scorecard_packets(
            bundle,
            items,
            continuity_snapshot,
            edition_name=edition_name,
            archive_root=resolved_archive_root,
            trusted_issue_number=trusted_baseline_issue_number,
            overrides_document=overrides_document,
        )
        scorecards, dimension_risks, scorecard_deltas = _build_scorecard_data(
            bundle=bundle,
            items=items,
            evidence_by_item=evidence_by_item,
            scorecard_packets=scorecard_packets,
            overrides_document=overrides_document,
            edition_name=edition_name,
            archive_root=resolved_archive_root,
            trusted_issue_number=trusted_baseline_issue_number,
            reports_root=resolved_reports_root,
        )
        signal_context = _load_report_signal_context(
            edition_name=edition_name,
            bundle=bundle,
            items=items,
            as_of=data_as_of,
            previous_snapshot=previous_snapshot,
            reports_root=resolved_reports_root,
        )
        default_exec_summary = _build_exec_summary_text(
            bundle,
            items,
            dimension_risks,
            deltas,
            dependency_cascades=(signal_context.dependency_cascades if signal_context is not None else ()),
            baseline_available=continuity_snapshot is not None,
        )

    if deltas is None:
        raise RuntimeError("ComputeStage must populate deltas before report generation.")
    if overrides_document is None or override_snapshot is None or overrides_path is None:
        raise RuntimeError("ComputeStage must populate override state before report generation.")
    if scorecard_packets is None or scorecards is None or dimension_risks is None or scorecard_deltas is None:
        raise RuntimeError("ComputeStage must populate scorecard state before report generation.")
    if (
        top_items is None
        or auto_suggestions is None
        or continuity_chapters is None
        or narratives_dir is None
        or loaded_narratives is None
        or visible_section_ids is None
        or loaded_exec_summary_text is None
        or exec_summary_text is None
        or workstream_blurbs is None
        or workstream_narrative_history is None
    ):
        top_items = _build_top_items(overrides_document, scorecards)
        auto_suggestions = _build_auto_suggested_top_items(scorecard_deltas, scorecard_packets)
        continuity_chapters = _visible_continuity_chapters(bundle, resolved_edition_type)
        existing_narratives = load_narratives(edition_name, resolved_issue_number, reports_root=resolved_reports_root)
        section_templates = (
            _build_chapter_templates(resolved_issue_number, continuity_chapters)
            if _is_continuity_layout(bundle)
            else _build_workstream_templates(
                issue_number=resolved_issue_number,
                bundle=bundle,
                items=items,
                scorecards=scorecards,
                scorecard_packets=scorecard_packets,
                overrides_document=overrides_document,
            )
        )
        if not _is_continuity_layout(bundle) and resolved_edition_type == EditionType.DETAILED and bundle.chapter_contract is not None:
            section_templates = {
                **section_templates,
                **_build_chapter_templates(
                    resolved_issue_number,
                    tuple(
                        chapter
                        for chapter in bundle.chapter_contract.chapters_for(resolved_edition_type.value)
                        if not chapter.chapter_exempt
                    ),
                ),
            }
        narrative_templates = {
            "exec_summary.md": (
                _build_continuity_exec_summary_template(
                    issue_number=resolved_issue_number,
                    program_objective=(bundle.program_context.objective if bundle.program_context is not None else None),
                    auto_suggestions=auto_suggestions,
                    scorecard_deltas=scorecard_deltas,
                    dimension_risks=dimension_risks,
                )
                if _is_continuity_layout(bundle)
                else _build_exec_summary_template(
                    resolved_issue_number,
                    layout_mode=bundle.config.layout_mode,
                )
            ),
            **section_templates,
        }
        if _is_continuity_layout(bundle):
            narrative_templates.update(
                {
                    name: content
                    for name, content in existing_narratives.items()
                    if name.startswith("ws_") and name not in narrative_templates
                }
            )
        narratives_dir = merge_narratives(
            edition_name,
            resolved_issue_number,
            templates=narrative_templates,
            reports_root=resolved_reports_root,
        )
        loaded_narratives = load_narratives(edition_name, resolved_issue_number, reports_root=resolved_reports_root)
        loaded_exec_summary_text = loaded_narratives.get("exec_summary.md", "").strip()
        exec_summary_text = loaded_exec_summary_text or default_exec_summary
        if _is_continuity_layout(bundle):
            visible_section_ids = {chapter.id for chapter in continuity_chapters}
            section_roster_current_ids = tuple(sorted(("exec_summary", *visible_section_ids)))
            workstream_blurbs = _active_chapter_notes(loaded_narratives, continuity_chapters)
            for chapter in continuity_chapters:
                workstream_blurbs.setdefault(chapter.id, "")
        else:
            raw_visible_section_ids = _visible_detail_section_ids(
                bundle,
                overrides_document,
                edition_type=resolved_edition_type,
                items=items,
                scorecards=scorecards,
                scorecard_packets=scorecard_packets,
                deltas=deltas,
                scorecard_deltas=scorecard_deltas,
                top_items=top_items,
            )
            visible_section_ids, diagnostic_section_ids = build_bridge_section_roster_ids(
                edition_name=edition_name,
                edition_type=resolved_edition_type,
                trusted_issue=trusted_baseline_issue_number,
                reports_root=resolved_reports_root,
                archive_root=resolved_archive_root,
                current_section_ids=raw_visible_section_ids,
                loaded_narratives=loaded_narratives,
                removed_section_ids=set(overrides_document.removed_sections),
            )
            chapter_surface_chapters = (
                tuple(
                    chapter
                    for chapter in bundle.chapter_contract.chapters_for(resolved_edition_type.value)
                    if not chapter.chapter_exempt
                )
                if resolved_edition_type == EditionType.DETAILED and bundle.chapter_contract is not None
                else ()
            )
            chapter_notes = _active_chapter_notes(loaded_narratives, chapter_surface_chapters)
            for chapter in chapter_surface_chapters:
                chapter_notes.setdefault(chapter.id, "")
            section_roster_current_ids = tuple(sorted(("exec_summary", *diagnostic_section_ids)))
            workstream_blurbs = _active_workstream_blurbs(loaded_narratives, visible_section_ids)
            if chapter_notes:
                visible_section_ids = set(visible_section_ids) | set(chapter_notes)
                section_roster_current_ids = tuple(sorted(("exec_summary", *diagnostic_section_ids, *chapter_notes)))
                workstream_blurbs = {**workstream_blurbs, **chapter_notes}
        workstream_narrative_history = (
            {}
            if _is_continuity_layout(bundle)
            else build_workstream_narrative_history(
                edition=edition_name,
                issue_number=resolved_issue_number,
                workstream_names=tuple(workstream.name for workstream in bundle.program_context.workstreams) if bundle.program_context is not None else (),
                current_workstream_blurbs=workstream_blurbs,
                archive_root=resolved_archive_root,
            )
        )

    if (
        top_items is None
        or auto_suggestions is None
        or continuity_chapters is None
        or narratives_dir is None
        or loaded_narratives is None
        or visible_section_ids is None
        or loaded_exec_summary_text is None
        or exec_summary_text is None
        or workstream_blurbs is None
        or workstream_narrative_history is None
    ):
        raise RuntimeError("NarrativeStage must populate narrative state before report generation.")

    # SP3-2: Run SharePoint evidence extraction on approved signals before blurb synthesis.
    # Converts approved sharepoint/lt_deck journal signals → WorkstreamEvidence in evidence_store.jsonl.
    # Must run before load_approved_evidence_by_lane (called inside _synthesize_v2_ai_content).
    if programs_root is not None and resolved_v2 is not None:
        try:
            from src.commands.gather_pipeline.sharepoint_evidence_stage import run_sharepoint_evidence_stage
            run_sharepoint_evidence_stage(
                program_id=resolved_v2.program.id,
                programs_root=programs_root,
                as_of=data_as_of,
                workstreams=tuple(resolved_v2.workstreams) if resolved_v2.workstreams else (),
            )
        except Exception:
            pass  # non-fatal: SP evidence is optional for report generation

    if ai_synthesis is None or render_exec_summary_text is None or render_workstream_blurbs is None:
        ai_context = None
        if not offline:
            ai_context = _load_draft_ai_context(
                edition_name=edition_name,
                bundle=bundle,
                items=items,
                as_of=data_as_of,
                previous_snapshot=previous_snapshot,
                reports_root=resolved_reports_root,
                signal_context=signal_context,
            )
        ai_synthesis = _synthesize_v2_ai_content(
            bundle=bundle,
            edition_name=edition_name,
            issue_number=resolved_issue_number,
            started_at=started_at,
            edition_type=resolved_edition_type,
            items=items,
            evidence_by_item=evidence_by_item,
            deltas=deltas,
            scorecards=scorecards,
            scorecard_packets=scorecard_packets,
            overrides_document=overrides_document,
            continuity_chapters=continuity_chapters,
            current_exec_summary_text=exec_summary_text,
            loaded_exec_summary_text=loaded_exec_summary_text,
            current_workstream_blurbs=workstream_blurbs,
            visible_section_ids=visible_section_ids,
            ai_program_context=bundle.program_context,
            ai_context=ai_context,
        )
        render_exec_summary_text = ai_synthesis.exec_summary_text
        render_workstream_blurbs = ai_synthesis.workstream_blurbs

    if ai_synthesis is None or render_exec_summary_text is None or render_workstream_blurbs is None:
        raise RuntimeError("AIStage must populate AI synthesis state before report generation.")
    if render_state is None:
        staged_render_ctx = replace(
            ctx,
            ai_synthesis=ai_synthesis,
            render_exec_summary_text=render_exec_summary_text,
            render_workstream_blurbs=render_workstream_blurbs,
        )
        render_state = RenderStage().execute(staged_render_ctx).render_state
    if render_state is None:
        raise RuntimeError("RenderStage must populate render state before validation.")

    freshness_report = render_state.freshness_report
    review_status = render_state.review_status
    review_status_path = render_state.review_status_path
    report = render_state.report
    workstream_data = render_state.workstream_data
    narrative_warnings = render_state.narrative_warnings
    forecast = render_state.forecast
    kusto_sections = render_state.kusto_sections
    kusto_warnings = render_state.kusto_warnings
    quality_matrix = render_state.quality_matrix
    remediation_report = render_state.remediation_report
    exec_summary_citations = render_state.exec_summary_citations
    title = render_state.title
    subject_signal = render_state.subject_signal
    email_subject = render_state.email_subject
    email_preheader = render_state.email_preheader
    html_body = render_state.html_body
    markdown_body = render_state.markdown_body
    snapshot = render_state.snapshot
    rendered_strings = render_state.rendered_strings
    if validation_state is None:
        staged_validation_ctx = replace(
            ctx,
            ai_synthesis=ai_synthesis,
            render_exec_summary_text=render_exec_summary_text,
            render_workstream_blurbs=render_workstream_blurbs,
            render_state=render_state,
        )
        validation_state = ValidationStage().execute(staged_validation_ctx).validation_state
    if validation_state is None:
        raise RuntimeError("ValidationStage must populate validation state before output generation.")

    report = validation_state.report
    final_manifest = validation_state.manifest
    warnings = validation_state.warnings
    draft_readiness = validation_state.draft_readiness
    exit_code = validation_state.exit_code
    diff_summary = None
    if diff_mode and previous_dry_run_state is not None and evidence_by_item is not None:
        ado_lines = _build_draft_ado_diff_lines(
            previous_dry_run_state=previous_dry_run_state,
            current_items=items,
            current_evidence_by_item=evidence_by_item,
            current_issue_number=resolved_issue_number,
            current_data_as_of=data_as_of,
            current_edition_type=resolved_edition_type,
        )
        diff_summary = build_report_diff_summary(
            previous_dry_run_state=previous_dry_run_state,
            current_issue_number=resolved_issue_number,
            current_override_snapshot=override_snapshot,
            current_top_3_now=top_3_now,
            current_exec_summary_text=render_exec_summary_text,
            ado_lines=ado_lines,
        )

    html_path: Path | None = None
    md_path: Path | None = None
    manifest_path: Path | None = None
    snapshot_path: Path | None = None
    output_dir = get_program_output_dir(edition_name, programs_root=programs_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    issue_dir = output_dir / f"issue_{resolved_issue_number:03d}"
    quality_matrix_md_path = _write_output_text(
        issue_dir / f"issue_{resolved_issue_number:03d}.quality_matrix.md",
        render_quality_matrix_markdown(quality_matrix),
    )
    quality_matrix_json_path = _write_output_json(
        issue_dir / f"issue_{resolved_issue_number:03d}.quality_matrix.json",
        quality_matrix,
    )
    remediation_md_path = _write_output_text(
        issue_dir / f"issue_{resolved_issue_number:03d}.remediation.md",
        render_remediation_markdown(remediation_report),
    )
    remediation_json_path = _write_output_json(
        issue_dir / f"issue_{resolved_issue_number:03d}.remediation.json",
        remediation_report,
    )
    workstream_snapshot_md_path, workstream_snapshot_json_path, workstream_associations_json_path = _write_workstream_snapshot_artifacts(
        program_id=(resolved_v2.program.id if resolved_v2 is not None else None),
        bundle=bundle,
        issue_number=resolved_issue_number,
        edition_name=edition_name,
        generated_at=started_at,
        quality_matrix=quality_matrix,
        markdown_body=markdown_body,
        items=items,
        output_dir=issue_dir,
        programs_root=programs_root,
    )
    eml_path: Path | None = None
    if resolved_edition_type == EditionType.DECK:
        md_path = _write_output_text(issue_dir / f"issue_{resolved_issue_number:03d}.deck.md", markdown_body)
    else:
        eml_path = write_eml(
            issue_dir / f"issue_{resolved_issue_number:03d}.eml",
            eml_bytes=_build_preview_eml_bytes(
                bundle,
                issue_number=resolved_issue_number,
                as_of=data_as_of,
                html_body=html_body,
                markdown_body=markdown_body,
                suggested_subject=email_subject,
                generated_at=started_at,
            ),
        )
        html_path = _write_output_text(issue_dir / f"issue_{resolved_issue_number:03d}.html", html_body)
        md_path = _write_output_text(issue_dir / f"issue_{resolved_issue_number:03d}.md", markdown_body)
    snapshot_path = _write_output_json(issue_dir / f"issue_{resolved_issue_number:03d}.snapshot.json", snapshot)
    continuation_contract = build_continuation_contract(
        edition_name=edition_name,
        issue_number=resolved_issue_number,
        started_at=started_at,
        reports_root=resolved_reports_root,
        archive_root=resolved_archive_root,
        editions_root=editions_root,
        programs_root=programs_root,
        overrides_document=overrides_document,
        workstream_data=workstream_data,
        output_dir=output_dir,
        current_scorecard_dimensions=tuple(
            sorted(
                (scorecard.name, dimension.name)
                for scorecard in bundle.config.scorecards
                for dimension in scorecard.dimensions
            )
        ),
        current_section_ids=section_roster_current_ids,
    )
    continuation_contract_path = (
        _write_output_json(
            get_continuation_contract_path(output_dir, resolved_issue_number),
            continuation_contract,
        )
        if continuation_contract is not None
        else None
    )
    draft_program_id = resolved_v2.program.id if resolved_v2 is not None else None
    _write_output_json(
        issue_dir / f"issue_{resolved_issue_number:03d}.draft.json",
        DraftState(
            issue_number=resolved_issue_number,
            generated_at=started_at,
            ado_data_as_of=data_as_of,
            edition_type=resolved_edition_type,
            items=items,
            workstream_blurbs=render_workstream_blurbs,
            ai_prompt_versions=dict(ai_synthesis.prompt_versions),
            ai_confidences=dict(ai_synthesis.ai_confidences),
            ai_trace_run_id=ai_synthesis.trace_run_id,
            kusto_sections=kusto_sections,
            override_snapshot=override_snapshot,
            program_fact_snapshot=_pin_program_fact_snapshot(
                draft_program_id,
                edition_name=edition_name,
                issue_number=resolved_issue_number,
                generated_at=started_at,
                db_root=_runtime_db_root_for_reports(resolved_reports_root),
            ),
            top_3_now=top_3_now,
            exec_summary_text=render_exec_summary_text,
        ),
    )
    adaptive_card_paths = _write_report_adaptive_cards(
        bundle=bundle,
        edition_name=edition_name,
        issue_number=resolved_issue_number,
        edition_type=resolved_edition_type,
        report=report,
        programs_root=programs_root,
        report_html_path=html_path,
    )
    manifest_path = write_run_manifest(
        edition_name,
        resolved_issue_number,
        final_manifest,
        programs_root=programs_root,
    )
    if open_browser and html_path is not None:
        webbrowser.open(html_path.resolve().as_uri())

    return DraftArtifacts(
        issue_number=resolved_issue_number,
        exit_code=exit_code,
        report=report,
        snapshot=snapshot,
        manifest=final_manifest,
        quality_matrix=quality_matrix,
        remediation_report=remediation_report,
        html_body=html_body,
        markdown_body=markdown_body,
        eml_path=eml_path,
        html_path=html_path,
        md_path=md_path,
        manifest_path=manifest_path,
        snapshot_path=snapshot_path,
        quality_matrix_md_path=quality_matrix_md_path,
        quality_matrix_json_path=quality_matrix_json_path,
        remediation_md_path=remediation_md_path,
        remediation_json_path=remediation_json_path,
        overrides_path=overrides_path,
        narratives_dir=narratives_dir,
        review_status_path=review_status_path,
        kusto_sections=kusto_sections,
        warnings=warnings,
        draft_readiness=draft_readiness,
        email_subject=email_subject,
        email_preheader=email_preheader,
        subject_signal=subject_signal,
        diff_summary=diff_summary,
        adaptive_card_paths=adaptive_card_paths,
        workstream_snapshot_md_path=workstream_snapshot_md_path,
        workstream_snapshot_json_path=workstream_snapshot_json_path,
        workstream_associations_json_path=workstream_associations_json_path,
        continuation_contract_path=continuation_contract_path,
        title=title,
    )

def _write_report_adaptive_cards(
    *,
    bundle: ReportBundle,
    edition_name: str,
    issue_number: int,
    edition_type: EditionType,
    report: ReportData,
    programs_root: Path = PROGRAMS_ROOT,
    report_html_path: Path | None,
) -> tuple[Path, ...]:
    program_output_dir = get_program_output_dir(edition_name, programs_root=programs_root)
    return _write_report_adaptive_cards_impl(
        bundle=bundle,
        edition_name=edition_name,
        issue_number=issue_number,
        edition_type=edition_type,
        report=report,
        output_root=program_output_dir,
        item_urls=_build_item_urls(bundle, report.items),
        report_html_url=(
            _artifact_url(bundle, output_root=program_output_dir, artifact_path=report_html_path)
            if report_html_path is not None
            else None
        ),
    )

def _generate_lookback_draft(
    *,
    edition_name: str,
    bundle: ReportBundle,
    archive_index: Any,
    resolved_issue_number: int,
    previous_dry_run_state: dict[str, Any] | None,
    started_at: datetime,
    data_as_of: datetime,
    lookback_range: int | None,
    diff_mode: bool,
    reports_root: Path,
    archive_root: Path,
    open_browser: bool,
) -> DraftArtifacts:
    if lookback_range is not None and lookback_range < 2:
        raise ValueError("lookback_range must be at least 2 when edition type is lookback.")

    snapshots = _load_lookback_snapshots(
        archive_index=archive_index,
        archive_root=archive_root,
        as_of=data_as_of,
        lookback_range=lookback_range,
        lookback_days=bundle.config.ado.date_window_days,
    )
    if len(snapshots) < 2:
        # Bootstrap fallback: use the sibling weekly edition when the lookback archive is new.
        weekly_edition = edition_name.replace("_quarterly", "_weekly").replace("_lookback", "_weekly")
        if weekly_edition != edition_name:
            weekly_index = read_archive_index(weekly_edition, archive_root=archive_root)
            snapshots = _load_lookback_snapshots(
                archive_index=weekly_index,
                archive_root=archive_root,
                as_of=data_as_of,
                lookback_range=lookback_range,
                lookback_days=bundle.config.ado.date_window_days,
            )
    if len(snapshots) < 2:
        raise ValueError(
            "Lookback edition requires at least two confirmed archived issues "
            f"(checked '{edition_name}' and sibling weekly archive)."
        )

    baseline_snapshot = snapshots[0]
    latest_snapshot = snapshots[-1]
    expected_scorecards = {
        scorecard.name: tuple(dimension.name for dimension in scorecard.dimensions)
        for scorecard in bundle.config.scorecards
    }
    overrides_document, _ = merge_overrides(
        issue_number=resolved_issue_number,
        expected_scorecards=expected_scorecards,
        existing=load_overrides(edition_name, reports_root=reports_root, issue_number=resolved_issue_number),
    )
    override_snapshot = build_override_snapshot(overrides_document)
    top_3_now = tuple(entry.text.strip() for entry in overrides_document.top_3_now if entry.text.strip())
    overrides_path = get_overrides_path(
        edition_name,
        reports_root,
        issue_number=resolved_issue_number,
    )
    items = _build_lookback_items(snapshots, fetched_at=data_as_of)
    evidence_by_item = {
        item.id: _build_lookback_evidence(
            work_item_id=item.id,
            summary=f"Archived issue history synthesized for lookback item #{item.id}.",
        )
        for item in items
    }
    deltas = build_deltas(
        current_items=items,
        previous_snapshot=baseline_snapshot,
        issue_number=resolved_issue_number,
        previous_issue_number=baseline_snapshot.issue_number,
        evidence_by_item=evidence_by_item,
    )

    scorecards, dimension_risks, scorecard_deltas, grouped_scorecard_deltas, scorecard_packets, scorecard_urls = _build_lookback_scorecard_data(
        snapshots,
        scorecard_delta_kind=_scorecard_delta_kind,
    )
    lookback_healthy_streak = 0
    if dimension_risks and all(dimension.risk in {RiskLevel.LOW, RiskLevel.DONE} for dimension in dimension_risks):
        lookback_healthy_streak = sum(
            1
            for snapshot in snapshots
            if snapshot.scorecards and all(dimension.risk in {RiskLevel.LOW, RiskLevel.DONE} for dimension in snapshot.scorecards)
        )
    health = _build_health_summary(
        dimension_risks,
        baseline_snapshot,
        healthy_streak=lookback_healthy_streak,
    )
    default_exec_summary = _build_lookback_exec_summary(
        snapshots=snapshots,
        items=items,
        deltas=deltas,
        scorecard_deltas=scorecard_deltas,
    )
    narratives_dir = merge_narratives(
        edition_name,
        resolved_issue_number,
        templates={"exec_summary.md": default_exec_summary},
        reports_root=reports_root,
    )
    loaded_narratives = load_narratives(edition_name, resolved_issue_number, reports_root=reports_root)
    exec_summary_text = loaded_narratives.get("exec_summary.md", default_exec_summary).strip()
    review_status = _ensure_review_status(
        edition_name=edition_name,
        issue_number=resolved_issue_number,
        workstream_section_ids=(),
        skipped_section_ids=set(),
        reports_root=reports_root,
    )
    review_status_path = get_review_status_path(edition_name, reports_root=reports_root)

    report = ReportData(
        issue_number=resolved_issue_number,
        edition=EditionType.LOOKBACK,
        generated_at=started_at,
        ado_data_as_of=data_as_of,
        program=_build_model_program_context(bundle),
        items=items,
        deltas=deltas,
        scorecard=dimension_risks,
        scorecard_deltas=scorecard_deltas,
        exec_summary_text=exec_summary_text,
        workstream_blurbs={},
        freshness=FreshnessReport(issue_number=resolved_issue_number, items=(), blocks=0, warns=0, infos=0),
        hygiene_warnings=(),
        review_status=review_status,
        manifest_id=uuid.uuid4().hex,
    )
    quality_matrix = build_quality_matrix(
        bundle=bundle,
        issue_number=resolved_issue_number,
        generated_at=started_at,
        current_items=items,
        previous_issue_number=baseline_snapshot.issue_number,
        ado_query_base_url=_ado_saved_query_base_url(bundle),
    )
    remediation_report = build_remediation_report(quality_matrix)

    item_urls = _build_item_urls(bundle, items)
    resolved_edition_type = EditionType.LOOKBACK
    continuity_chapters: tuple[ChapterDefinition, ...] = ()
    visible_section_ids: set[str] = set()
    provisional_qg_status = _derive_qg_status(has_blockers=False, has_warnings=False)
    edition_meta = EditionMeta(
        edition=edition_name,
        issue_number=resolved_issue_number,
        generated_at=started_at,
        ado_data_as_of=data_as_of,
        manifest_id=report.manifest_id,
        qg_status=provisional_qg_status,
    )
    title = _format_edition_title(bundle, resolved_issue_number, data_as_of)
    subtitle = f"Issue {resolved_issue_number:03d} retrospective"
    preheader = (
        f"Retrospective across {len(snapshots)} confirmed issues ending with Issue {latest_snapshot.issue_number:03d}"
    )
    programs_root = reports_root.parent / "programs"
    resolved_edition = resolve_edition(
        edition_name,
        programs_root=programs_root,
    )
    assumption_lifecycle = _build_lookback_assumption_lifecycle(
        program_id=resolved_edition.program.id if resolved_edition is not None else "",
        snapshots=snapshots,
        as_of=data_as_of,
        programs_root=programs_root,
        edition_name=edition_name,
        archive_root=archive_root,
    )
    charter_review = _build_lookback_charter_review(
        raw_program=resolved_edition.raw_program if resolved_edition is not None else {},
        snapshots=snapshots,
        risk_rank=_risk_rank,
    )
    incident_learning = _build_lookback_incident_learning_summary(
        program_id=resolved_edition.program.id if resolved_edition is not None else "",
        programs_root=programs_root,
        snapshots=snapshots,
    )
    retrospective_intelligence = _build_lookback_retrospective_intelligence(
        program_id=resolved_edition.program.id if resolved_edition is not None else "",
        edition_id=edition_name,
        programs_root=programs_root,
        snapshots=snapshots,
        items=items,
        scorecard_deltas=scorecard_deltas,
        charter_review=charter_review,
    )
    lookback_ai_calls = 0
    lookback_ai_cost_usd = 0.0
    lookback_trace_run_id: str | None = None
    if bundle.config.ai.enabled and retrospective_intelligence is not None:
        exec_deployments = _resolve_report_ai_deployments(
            feature_name="exec_summary_drafter",
            primary=bundle.config.ai.exec_summary_deployment,
            backup=bundle.config.ai.exec_summary_backup_deployment,
            primary_fallback_envs=_REPORT_AI_EXEC_PRIMARY_FALLBACK_ENVS,
            backup_fallback_envs=_REPORT_AI_EXEC_BACKUP_FALLBACK_ENVS,
        )
        if exec_deployments:
            try:
                ai_client = _create_ai_client(
                    deployment=exec_deployments[0],
                    temperature=(bundle.config.ai.temperature or 0.2),
                    budget_usd=bundle.config.ai.budget_usd_per_run,
                    trace_context=None,
                )
                ai_rows = _build_lookback_ai_retrospective_rows(
                    client=ai_client,
                    retrospective_intelligence=retrospective_intelligence,
                    snapshots=snapshots,
                )
                if ai_rows:
                    retrospective_intelligence = replace(
                        retrospective_intelligence,
                        rows=retrospective_intelligence.rows + ai_rows,
                    )
                lookback_ai_calls = int(getattr(getattr(ai_client, "usage_stats", None), "call_count", 0) or 0)
                lookback_ai_cost_usd = float(getattr(ai_client, "spent_usd", 0.0) or 0.0)
                lookback_trace_run_id = _build_report_ai_trace_run_id(
                    edition_name=edition_name,
                    issue_number=resolved_issue_number,
                    started_at=started_at,
                )
            except (AIPipelineError, AuthError, ConfigError, QueryError, QueryTimeoutError, TypeError, ValueError):
                # Retrospective AI synthesis is a best-effort enhancement for lookback editions.
                # Fail closed to the deterministic retrospective rows rather than blocking report generation.
                pass
    render_context = RenderContext(
        title=title,
        subtitle=subtitle,
        preheader=preheader,
        report=report,
        edition_meta=edition_meta,
        health=health,
        top_items=(),
        scorecards=scorecards,
        kusto_sections=(),
        workstreams=(),
        charter_review=charter_review,
        assumption_lifecycle=assumption_lifecycle,
        incident_learning=incident_learning,
        retrospective_intelligence=retrospective_intelligence,
        exec_summary_citations=(),
        sections=(),
        template_contract=None,
        prior_date_label=_format_prior_date_label(baseline_snapshot),
        changes_url=None,
        item_urls=item_urls,
        scorecard_packets=scorecard_packets,
        scorecard_deltas=grouped_scorecard_deltas,
        scorecard_urls=scorecard_urls,
        workstream_urls={},
        mobile_safe_scorecards=bundle.config.mobile_safe_scorecards,
        type_scale_v2=bundle.config.type_scale_v2,
    )

    html_body = HTMLRenderer(edition_name, reports_root=reports_root).render(render_context)
    markdown_body = TeamsRenderer(edition_name, reports_root=reports_root).render(render_context)
    snapshot = _build_snapshot(report, scorecard_packets)

    rendered_strings, location_profiles = build_lookback_ban_list_inputs(
        html_body=html_body,
        markdown_body=markdown_body,
        exec_summary_text=exec_summary_text,
        incident_learning=incident_learning,
    )
    ban_violations = find_ban_list_violations(
        rendered_strings,
        bundle.editorial_rules,
        location_profiles=location_profiles,
    )
    voice_violations = find_voice_violations(
        editorial_rules=bundle.editorial_rules,
        edition_name=edition_name,
        exec_summary_text=exec_summary_text,
        workstream_blurbs={},
        program_context=bundle.program_context,
    )
    verbosity_violations = enforce_verbosity(
        workstream_blurbs={},
        exec_summary_text=exec_summary_text,
        scorecard_summaries={dimension.name: dimension.summary for dimension in dimension_risks},
        subject_line=title,
        verbosity=bundle.editorial_rules.verbosity,
        edition_type=report.edition,
    )
    hygiene_warnings = evaluate_hygiene(
        workstream_blurbs={},
        workstream_citations={},
        exec_summary_text=exec_summary_text,
        exec_summary_citations=(),
        scorecard=dimension_risks,
    )
    report = ReportData(
        issue_number=report.issue_number,
        edition=report.edition,
        generated_at=report.generated_at,
        ado_data_as_of=report.ado_data_as_of,
        program=report.program,
        items=report.items,
        deltas=report.deltas,
        scorecard=report.scorecard,
        scorecard_deltas=report.scorecard_deltas,
        exec_summary_text=report.exec_summary_text,
        workstream_blurbs=report.workstream_blurbs,
        freshness=report.freshness,
        hygiene_warnings=hygiene_warnings,
        review_status=report.review_status,
        manifest_id=report.manifest_id,
    )

    provisional_manifest = build_run_manifest(
        manifest_id=report.manifest_id,
        issue_number=resolved_issue_number,
        edition=edition_name,
        started_at=started_at,
        ended_at=datetime.now(timezone.utc),
        config_payload=bundle.config,
        snapshot=snapshot,
        html_content=html_body,
        markdown_content=markdown_body,
        ado_calls=0,
        ai_calls=0,
        ai_cost_usd=0.0,
        freshness_summary={"blocks": 0, "warns": 0, "infos": 0},
        qg_results={},
        git_sha=_read_git_sha(),
    )
    qg_phase_1a = evaluate_phase_1a_gates(
        ban_list_violations=ban_violations,
        verbosity_violations=verbosity_violations,
        manifest=provisional_manifest,
        expected_snapshot_hash=provisional_manifest.snapshot_hash,
        dimension_risks=dimension_risks,
        program_id=resolved_edition.program.id if resolved_edition is not None else None,
        edition_name=edition_name,
        issue_number=resolved_issue_number,
        archive_root=archive_root,
        programs_root=programs_root,
    )
    journal_signals: tuple[Signal, ...] = ()
    approved_signals: tuple[Signal, ...] = ()
    if resolved_edition is not None:
        signal_store = build_signal_store_for_program_id(
            resolved_edition.program.id,
            programs_root=programs_root,
        )
        journal_signals = signal_store.read(
            resolved_edition.program.id,
            end=data_as_of,
        )
        evidence_window_start = data_as_of - timedelta(days=bundle.config.ado.date_window_days)
        review_states = signal_store.read_reviews(resolved_edition.program.id)
        approved_signals = tuple(
            signal
            for signal in journal_signals
            if signal.timestamp >= evidence_window_start
            and signal_is_approved_for_evidence(signal, review_states)
        )
    newsletter_items = _build_newsletter_scoped_items(
        bundle=bundle,
        edition_type=resolved_edition_type,
        items=items,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        overrides_document=overrides_document,
        continuity_chapters=continuity_chapters,
        visible_section_ids=visible_section_ids,
    )
    qg_phase_1b = evaluate_phase_1b_gates(
        freshness_report=report.freshness,
        items=items,
        publishable_item_ids=tuple(item.id for item in newsletter_items),
        as_of=data_as_of,
        deltas=deltas,
        edition_name=edition_name,
        issue_number=resolved_issue_number,
        workstream_blurbs=report.workstream_blurbs,
        program_context=bundle.program_context,
        dimension_risks=dimension_risks,
        overrides_document=overrides_document,
        approved_signals=approved_signals,
        narratives=loaded_narratives,
        journal_signals=journal_signals,
        program_id=(resolved_edition.program.id if resolved_edition is not None else None),
        program_maturity_level=(resolved_edition.program.maturity_level if resolved_edition is not None else 0),
        workstreams=(resolved_edition.workstreams if resolved_edition is not None else ()),
        scorecards=(resolved_edition.scorecards if resolved_edition is not None else ()),
        archive_root=archive_root,
        programs_root=programs_root,
        stale_claim_ids=(
            load_stale_claim_ids(resolved_edition.program.id, resolved_issue_number, programs_root=programs_root)
            if resolved_edition is not None
            else ()
        ),
    )
    qg_phase_1c = evaluate_phase_1c_gates(
        hygiene_warnings=hygiene_warnings,
        review_status=review_status,
        review_required=bundle.review.required,
        archive_inconsistencies=find_archive_index_inconsistencies(edition_name, archive_root=archive_root),
    )
    qg_reports = [qg_phase_1a, qg_phase_1b, qg_phase_1c]
    # P4-4 (spec §14.1): QG-WIQ-4 — WorkIQ/AI run-cost info gate (report surface).
    # Surfaces a note when spend exceeds 80% of the configured per-run budget; never blocks.
    qg_reports.append(
        QualityGateReport(
            results=(
                evaluate_workiq_budget_gate(
                    cost_usd=lookback_ai_cost_usd,
                    budget_usd_per_run=(bundle.config.ai.budget_usd_per_run if bundle.config.ai.enabled else 0.0),
                ),
            )
        )
    )
    if _is_continuity_layout(bundle):
        qg_reports.append(
            evaluate_continuity_gates(
                html_content=html_body,
                issue_number=resolved_issue_number,
            )
        )
    qg_report = combine_gate_reports(*qg_reports)
    final_manifest = build_run_manifest(
        manifest_id=report.manifest_id,
        issue_number=resolved_issue_number,
        edition=edition_name,
        started_at=started_at,
        ended_at=datetime.now(timezone.utc),
        config_payload=bundle.config,
        snapshot=snapshot,
        html_content=html_body,
        markdown_content=markdown_body,
        ado_calls=0,
        ai_calls=lookback_ai_calls,
        ai_cost_usd=lookback_ai_cost_usd,
        freshness_summary={"blocks": 0, "warns": 0, "infos": 0},
        qg_results=qg_report.qg_results,
        git_sha=_read_git_sha(),
        metadata={"ai_safety": {"trace_run_id": lookback_trace_run_id}} if lookback_trace_run_id is not None else None,
    )

    blocking_warnings = (
        tuple(_format_ban_violation(violation) for violation in ban_violations)
        + tuple(f"{violation.location}: {violation.message}" for violation in verbosity_violations)
        + tuple(f"{violation.location}: {violation.message}" for violation in voice_violations)
    )
    gate_warnings = tuple(
        result.message
        for result in qg_report.failing_results
        if result.gate_id not in {"QG-4", "QG-5"}
    )
    non_blocking_warnings = tuple(dict.fromkeys(tuple(hygiene_warnings) + gate_warnings))
    has_blockers = bool(blocking_warnings)
    has_warnings = bool(non_blocking_warnings)
    diff_summary = None
    if diff_mode and previous_dry_run_state is not None:
        ado_lines = _build_draft_ado_diff_lines(
            previous_dry_run_state=previous_dry_run_state,
            current_items=items,
            current_evidence_by_item=evidence_by_item,
            current_issue_number=resolved_issue_number,
            current_data_as_of=data_as_of,
            current_edition_type=EditionType.LOOKBACK,
        )
        diff_summary = build_report_diff_summary(
            previous_dry_run_state=previous_dry_run_state,
            current_issue_number=resolved_issue_number,
            current_override_snapshot=override_snapshot,
            current_top_3_now=top_3_now,
            current_exec_summary_text=exec_summary_text,
            ado_lines=ado_lines,
        )

    html_path: Path | None = None
    md_path: Path | None = None
    manifest_path: Path | None = None
    snapshot_path: Path | None = None
    output_dir = get_program_output_dir(edition_name, programs_root=programs_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    issue_dir = output_dir / f"issue_{resolved_issue_number:03d}"
    quality_matrix_md_path = _write_output_text(
        issue_dir / f"issue_{resolved_issue_number:03d}.quality_matrix.md",
        render_quality_matrix_markdown(quality_matrix),
    )
    quality_matrix_json_path = _write_output_json(
        issue_dir / f"issue_{resolved_issue_number:03d}.quality_matrix.json",
        quality_matrix,
    )
    remediation_md_path = _write_output_text(
        issue_dir / f"issue_{resolved_issue_number:03d}.remediation.md",
        render_remediation_markdown(remediation_report),
    )
    remediation_json_path = _write_output_json(
        issue_dir / f"issue_{resolved_issue_number:03d}.remediation.json",
        remediation_report,
    )
    workstream_snapshot_md_path, workstream_snapshot_json_path, workstream_associations_json_path = _write_workstream_snapshot_artifacts(
        program_id=(resolved_edition.program.id if resolved_edition is not None else None),
        bundle=bundle,
        issue_number=resolved_issue_number,
        edition_name=edition_name,
        generated_at=started_at,
        quality_matrix=quality_matrix,
        markdown_body=markdown_body,
        items=items,
        output_dir=issue_dir,
        programs_root=programs_root,
    )
    eml_path = write_eml(
        issue_dir / f"issue_{resolved_issue_number:03d}.eml",
        eml_bytes=_build_preview_eml_bytes(
            bundle,
            issue_number=resolved_issue_number,
            as_of=data_as_of,
            html_body=html_body,
            markdown_body=markdown_body,
            suggested_subject="",
            generated_at=started_at,
        ),
    )
    html_path = _write_output_text(issue_dir / f"issue_{resolved_issue_number:03d}.html", html_body)
    md_path = _write_output_text(issue_dir / f"issue_{resolved_issue_number:03d}.md", markdown_body)
    snapshot_path = _write_output_json(issue_dir / f"issue_{resolved_issue_number:03d}.snapshot.json", snapshot)
    draft_program_id = resolved_edition.program.id if resolved_edition is not None else None
    _write_output_json(
        issue_dir / f"issue_{resolved_issue_number:03d}.draft.json",
        DraftState(
            issue_number=resolved_issue_number,
            generated_at=started_at,
            ado_data_as_of=data_as_of,
            edition_type=EditionType.LOOKBACK,
            items=items,
            ai_prompt_versions={},
            ai_confidences={},
            ai_trace_run_id=lookback_trace_run_id,
            kusto_sections=(),
            override_snapshot=override_snapshot,
            program_fact_snapshot=_pin_program_fact_snapshot(
                draft_program_id,
                edition_name=edition_name,
                issue_number=resolved_issue_number,
                generated_at=started_at,
                db_root=_runtime_db_root_for_reports(reports_root),
            ),
            top_3_now=top_3_now,
            exec_summary_text=exec_summary_text,
        ),
    )
    manifest_path = write_run_manifest(
        edition_name,
        resolved_issue_number,
        final_manifest,
    )
    if open_browser and html_path is not None:
        webbrowser.open(html_path.resolve().as_uri())

    warning_exit_code = 3 if has_blockers else 2 if has_warnings else 0
    exit_code = max(warning_exit_code, 0 if qg_report.passed else qg_report.exit_code)
    return DraftArtifacts(
        issue_number=resolved_issue_number,
        exit_code=exit_code,
        report=report,
        snapshot=snapshot,
        manifest=final_manifest,
        quality_matrix=quality_matrix,
        remediation_report=remediation_report,
        html_body=html_body,
        markdown_body=markdown_body,
        eml_path=eml_path,
        html_path=html_path,
        md_path=md_path,
        manifest_path=manifest_path,
        snapshot_path=snapshot_path,
        quality_matrix_md_path=quality_matrix_md_path,
        quality_matrix_json_path=quality_matrix_json_path,
        remediation_md_path=remediation_md_path,
        remediation_json_path=remediation_json_path,
        overrides_path=overrides_path,
        narratives_dir=narratives_dir,
        review_status_path=review_status_path,
        kusto_sections=(),
        warnings=blocking_warnings + non_blocking_warnings,
        draft_readiness=None,
        diff_summary=diff_summary,
        workstream_snapshot_md_path=workstream_snapshot_md_path,
        workstream_snapshot_json_path=workstream_snapshot_json_path,
        workstream_associations_json_path=workstream_associations_json_path,
        title=_format_edition_title(bundle, resolved_issue_number, data_as_of),
    )

def _load_eta_forecasts(
    *,
    edition_name: str,
    items: tuple[WorkItem, ...],
    as_of: datetime,
    reports_root: Path,
) -> dict[int, ETAForecast]:
    return _load_eta_forecasts_impl(
        edition_name=edition_name,
        items=items,
        as_of=as_of,
        reports_root=reports_root,
        item_trajectory_points=_item_trajectory_points,
    )


def _load_draft_ai_context(
    *,
    edition_name: str,
    bundle: ReportBundle,
    items: tuple[WorkItem, ...],
    as_of: datetime,
    previous_snapshot: Snapshot | None,
    reports_root: Path,
    signal_context: _ReportSignalContext | None = None,
) -> _DraftAIContext | None:
    return _load_draft_ai_context_impl(
        edition_name=edition_name,
        bundle=bundle,
        items=items,
        as_of=as_of,
        previous_snapshot=previous_snapshot,
        reports_root=reports_root,
        signal_context=signal_context,
        load_report_signal_context=_load_report_signal_context,
    )


def _load_report_signal_context(
    *,
    edition_name: str,
    bundle: ReportBundle,
    items: tuple[WorkItem, ...],
    as_of: datetime,
    previous_snapshot: Snapshot | None,
    reports_root: Path,
) -> _ReportSignalContext | None:
    return _load_report_signal_context_impl(
        edition_name=edition_name,
        bundle=bundle,
        items=items,
        as_of=as_of,
        previous_snapshot=previous_snapshot,
        reports_root=reports_root,
        item_trajectory_points=_item_trajectory_points,
    )




def _load_guarded_review_evidence(
    *,
    edition_name: str,
    bundle: ReportBundle,
    items: tuple[WorkItem, ...],
    as_of: datetime,
    previous_snapshot: Snapshot | None,
    reports_root: Path,
) -> _GuardedReviewEvidence:
    return _load_guarded_review_evidence_impl(
        edition_name=edition_name,
        bundle=bundle,
        items=items,
        as_of=as_of,
        previous_snapshot=previous_snapshot,
        reports_root=reports_root,
        load_report_signal_context=_load_report_signal_context,
    )


def _synthesize_v2_ai_content(
    *,
    bundle: ReportBundle,
    edition_name: str,
    issue_number: int,
    started_at: datetime,
    edition_type: EditionType,
    items: tuple[WorkItem, ...],
    evidence_by_item: dict[int, EvidencePacket],
    deltas: DeltaSet,
    scorecards: tuple[ScorecardData, ...],
    scorecard_packets: dict[str, dict[str, ScorecardEvidencePacket]],
    overrides_document: OverridesDocument,
    continuity_chapters: tuple[ChapterDefinition, ...],
    current_exec_summary_text: str,
    loaded_exec_summary_text: str,
    current_workstream_blurbs: dict[str, str],
    visible_section_ids: set[str] | tuple[str, ...],
    ai_program_context: NarrativeProgramContext | None,
    ai_context: _DraftAIContext | None,
) -> _AISynthesisResult:
    return _synthesize_v2_ai_content_impl(
        bundle=bundle,
        edition_name=edition_name,
        issue_number=issue_number,
        started_at=started_at,
        edition_type=edition_type,
        items=items,
        evidence_by_item=evidence_by_item,
        deltas=deltas,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        overrides_document=overrides_document,
        continuity_chapters=continuity_chapters,
        current_exec_summary_text=current_exec_summary_text,
        loaded_exec_summary_text=loaded_exec_summary_text,
        current_workstream_blurbs=current_workstream_blurbs,
        visible_section_ids=visible_section_ids,
        ai_program_context=ai_program_context,
        ai_context=ai_context,
        create_ai_client=_create_ai_client,
        draft_exec_summary_runner=draft_exec_summary,
        generate_workstream_blurb_runner=generate_workstream_blurb,
        iter_ai_generated_sections=_iter_ai_generated_sections,
        relevant_item_deltas=_relevant_item_deltas,
    )


def _build_disabled_ai_synthesis_result(
    *,
    current_exec_summary_text: str,
    current_workstream_blurbs: dict[str, str],
) -> _AISynthesisResult:
    return _AISynthesisResult(
        exec_summary_text=current_exec_summary_text,
        workstream_blurbs=dict(current_workstream_blurbs),
        warnings=("AI synthesis disabled by --no-ai / AIMode.DISABLED.",),
    )


def _iter_ai_generated_sections(
    *,
    bundle: ReportBundle,
    edition_type: EditionType,
    items: tuple[WorkItem, ...],
    scorecards: tuple[ScorecardData, ...],
    scorecard_packets: dict[str, dict[str, ScorecardEvidencePacket]],
    overrides_document: OverridesDocument,
    continuity_chapters: tuple[ChapterDefinition, ...],
) -> tuple[_AIGeneratedSection, ...]:
    return _iter_ai_generated_sections_impl(
        bundle=bundle,
        edition_type=edition_type,
        items=items,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        overrides_document=overrides_document,
        continuity_chapters=continuity_chapters,
        iter_detail_sections=_iter_detail_sections,
    )


def _build_newsletter_scoped_items(
    *,
    bundle: ReportBundle,
    edition_type: EditionType,
    items: tuple[WorkItem, ...],
    scorecards: tuple[ScorecardData, ...],
    scorecard_packets: dict[str, dict[str, ScorecardEvidencePacket]],
    overrides_document: OverridesDocument,
    continuity_chapters: tuple[ChapterDefinition, ...],
    visible_section_ids: set[str] | tuple[str, ...],
) -> tuple[WorkItem, ...]:
    return _build_newsletter_scoped_items_impl(
        bundle=bundle,
        edition_type=edition_type,
        items=items,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        overrides_document=overrides_document,
        continuity_chapters=continuity_chapters,
        visible_section_ids=visible_section_ids,
        iter_ai_generated_sections=_iter_ai_generated_sections,
    )


def _build_newsletter_narrative_covered_item_ids(
    *,
    bundle: ReportBundle,
    edition_type: EditionType,
    items: tuple[WorkItem, ...],
    scorecards: tuple[ScorecardData, ...],
    scorecard_packets: dict[str, dict[str, ScorecardEvidencePacket]],
    overrides_document: OverridesDocument,
    continuity_chapters: tuple[ChapterDefinition, ...],
    visible_section_ids: set[str] | tuple[str, ...],
    loaded_narratives: Mapping[str, str],
) -> tuple[int, ...]:
    return _build_newsletter_narrative_covered_item_ids_impl(
        bundle=bundle,
        edition_type=edition_type,
        items=items,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        overrides_document=overrides_document,
        continuity_chapters=continuity_chapters,
        visible_section_ids=visible_section_ids,
        loaded_narratives=loaded_narratives,
        iter_ai_generated_sections=_iter_ai_generated_sections,
    )










def _create_ai_client(
    *,
    deployment: str,
    temperature: float,
    budget_usd: float,
    trace_context: AITraceContext | None = None,
) -> AIClient:
    if get_ai_mode() == AIMode.DISABLED:
        raise AIClientError("AI execution is disabled for this invocation.")
    # D-20: bind the trace context to the process-level ContextVar so any
    # nested helper (rate-limit scope, cost-guard construction, trace-file
    # write path) that doesn't take an explicit `trace_context=` arg still
    # picks it up. The explicit kwarg below still wins, so this is
    # behavior-preserving.
    with use_trace_context(trace_context):
        return AIClient(
            deployment=deployment,
            temperature=temperature,
            budget_usd=budget_usd,
            trace_context=trace_context,
        )


































def _load_live_work_items(bundle: ReportBundle, as_of: datetime) -> tuple[tuple[WorkItem, ...], int]:
    return _load_live_work_items_impl(
        bundle,
        as_of,
        ado_client_factory=ADOClient,
        slice_contract_saved_query_ids=_slice_contract_saved_query_ids,
        slice_contract_saved_query_clauses=_slice_contract_saved_query_clauses,
        slice_contract_explicit_work_item_ids=_slice_contract_explicit_work_item_ids,
        query_work_item_batch_rows=_query_work_item_batch_rows,
        work_item_from_sources=_work_item_from_sources,
    )












def _build_scorecard_data(
    bundle: ReportBundle,
    items: tuple[WorkItem, ...],
    evidence_by_item: dict[int, EvidencePacket],
    scorecard_packets: dict[str, dict[str, Any]],
    overrides_document: OverridesDocument,
    *,
    edition_name: str | None = None,
    archive_root: Path = ARCHIVE_ROOT,
    trusted_issue_number: int | None = None,
    reports_root: Path = REPORTS_ROOT,
) -> tuple[tuple[ScorecardData, ...], tuple[DimensionRisk, ...], tuple[ScorecardDelta, ...]]:
    resolved_edition = None
    if edition_name is not None:
        resolved_edition = resolve_edition(
            edition_name,
            programs_root=reports_root.parent / "programs",
        )
    return _build_scorecard_data_impl(
        bundle,
        items,
        evidence_by_item,
        scorecard_packets,
        overrides_document,
        edition_name=edition_name,
        archive_root=archive_root,
        trusted_issue_number=trusted_issue_number,
        program_id=(resolved_edition.program.id if resolved_edition is not None else None),
        programs_root=reports_root.parent / "programs",
        raw_program=(resolved_edition.raw_program if resolved_edition is not None else None),
        resolved_scorecards=(resolved_edition.scorecards if resolved_edition is not None else ()),
        derive_vector_label=_derive_vector_label,
        derive_risk_sparkline=_derive_risk_sparkline,
        scorecard_delta_kind=_scorecard_delta_kind,
        risk_rank=_risk_rank,
    )
















def _apply_scorecard_trend_annotation(
    summary: str,
    risk: RiskLevel,
    packet: ScorecardEvidencePacket,
) -> str:
    return _apply_scorecard_trend_annotation_impl(summary, risk, packet, risk_rank=_risk_rank)
















def _build_exec_summary_text(
    bundle: ReportBundle,
    items: tuple[WorkItem, ...],
    dimension_risks: tuple[DimensionRisk, ...],
    deltas: Any,
    *,
    dependency_cascades: tuple[DependencyCascade, ...],
    baseline_available: bool,
) -> str:
    high_count = sum(1 for dimension in dimension_risks if dimension.risk == RiskLevel.HIGH)
    medium_count = sum(1 for dimension in dimension_risks if dimension.risk == RiskLevel.MEDIUM)
    if not baseline_available:
        summary = (
            f"{bundle.config.edition.name} tracks {len(items)} in-scope items. "
            f"{high_count} dimensions are High and {medium_count} are Medium. "
            "Current summary is based on current-state inventory; prior-issue deltas are unavailable."
        )
    else:
        summary = (
        f"{bundle.config.edition.name} tracks {len(items)} in-scope items. "
        f"{high_count} dimensions are High and {medium_count} are Medium. "
        f"Changes include {len(deltas.new_items)} new items and {len(deltas.closed_items)} closed items."
    )
    cascade_summary = _build_cascade_exec_summary_text(dependency_cascades)
    if not cascade_summary:
        return summary
    return f"{summary} {cascade_summary}"













def _build_exec_summary_severe_signal_seeds(auto_suggestions: tuple[Top3Item, ...]) -> tuple[str, ...]:
    return _build_exec_summary_severe_signal_seeds_impl(
        auto_suggestions,
        is_decision_type=_is_decision_type,
        is_risk_type=_is_risk_type,
    )


def _build_continuity_exec_summary_template(
    *,
    issue_number: int,
    program_objective: str | None,
    auto_suggestions: tuple[Top3Item, ...],
    scorecard_deltas: tuple[ScorecardDelta, ...],
    dimension_risks: tuple[DimensionRisk, ...],
) -> str:
    return _build_continuity_exec_summary_template_impl(
        issue_number=issue_number,
        program_objective=program_objective,
        auto_suggestions=auto_suggestions,
        scorecard_deltas=scorecard_deltas,
        dimension_risks=dimension_risks,
        is_decision_type=_is_decision_type,
        is_risk_type=_is_risk_type,
    )


def _build_workstream_templates(
    issue_number: int,
    bundle: ReportBundle,
    items: tuple[WorkItem, ...],
    scorecards: tuple[ScorecardData, ...],
    scorecard_packets: dict[str, dict[str, ScorecardEvidencePacket]],
    overrides_document: OverridesDocument,
) -> dict[str, str]:
    return _build_workstream_templates_impl(
        issue_number=issue_number,
        bundle=bundle,
        items=items,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        overrides_document=overrides_document,
        assign_dimension_items=assign_dimension_items,
        ado_query_base_url=_ado_saved_query_base_url(bundle),
        slice_contracts=_slice_contract_map(bundle),
        dimension_sort_rank=_dimension_sort_rank,
    )








def _visible_detail_section_ids(
    bundle: ReportBundle,
    overrides_document: OverridesDocument,
    *,
    edition_type: EditionType | str | None = None,
    items: tuple[WorkItem, ...] = (),
    scorecards: tuple[ScorecardData, ...] = (),
    scorecard_packets: dict[str, dict[str, ScorecardEvidencePacket]] | None = None,
    deltas: DeltaSet | None = None,
    scorecard_deltas: tuple[ScorecardDelta, ...] = (),
    top_items: tuple[Top3Item, ...] = (),
) -> set[str]:
    return _visible_detail_section_ids_impl(
        bundle=bundle,
        overrides_document=overrides_document,
        edition_type=edition_type,
        items=items,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        deltas=deltas,
        scorecard_deltas=scorecard_deltas,
        top_items=top_items,
        assign_dimension_items=assign_dimension_items,
        ado_query_base_url=_ado_saved_query_base_url(bundle),
        slice_contracts=_slice_contract_map(bundle),
    )


def _iter_detail_sections(
    *,
    bundle: ReportBundle,
    items: tuple[WorkItem, ...],
    scorecards: tuple[ScorecardData, ...],
    scorecard_packets: dict[str, dict[str, ScorecardEvidencePacket]],
    overrides_document: OverridesDocument,
) -> tuple[tuple[str, str, DimensionRisk, ScorecardEvidencePacket, tuple[WorkItem, ...]], ...]:
    return _iter_detail_sections_impl(
        bundle=bundle,
        items=items,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        overrides_document=overrides_document,
        assign_dimension_items=assign_dimension_items,
        ado_query_base_url=_ado_saved_query_base_url(bundle),
        slice_contracts=_slice_contract_map(bundle),
        dimension_sort_rank=_dimension_sort_rank,
    )


def _skipped_review_sections(
    *,
    bundle: ReportBundle,
    items: tuple[WorkItem, ...],
    scorecards: tuple[ScorecardData, ...],
    scorecard_packets: dict[str, dict[str, ScorecardEvidencePacket]],
    overrides_document: OverridesDocument,
    deltas: DeltaSet,
    freshness_report: FreshnessReport,
    top_items: tuple[Top3Item, ...],
    previous_snapshot: Snapshot | None,
) -> set[str]:
    return _skipped_review_sections_impl(
        bundle=bundle,
        items=items,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        overrides_document=overrides_document,
        deltas=deltas,
        freshness_report=freshness_report,
        top_items=top_items,
        previous_snapshot=previous_snapshot,
        iter_detail_sections=_iter_detail_sections,
    )


















def _build_workstream_data(
    issue_number: int,
    bundle: ReportBundle,
    edition_type: EditionType | None,
    items: tuple[WorkItem, ...],
    scorecards: tuple[ScorecardData, ...],
    scorecard_packets: dict[str, dict[str, ScorecardEvidencePacket]],
    overrides_document: OverridesDocument,
    workstream_blurbs: dict[str, str],
    dependency_cascades: tuple[DependencyCascade, ...],
    review_status: ReviewStatus,
    evidence_by_item: dict[int, EvidencePacket],
    item_urls: dict[int, str],
    eta_forecasts: dict[int, ETAForecast] | None = None,
    approved_signals: tuple[Signal, ...] = (),
    workstreams: tuple[Workstream, ...] = (),
    program_id: str | None = None,
    programs_root: Path = PROGRAMS_ROOT,
    source_footnotes: dict[str, str] | None = None,
) -> tuple[WorkstreamData, ...]:
    resolved_edition_type = edition_type or EditionType.from_string(bundle.config.edition.type)
    resolved_eta_forecasts = eta_forecasts or {}
    resolved_source_footnotes = source_footnotes or {}
    if _is_continuity_layout(bundle):
        return _attach_kpi_tiles_to_workstreams(
            _build_continuity_workstream_data(
                issue_number=issue_number,
                bundle=bundle,
                edition_type=resolved_edition_type,
                scorecards=scorecards,
                scorecard_packets=scorecard_packets,
                overrides_document=overrides_document,
                workstream_blurbs=workstream_blurbs,
                dependency_cascades=dependency_cascades,
                review_status=review_status,
                evidence_by_item=evidence_by_item,
                item_urls=item_urls,
                items=items,
                eta_forecasts=resolved_eta_forecasts,
                source_footnotes=resolved_source_footnotes,
            ),
            approved_signals=approved_signals,
            workstreams=workstreams,
            program_id=program_id,
            programs_root=programs_root,
        )
    review_lookup = {section.section_id: section.state for section in review_status.sections}
    if bundle.chapter_contract is not None:
        chapter_surface_chapters = tuple(
            chapter
            for chapter in bundle.chapter_contract.chapters_for(resolved_edition_type.value)
            if chapter.id in workstream_blurbs
        )
        if chapter_surface_chapters:
            chapter_workstreams = _build_continuity_workstream_data(
                issue_number=issue_number,
                bundle=bundle,
                edition_type=resolved_edition_type,
                scorecards=scorecards,
                scorecard_packets=scorecard_packets,
                overrides_document=overrides_document,
                workstream_blurbs=workstream_blurbs,
                dependency_cascades=dependency_cascades,
                review_status=review_status,
                evidence_by_item=evidence_by_item,
                item_urls=item_urls,
                items=items,
                chapters=chapter_surface_chapters,
                eta_forecasts=resolved_eta_forecasts,
                source_footnotes=resolved_source_footnotes,
            )
            chapter_dependency_cascades: dict[str, tuple[str, ...]] = {}
            for chapter in chapter_surface_chapters:
                cascade_messages: list[str] = []
                for dimension_id in chapter.dimensions:
                    binding = bundle.chapter_contract.resolve_dimension(dimension_id)
                    if binding is None:
                        continue
                    for message in _cascade_messages_for_section(binding[0], binding[1], dependency_cascades):
                        if message not in cascade_messages:
                            cascade_messages.append(message)
                chapter_dependency_cascades[chapter.id] = tuple(cascade_messages)
            hybrid_workstreams = tuple(
                replace(
                    workstream,
                    dependency_cascades=chapter_dependency_cascades.get(workstream.section_id, ()),
                )
                for workstream in chapter_workstreams
            )
            if resolved_edition_type == EditionType.DETAILED:
                configured_queries = load_kpi_queries(program_id, programs_root=programs_root) if program_id is not None else ()
                existing_chapter_ids = {workstream.section_id for workstream in hybrid_workstreams}
                for chapter in chapter_surface_chapters:
                    if chapter.id in existing_chapter_ids:
                        continue
                    if not any(query.chapter == chapter.id for query in configured_queries):
                        continue
                    chapter_note = workstream_blurbs.get(chapter.id, "").strip()
                    hybrid_workstreams = hybrid_workstreams + (
                        WorkstreamData(
                            section_id=chapter.id,
                            title=_continuity_chapter_title(chapter, overrides_document),
                            blurb=chapter_note,
                            dependency_cascades=chapter_dependency_cascades.get(chapter.id, ()),
                            items=(),
                            citations=(),
                            review_state=review_lookup.get(f"ws:{chapter.id}", ReviewState.PENDING),
                            risk=RiskLevel.UNKNOWN,
                            summary=chapter_note,
                            total_items=0,
                            blocked_count=0,
                            overdue_count=0,
                            unowned_count=0,
                            edit_path=f"narratives/issue_{issue_number:03d}/chapter_{chapter.id}.md",
                            edit_line=1,
                            narrative_empty=(chapter.id in workstream_blurbs and not chapter_note),
                            source_footnote=resolved_source_footnotes.get(chapter.id),
                        ),
                    )
                chapter_dimension_ids = {
                    dimension_id
                    for chapter in chapter_surface_chapters
                    for dimension_id in chapter.dimensions
                }
                detail_sections = tuple(
                    section
                    for section in _iter_detail_sections(
                        bundle=bundle,
                        items=items,
                        scorecards=scorecards,
                        scorecard_packets=scorecard_packets,
                        overrides_document=overrides_document,
                    )
                    if canonical_dimension_binding_id(section[0], section[2].name) not in chapter_dimension_ids
                )
                if detail_sections:
                    hybrid_workstreams = hybrid_workstreams + _build_detail_workstream_data(
                        issue_number=issue_number,
                        detail_sections=detail_sections,
                        workstream_blurbs=workstream_blurbs,
                        dependency_cascades=dependency_cascades,
                        review_lookup=review_lookup,
                        evidence_by_item=evidence_by_item,
                        build_workstream_citations=lambda workstream_items, evidence: _build_workstream_citations(
                            workstream_items,
                            evidence,
                            ado_base_url=_ado_item_base_url(bundle),
                        ),
                        workstream_significant_findings=_workstream_significant_findings,
                        cascade_messages_for_section=_cascade_messages_for_section,
                        continuity_packet_eta_label=_continuity_packet_eta_label,
                        eta_forecasts=resolved_eta_forecasts,
                        source_footnotes=resolved_source_footnotes,
                    )
            return _attach_kpi_tiles_to_workstreams(
                hybrid_workstreams,
                approved_signals=approved_signals,
                workstreams=workstreams,
                program_id=program_id,
                programs_root=programs_root,
            )
    del item_urls
    detail_sections = _iter_detail_sections(
        bundle=bundle,
        items=items,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        overrides_document=overrides_document,
    )
    return _attach_kpi_tiles_to_workstreams(
        _build_detail_workstream_data(
            issue_number=issue_number,
            detail_sections=detail_sections,
            workstream_blurbs=workstream_blurbs,
            dependency_cascades=dependency_cascades,
            review_lookup=review_lookup,
            evidence_by_item=evidence_by_item,
            build_workstream_citations=lambda workstream_items, evidence: _build_workstream_citations(
                workstream_items,
                evidence,
                ado_base_url=_ado_item_base_url(bundle),
            ),
            workstream_significant_findings=_workstream_significant_findings,
            cascade_messages_for_section=_cascade_messages_for_section,
            continuity_packet_eta_label=_continuity_packet_eta_label,
            eta_forecasts=resolved_eta_forecasts,
            source_footnotes=resolved_source_footnotes,
        ),
        approved_signals=approved_signals,
        workstreams=workstreams,
        program_id=program_id,
        programs_root=programs_root,
    )


def _attach_kpi_tiles_to_workstreams(
    workstream_data: tuple[WorkstreamData, ...],
    *,
    approved_signals: tuple[Signal, ...],
    workstreams: tuple[Workstream, ...],
    program_id: str | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[WorkstreamData, ...]:
    if not workstreams:
        return workstream_data

    configured_queries = load_kpi_queries(program_id, programs_root=programs_root) if program_id is not None else ()
    kpi_signals = tuple(signal for signal in approved_signals if signal.source == "kusto_kpi")
    if not kpi_signals and not configured_queries:
        return workstream_data

    return tuple(
        replace(
            workstream,
            kpi_tiles=_kpi_tiles_for_section(
                workstream,
                approved_signals=kpi_signals,
                workstreams=workstreams,
                configured_queries=configured_queries,
            ),
        )
        for workstream in workstream_data
    )


def _kpi_tiles_for_section(
    workstream: WorkstreamData,
    *,
    approved_signals: tuple[Signal, ...],
    workstreams: tuple[Workstream, ...],
    configured_queries: tuple[Any, ...],
) -> tuple[KpiTile, ...]:
    workstream_id = _section_workstream_id(workstream, workstreams=workstreams)
    chapter_query_ids = {
        query.id
        for query in configured_queries
        if query.chapter == workstream.section_id
    }
    if workstream_id is None and not chapter_query_ids:
        return ()

    latest_by_query_id: dict[str, Signal] = {}
    for signal in approved_signals:
        if signal.metadata is None:
            continue
        query_id = signal.metadata.get("query_id")
        if not isinstance(query_id, str) or not query_id.strip():
            continue
        if workstream_id is not None:
            if signal.workstream_id != workstream_id:
                continue
        elif query_id not in chapter_query_ids:
            continue
        existing = latest_by_query_id.get(query_id)
        if existing is None or signal.timestamp > existing.timestamp:
            latest_by_query_id[query_id] = signal

    configured_for_workstream = (
        tuple(query for query in configured_queries if workstream_id in query.workstream_ids)
        if workstream_id is not None
        else tuple(query for query in configured_queries if query.id in chapter_query_ids)
    )
    if configured_for_workstream:
        tiles: list[KpiTile] = []
        configured_ids = {query.id for query in configured_for_workstream}
        for query in configured_for_workstream:
            latest_signal = latest_by_query_id.get(query.id)
            tiles.append(_kpi_tile_from_signal(latest_signal, query=query) if latest_signal is not None else _kpi_tile_from_query(query))
        extra_signals = sorted(
            ((query_id, signal) for query_id, signal in latest_by_query_id.items() if query_id not in configured_ids),
            key=lambda entry: entry[1].timestamp,
            reverse=True,
        )
        tiles.extend(_kpi_tile_from_signal(signal) for _, signal in extra_signals)
        return tuple(tiles)

    return tuple(
        _kpi_tile_from_signal(signal)
        for _, signal in sorted(latest_by_query_id.items(), key=lambda entry: entry[1].timestamp, reverse=True)
    )


def _section_workstream_id(workstream: WorkstreamData, *, workstreams: tuple[Workstream, ...]) -> str | None:
    resolved_ids = {
        resolved_id
        for item in workstream.items
        for resolved_id in (_resolve_vitality_workstream_id(item.area_path, workstreams),)
        if resolved_id is not None
    }
    if len(resolved_ids) == 1:
        return next(iter(resolved_ids))
    if not resolved_ids:
        for candidate in workstreams:
            if workstream.section_id == candidate.id or workstream.section_id == build_anchor(candidate.name):
                return candidate.id
    return None


def _kpi_tile_from_signal(signal: Signal, *, query: Any | None = None) -> KpiTile:
    metadata = signal.metadata or {}
    label: str | None = metadata.get("label") if isinstance(metadata.get("label"), str) and metadata.get("label") else None
    query_id = str(metadata.get("query_id") or (query.id if query is not None else signal.id))
    value = metadata.get("result_value")
    value_text = str(value) if value is not None else "n/a"
    result_payload = _signal_result_payload(metadata)
    fallback_label = (
        str(query.label) if query is not None and query.label
        else str(query.section) if query is not None
        else query_id
    )
    return KpiTile(
        query_id=query_id,
        label=label or fallback_label,
        value=value_text,
        unit=None,
        trend=None,
        confidence=signal.confidence.value,
        as_of=signal.timestamp,
        source_signal_id=signal.id,
        render_mode=query.render_as if query is not None else "metric_highlight",
        validated=bool(metadata.get("validated", query.validated if query is not None else True)),
        refresh_on_gather=query.refresh_on_gather if query is not None else False,
        owner_alias=query.owner_alias if query is not None else None,
        reference_url=query.reference_url if query is not None else None,
        catalog_source=query.catalog_source if query is not None else None,
        result_payload=result_payload,
    )


def _kpi_tile_from_query(query: Any) -> KpiTile:
    return KpiTile(
        query_id=query.id,
        label=query.label or query.section or query.id,
        value="",
        unit=None,
        trend=None,
        confidence=query.confidence,
        as_of=None,
        source_signal_id=None,
        render_mode=query.render_as,
        validated=query.validated,
        refresh_on_gather=query.refresh_on_gather,
        owner_alias=query.owner_alias,
        reference_url=query.reference_url,
        catalog_source=query.catalog_source,
        result_payload=None,
    )


def _signal_result_payload(metadata: dict[str, str | int | float | bool | None]) -> dict[str, Any] | None:
    raw_payload = metadata.get("result_json")
    if not isinstance(raw_payload, str) or not raw_payload.strip():
        return None
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _build_continuity_workstream_data(
    *,
    issue_number: int,
    bundle: ReportBundle,
    edition_type: EditionType,
    scorecards: tuple[ScorecardData, ...],
    scorecard_packets: dict[str, dict[str, ScorecardEvidencePacket]],
    overrides_document: OverridesDocument,
    workstream_blurbs: dict[str, str],
    dependency_cascades: tuple[DependencyCascade, ...],
    review_status: ReviewStatus,
    evidence_by_item: dict[int, EvidencePacket],
    item_urls: dict[int, str],
    items: tuple[WorkItem, ...],
    chapters: tuple[Any, ...] | None = None,
    eta_forecasts: dict[int, ETAForecast] | None = None,
    source_footnotes: dict[str, str] | None = None,
) -> tuple[WorkstreamData, ...]:
    del dependency_cascades, item_urls
    return _build_continuity_workstream_data_impl(
        issue_number=issue_number,
        bundle=bundle,
        edition_type=edition_type,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        overrides_document=overrides_document,
        workstream_blurbs=workstream_blurbs,
        review_status=review_status,
        evidence_by_item=evidence_by_item,
        items=items,
        chapter_title_builder=lambda chapter: _continuity_chapter_title(chapter, overrides_document),
        higher_risk=_higher_risk,
        ado_base_url=_ado_item_base_url(bundle),
        chapters=chapters,
        eta_forecasts=eta_forecasts,
        source_footnotes=source_footnotes,
    )




def _build_continuity_render_data(
    *,
    bundle: ReportBundle,
    issue_number: int,
    edition_type: EditionType,
    overrides_document: OverridesDocument,
    scorecards: tuple[ScorecardData, ...],
    scorecard_packets: dict[str, dict[str, ScorecardEvidencePacket]],
    workstream_data: tuple[WorkstreamData, ...],
    items: tuple[WorkItem, ...],
    item_urls: dict[int, str],
    eta_forecasts: dict[int, ETAForecast],
) -> ContinuityRenderData | None:
    return _build_continuity_render_data_impl(
        bundle=bundle,
        issue_number=issue_number,
        edition_type=edition_type,
        overrides_document=overrides_document,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        workstream_data=workstream_data,
        items=items,
        item_urls=item_urls,
        eta_forecasts=eta_forecasts,
        slice_contracts=_slice_contract_map(bundle),
        order_dimensions=_order_continuity_dimensions,
    )


def _order_continuity_dimensions(
    dimensions: tuple[DimensionRisk, ...],
    sort_mode: str,
) -> tuple[DimensionRisk, ...]:
    return _order_dimensions_by_risk(dimensions, sort_mode)




def _higher_risk(current: RiskLevel, candidate: RiskLevel) -> RiskLevel:
    if current == RiskLevel.UNKNOWN:
        return candidate
    if candidate == RiskLevel.UNKNOWN:
        return current
    if _RISK_LOAD_WEIGHTS.get(candidate, 0) > _RISK_LOAD_WEIGHTS.get(current, 0):
        return candidate
    return current






































def _rest_call_count(item_count: int, *, batch_size: int = 200) -> int:
    if item_count <= 0:
        return 0
    return ((item_count - 1) // batch_size) + 1




def _read_git_sha() -> str | None:
    return os.environ.get("GIT_COMMIT")
