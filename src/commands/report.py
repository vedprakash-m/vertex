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
import portalocker.exceptions
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
from src.core.archive_store import archive_integrity_waived, find_archive_index_inconsistencies, get_all_green_streak, read_vitality_history, verify_archive_integrity
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
from src.core.adoption_telemetry import GoldenWorkflow, record_adoption
from src.core.alerts import append_or_suppress_alert
from src.core.operation_trace import REF_TYPE_OUTPUT, record_trace_link
from src.core.edition_resolver import filter_workstreams, resolve_edition, get_program_output_dir
from src.core.eml_writer import write_eml
from src.core.evidence_engine import build_evidence
from src.core.exceptions import AuthError, ConfigError, QueryError, QueryTimeoutError, StateError
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
from src.core.program_reality import ProgramReality
from src.core.quality_gates import combine_gate_reports, evaluate_continuity_gates, evaluate_phase_1a_gates, evaluate_phase_1b_gates, evaluate_phase_1c_gates
from src.core.quality_matrix_engine import QualityMatrix, build_quality_matrix, render_quality_matrix_markdown
from src.core.query_builder import build_odata_filter
from src.core.remediation_engine import RemediationReport, build_remediation_report, render_remediation_markdown
from src.core.store_factory import build_signal_store_for_program_id, build_trajectory_store_for_program_id
from src.core.review_status_store import get_review_status_path, load_review_status, save_review_status
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
from src.core.gather_run_manifest import resolve_latest_committed_manifest
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

# re-export all assembly-stage identifiers so existing import sites
# (confirm.py, diff.py, prep.py, evidence.py, …) continue to work
# unchanged after WI-6.2 extraction.
from src.commands.report_pipeline.assemble_stage import (
    DraftArtifacts,
    DraftState,
    ProgramFactSnapshotDraftState,
    _pin_program_fact_snapshot,
    _runtime_db_root_for_reports,
    _is_decision_type,
    _is_risk_type,
    _normalize_section_filter_ids,
    _compute_healthy_streak,
    _compute_read_time_minutes,
    _format_prior_date_label,
    _build_v2_vitality_snapshot,
    _resolve_vitality_workstream_id,
    _vitality_owner_alias,
    _count_new_high_dimensions,
    _has_severe_freshness_signals,
    _truncate_words,
    _decision_strip_ack_required,
    _derive_vector_label,
    _risk_rank,
    _spark_char,
    _derive_risk_sparkline,
    _generate_report_draft_from_context,
    _write_report_adaptive_cards,
    _generate_lookback_draft,
    _load_eta_forecasts,
    _load_draft_ai_context,
    _load_report_signal_context,
    _load_guarded_review_evidence,
    _synthesize_v2_ai_content,
    _build_disabled_ai_synthesis_result,
    _iter_ai_generated_sections,
    _build_newsletter_scoped_items,
    _build_newsletter_narrative_covered_item_ids,
    _create_ai_client,
    _load_live_work_items,
    _build_scorecard_data,
    _apply_scorecard_trend_annotation,
    _build_exec_summary_text,
    _build_exec_summary_severe_signal_seeds,
    _build_continuity_exec_summary_template,
    _build_workstream_templates,
    _visible_detail_section_ids,
    _iter_detail_sections,
    _skipped_review_sections,
    _build_workstream_data,
    _attach_kpi_tiles_to_workstreams,
    _kpi_tiles_for_section,
    _section_workstream_id,
    _kpi_tile_from_signal,
    _kpi_tile_from_query,
    _signal_result_payload,
    _build_continuity_workstream_data,
    _build_continuity_render_data,
    _order_continuity_dimensions,
    _higher_risk,
    _rest_call_count,
    _read_git_sha,
    WorkItemLoader,
)



DEFAULT_ADO_TOP = 1000
_WORK_ITEM_BATCH_SIZE = 200
_BATCH_FIELDS = (
    "System.Id",
    "System.WorkItemType",
    "System.Title",
    "System.State",
    "System.AssignedTo",
    "System.AreaPath",
    "System.IterationPath",
    "System.ChangedDate",
    "Microsoft.VSTS.Scheduling.TargetDate",
    "System.Tags",
    ADO_RISK_ASSESSMENT_FIELD,
    ADO_RISK_ASSESSMENT_COMMENT_FIELD,
)
_TRAJECTORY_BACKFILL_WINDOW_DAYS = 180
DraftEmailSender = Callable[[GraphMailMessage], None]

_build_override_snapshot = build_override_snapshot
_build_report_diff_summary = build_report_diff_summary


@dataclass(frozen=True, slots=True)
class _ReportStageSupport:
    load_program_reality: Callable[..., ProgramReality]
    load_eta_forecasts: Callable[..., dict[int, ETAForecast]]
    build_continuity_deltas: Callable[..., DeltaSet]
    build_scorecard_packets: Callable[..., dict[str, dict[str, Any]]]
    build_scorecard_data: Callable[..., tuple[tuple[ScorecardData, ...], tuple[DimensionRisk, ...], tuple[ScorecardDelta, ...]]]
    load_report_signal_context: Callable[..., _ReportSignalContext | None]
    build_exec_summary_text: Callable[..., str]
    build_override_snapshot: Callable[[OverridesDocument], dict[str, dict[str, dict[str, Any]]]]
    build_top_items: Callable[..., Any]
    build_auto_suggested_top_items: Callable[..., Any]
    build_newsletter_scoped_items: Callable[..., tuple[WorkItem, ...]]
    build_newsletter_narrative_covered_item_ids: Callable[..., tuple[int, ...]]
    visible_continuity_chapters: Callable[..., Any]
    is_continuity_layout: Callable[..., bool]
    build_chapter_templates: Callable[..., Any]
    build_workstream_templates: Callable[..., Any]
    build_continuity_exec_summary_template: Callable[..., str]
    build_exec_summary_template: Callable[..., str]
    active_chapter_notes: Callable[..., dict[str, str]]
    visible_detail_section_ids: Callable[..., Any]
    active_workstream_blurbs: Callable[..., dict[str, str]]
    load_draft_ai_context: Callable[..., Any]
    synthesize_v2_ai_content: Callable[..., Any]
    build_disabled_ai_synthesis_result: Callable[..., Any]
    ensure_review_status: Callable[..., Any]
    skipped_review_sections: Callable[..., Any]
    build_model_program_context: Callable[..., Any]
    build_item_urls: Callable[..., Any]
    build_workstream_data: Callable[..., Any]
    build_continuity_render_data: Callable[..., Any]
    workstream_narrative_warnings: Callable[..., Any]
    detect_stale_narratives: Callable[..., Any]
    count_new_high_dimensions: Callable[..., int]
    decision_strip_ack_required: Callable[..., bool]
    compute_read_time_minutes: Callable[..., int]
    compute_healthy_streak: Callable[..., int]
    build_health_summary: Callable[..., Any]
    build_report_milestone_rows: Callable[..., Any]
    resolve_forwarding_context: Callable[..., Any]
    build_v2_vitality_snapshot: Callable[..., Any]
    ado_saved_query_base_url: Callable[..., str | None]
    ado_item_base_url: Callable[..., str | None]
    derive_qg_status: Callable[..., Any]
    format_edition_title: Callable[..., str]
    subject_signal: Callable[..., str]
    build_email_subject: Callable[..., str]
    build_email_preheader: Callable[..., str]
    format_prior_date_label: Callable[..., str | None]
    build_deck_assumption_rows: Callable[..., Any]
    build_deck_decision_rows: Callable[..., Any]
    build_deck_ask_rows: Callable[..., Any]
    build_deck_render_context: Callable[..., Any]
    build_snapshot: Callable[..., Any]
    group_scorecard_deltas: Callable[..., Any]
    live_kusto_query_executor: Callable[..., Any]
    now_utc: Callable[[], datetime]
    read_git_sha: Callable[[], str | None]
    build_draft_readiness: Callable[..., Any]
    format_ban_violation: Callable[..., str]




_HISTORICAL_ISSUE_CUTOFF = 77  # Preserved for potential future use
_HISTORICAL_ISSUE_GUARD_MESSAGE = "historical issue — KPI IDs migrated; re-render unsupported (NG7)"


def _load_program_reality(
    program_id: str,
    *,
    programs_root: Path,
    as_of: datetime | None = None,
    edition_name: str | None = None,
    archive_root: Path | None = None,
) -> ProgramReality:
    kwargs: dict[str, Any] = {
        "programs_root": programs_root,
        "as_of": as_of,
        "edition_name": edition_name,
    }
    if archive_root is not None:
        kwargs["archive_root"] = archive_root
    return ProgramReality.load(program_id, **kwargs)


def report_command(
    edition: str = typer.Option(..., "--edition", help="Edition name, for example acme_weekly."),
    issue: int | None = typer.Option(None, "--issue", help="Issue number to render. Defaults to next issue."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Generate draft outputs without archive writes. Narrative seeding may still write draft narrative files for the current issue."),
    reseed: bool = typer.Option(False, "--reseed", help="Delete seedable draft narratives for the target issue before re-seeding from the trusted baseline. Dry-run only."),
    no_seed: bool = typer.Option(False, "--no-seed", help="Skip trusted-baseline narrative seeding for this run and render from scaffold templates or existing narratives instead."),
    offline: bool = typer.Option(False, "--offline", help="Render from the newest cached snapshot without live ADO or Kusto calls."),
    diff_mode: bool = typer.Option(False, "--diff", help="Compare the current draft against the last dry-run and print a diff summary."),
    send_draft: bool = typer.Option(False, "--send-draft", help="Send the rendered draft to the author's mailbox for Outlook preview."),
    ai_review: bool = typer.Option(False, "--ai-review", help="Run advisory draft review suggestions after rendering the dry-run draft."),
    no_ai: bool = typer.Option(False, "--no-ai", help="Suppress optional AI-powered draft review helpers."),
    as_of: datetime | None = typer.Option(None, "--as-of", help="Override ADO data timestamp in UTC."),
    edition_type: str | None = typer.Option(None, "--edition-type", help="Override edition type for rendering."),
    lookback_range: int | None = typer.Option(None, "--range", min=2, help="Number of confirmed issues to include for lookback editions. Defaults to the edition window when omitted."),
    sections: list[str] | None = typer.Option(None, "--sections", help="Limit rendered detail sections to these section ids. Repeat or comma-separate values; ws:<id> is accepted."),
    stdout: bool = typer.Option(False, "--stdout", help="Emit compact JSON manifest to stdout."),
    format: str = typer.Option("json", "--format", help="Stdout payload format: json, html, or md."),
    verbose: bool = typer.Option(False, "--verbose", help="Include evidence packet details in --stdout json output."),
    write_overrides: bool = typer.Option(False, "--write-overrides", hidden=True),
) -> None:
    if stdout and diff_mode:
        raise typer.BadParameter("--diff cannot be combined with --stdout.")
    if stdout and send_draft:
        raise typer.BadParameter("--send-draft cannot be combined with --stdout.")
    if stdout and ai_review:
        raise typer.BadParameter("--ai-review cannot be combined with --stdout.")
    if offline and send_draft:
        raise typer.BadParameter("--offline cannot be combined with --send-draft.")
    if offline and as_of is not None:
        raise typer.BadParameter("--offline cannot be combined with --as-of.")
    if verbose and not stdout:
        raise typer.BadParameter("--verbose requires --stdout.")
    if verbose and format.strip().lower() != "json":
        raise typer.BadParameter("--verbose is only supported with --format json.")
    if ai_review and not dry_run:
        raise typer.BadParameter("--ai-review requires --dry-run.")
    if reseed and not dry_run:
        raise typer.BadParameter("--reseed requires --dry-run.")
    if reseed and no_seed:
        raise typer.BadParameter("--reseed cannot be combined with --no-seed.")
    load_result = load_bundle_with_mode(edition, reports_root=REPORTS_ROOT)
    if str(load_result.bundle.config.edition.type).strip().lower() == "nudge":
        raise typer.BadParameter("Use 'vertex nudge --program <id>' for type: nudge editions.")
    # Historical re-render guard: prevent re-rendering issues before the
    # cutoff (schema migration boundary).  Generalized — applies to all
    # editions, not just a specific prefix.
    if issue is not None and issue < _HISTORICAL_ISSUE_CUTOFF:
        typer.echo(_HISTORICAL_ISSUE_GUARD_MESSAGE)
        return
    resolved_option_type = EditionType.from_string(edition_type) if edition_type is not None else None
    effective_edition_type = resolved_option_type or EditionType.from_string(load_result.bundle.config.edition.type)
    section_filter_ids = _normalize_section_filter_ids(sections)
    if lookback_range is not None and effective_edition_type != EditionType.LOOKBACK:
        raise typer.BadParameter("--range requires --edition-type lookback.")
    if section_filter_ids and effective_edition_type == EditionType.LOOKBACK:
        raise typer.BadParameter("--sections is not supported with lookback editions.")

    # WS-1 §9a P1: archive integrity pre-flight — blocks dangling refs before any render/write.
    if not archive_integrity_waived():
        _ai_result = verify_archive_integrity(edition, archive_root=ARCHIVE_ROOT)
        if not _ai_result.ok:
            typer.echo(
                f"[ARCHIVE INTEGRITY FAILURE] {len(_ai_result.inconsistencies)} inconsistency/ies found "
                f"in the {edition!r} archive:\n"
                + "\n".join(f"  - {i}" for i in _ai_result.inconsistencies)
                + "\nFix with: python scripts/reconcile_archive_index.py --edition "
                + edition
                + " --strategy readd --dry-run\n"
                + "Bypass (not recommended): VERTEX_ARCHIVE_INTEGRITY_WAIVER=1 vertex report ...",
                err=True,
            )
            raise typer.Exit(code=3)

    if not stdout:
        typer.echo("[MODE: V2 HYBRID JOURNAL]")

    trace_path = get_command_trace_path(edition, "report") if verbose else None
    trace_logger: RunLoggerAdapter | None = None
    if trace_path is not None:
        trace_logger = configure_file_logging(
            uuid.uuid4().hex,
            trace_path=trace_path,
            logger_name=f"vertex.report.{edition}",
        )
        trace_logger.info(
            "report command started",
            extra={
                "stage": "command",
                "edition": edition,
                "issue_number": issue,
                "dry_run": dry_run,
                "stdout": stdout,
            },
        )

    # ADF-W2.12 (Section 8.2.6): generate one correlation_id for the whole
    # report run, threaded through every pipeline stage (via StageContext) so
    # each artifact-producing stage can record a trace link sharing it. The
    # run_id distinguishes two runs that happen to share a correlation_id
    # (e.g. a retry); both are stable strings, generated here -- the single
    # upstream source -- rather than fresh per call site.
    correlation_id = uuid.uuid4().hex
    workflow_id = GoldenWorkflow.WEEKLY_REPORT.value
    run_id = uuid.uuid4().hex

    try:
        _maybe_auto_run_workiq_enrich(
            edition_name=edition,
            dry_run=dry_run,
            offline=offline,
            show_progress=not stdout,
        )
        artifacts = generate_report_draft(
            edition_name=edition,
            issue_number=issue,
            reseed=reseed,
            no_seed=no_seed,
            dry_run=dry_run,
            offline=offline,
            diff_mode=diff_mode,
            as_of=as_of,
            edition_type_override=edition_type,
            lookback_range=lookback_range,
            section_filter_ids=section_filter_ids,
            open_browser=not stdout,
            show_progress=not stdout,
            trace_logger=trace_logger,
            correlation_id=correlation_id,
            workflow_id=workflow_id,
            run_id=run_id,
        )
    except QueryTimeoutError as error:
        typer.echo(str(error))
        raise typer.Exit(code=4)
    except (AuthError, ConfigError, QueryError) as error:
        typer.echo(str(error))
        raise typer.Exit(code=2)

    # ADF-W5.14: a real, completed (non-dry-run) report generation is the
    # weekly_report golden workflow's adoption moment -- a --dry-run is a
    # preview, not a completed run, matching cockpit's --no-persist gate.
    if not dry_run:
        try:
            record_adoption(load_result.bundle.program.id, GoldenWorkflow.WEEKLY_REPORT, programs_root=PROGRAMS_ROOT)
        except (OSError, StateError):
            pass

        # ADF-W2.12: the render stage records its output artifact under the
        # shared correlation_id generated once at report_command entry (now
        # threaded through every stage via StageContext). This is the output
        # stage's link; acquisition/fact/context/release links are recorded
        # by their owning stages inside the pipeline, all sharing this id.
        try:
            record_trace_link(
                program_id=load_result.bundle.program.id,
                correlation_id=correlation_id,
                workflow_id=workflow_id,
                run_id=run_id,
                stage="render",
                ref_type=REF_TYPE_OUTPUT,
                ref_id=str(artifacts.manifest_path) if artifacts.manifest_path is not None else f"issue-{artifacts.issue_number}",
                programs_root=PROGRAMS_ROOT,
            )
        except (OSError, portalocker.exceptions.LockException):
            pass

    if write_overrides:
        typer.echo("--write-overrides is deprecated; use 'vertex apply-overrides' instead.", err=True)
        report_dir = next(
            (
                path.parent
                for path in (
                    artifacts.html_path,
                    artifacts.md_path,
                    artifacts.manifest_path,
                    artifacts.snapshot_path,
                )
                if path is not None
            ),
            None,
        )
        if report_dir is None:
            typer.echo("Unable to locate report output directory for override application.", err=True)
            raise typer.Exit(code=2)
        applied_overrides_path = apply_pending_overrides(edition, report_dir, reports_root=REPORTS_ROOT)
        if applied_overrides_path is None:
            typer.echo("No pending overrides found to apply. Run 'vertex report' first.", err=True)
            raise typer.Exit(code=2)
        artifacts = replace(artifacts, overrides_path=applied_overrides_path)

    review_path: Path | None = None
    review_json_path: Path | None = None
    review_summary: str | None = None
    if ai_review:
        if no_ai:
            review_summary = "AI review suppressed by --no-ai."
        else:
            bundle = load_bundle(edition, reports_root=REPORTS_ROOT)
            review_report, info_messages = review_draft(
                report=artifacts.report,
                draft_markdown=artifacts.markdown_body,
                program_context=bundle.program_context,
                editorial_rules=bundle.editorial_rules,
                kusto_settings=bundle.config.kusto,
                edition_name=edition,
                archive_root=ARCHIVE_ROOT,
            )
            review_body = render_review_markdown(review_report, info_messages)
            review_path = _write_output_text(
                get_program_output_dir(edition, programs_root=PROGRAMS_ROOT) / f"issue_{artifacts.issue_number:03d}" / f"issue_{artifacts.issue_number:03d}.review.md",
                review_body,
            )
            review_json_path = _write_output_json(
                get_program_output_dir(edition, programs_root=PROGRAMS_ROOT) / f"issue_{artifacts.issue_number:03d}" / f"issue_{artifacts.issue_number:03d}.review.json",
                build_review_artifact(
                    review_report,
                    info_messages=info_messages,
                    report=artifacts.report,
                    rendered_kusto_query_ids=tuple(section.query_id for section in artifacts.kusto_sections),
                ),
            )
            review_summary = render_review_summary(review_report, info_messages)

    if stdout:
        verbose_evidence = None
        if verbose:
            bundle = load_bundle(edition, reports_root=REPORTS_ROOT)
            trusted_issue = load_trusted_baseline_issue(edition, before_issue_number=artifacts.issue_number)
            previous_snapshot, _ = _load_previous_snapshot(edition, artifacts.issue_number, ARCHIVE_ROOT, trusted_issue_number=trusted_issue)
            evidence_window_start = artifacts.report.ado_data_as_of - timedelta(days=bundle.config.ado.date_window_days)
            evidence_by_item = {
                item.id: build_evidence(item, evidence_window_start, artifacts.report.ado_data_as_of)
                for item in artifacts.report.items
            }
            scorecard_packets = _build_scorecard_packets(
                bundle,
                artifacts.report.items,
                previous_snapshot,
                edition_name=edition,
                archive_root=ARCHIVE_ROOT,
            )
            verbose_evidence = build_verbose_evidence_payload(evidence_by_item, scorecard_packets)
        compact_manifest = build_compact_manifest(
            manifest=artifacts.manifest,
            eml_path=artifacts.eml_path,
            html_path=artifacts.html_path,
            md_path=artifacts.md_path,
            manifest_path=artifacts.manifest_path,
            snapshot_path=artifacts.snapshot_path,
            quality_matrix_md_path=artifacts.quality_matrix_md_path,
            quality_matrix_json_path=artifacts.quality_matrix_json_path,
            remediation_md_path=artifacts.remediation_md_path,
            remediation_json_path=artifacts.remediation_json_path,
            overrides_path=artifacts.overrides_path,
            narratives_dir=artifacts.narratives_dir,
            review_status_path=artifacts.review_status_path,
            warnings=artifacts.warnings,
            suggested_subject=artifacts.email_subject,
            suggested_preheader=artifacts.email_preheader,
            verbose_evidence=verbose_evidence,
            trace_path=trace_path,
        )
        typer.echo(
            render_stdout_payload(
                output_format=format,
                compact_manifest=compact_manifest,
                html_body=artifacts.html_body,
                markdown_body=artifacts.markdown_body,
            ),
            nl=False,
        )
        raise typer.Exit(code=artifacts.exit_code)

    if send_draft:
        if artifacts.html_path is None:
            typer.echo("Draft email not sent because the draft has blocking render errors.")
        else:
            bundle = load_bundle(edition, reports_root=REPORTS_ROOT)
            try:
                _build_draft_email_sender()(_build_draft_email_message(bundle, artifacts))
                typer.echo(f"Draft email sent to {bundle.config.author.email}.")
            except (AuthError, QueryError) as error:
                typer.echo(str(error))
                raise typer.Exit(code=2)

    typer.echo(f"Draft issue {artifacts.issue_number:03d} generated for {edition}.")
    if artifacts.email_subject:
        typer.echo(f"Suggested subject: {artifacts.email_subject}")
    if artifacts.email_preheader:
        typer.echo(f"Suggested preheader: {artifacts.email_preheader}")
    if artifacts.html_path is not None:
        typer.echo(f"HTML: {artifacts.html_path}")
    if artifacts.eml_path is not None:
        typer.echo(f"EML: {artifacts.eml_path}")
    if artifacts.md_path is not None:
        typer.echo(f"Markdown: {artifacts.md_path}")
    if artifacts.manifest_path is not None:
        typer.echo(f"Manifest: {artifacts.manifest_path}")
    if artifacts.quality_matrix_md_path is not None:
        typer.echo(f"Quality Matrix (Markdown): {artifacts.quality_matrix_md_path}")
    if artifacts.quality_matrix_json_path is not None:
        typer.echo(f"Quality Matrix (JSON): {artifacts.quality_matrix_json_path}")
    if artifacts.remediation_md_path is not None:
        typer.echo(f"Remediation (Markdown): {artifacts.remediation_md_path}")
    if artifacts.remediation_json_path is not None:
        typer.echo(f"Remediation (JSON): {artifacts.remediation_json_path}")
    if artifacts.workstream_snapshot_md_path is not None:
        typer.echo(f"Workstream Snapshot (Markdown): {artifacts.workstream_snapshot_md_path}")
    if artifacts.workstream_snapshot_json_path is not None:
        typer.echo(f"Workstream Snapshot (JSON): {artifacts.workstream_snapshot_json_path}")
    if artifacts.workstream_associations_json_path is not None:
        typer.echo(f"Workstream Associations (JSON): {artifacts.workstream_associations_json_path}")
    if artifacts.persona_signal_coverage_path is not None:
        typer.echo(f"Persona Signal Coverage (JSON): {artifacts.persona_signal_coverage_path}")
    typer.echo(f"Overrides: {artifacts.overrides_path}")
    typer.echo(f"Narratives: {artifacts.narratives_dir}")
    if review_summary is not None:
        typer.echo(review_summary)
        if review_path is not None:
            typer.echo(f"Review Markdown: {review_path}")
        if review_json_path is not None:
            typer.echo(f"Review JSON: {review_json_path}")
    if artifacts.warnings:
        typer.echo(f"Warnings: {len(artifacts.warnings)}")
        for warning in artifacts.warnings:
            typer.echo(f"- {warning}")
    if artifacts.draft_readiness is not None:
        typer.echo(artifacts.draft_readiness.summary)

    if diff_mode and artifacts.diff_summary is not None:
        typer.echo("")
        typer.echo(artifacts.diff_summary, nl=False)

    if trace_logger is not None and trace_path is not None:
        trace_logger.info(
            "report command completed",
            extra={
                "stage": "command",
                "edition": edition,
                "issue_number": artifacts.issue_number,
                "exit_code": artifacts.exit_code,
            },
        )
        typer.echo(f"Trace: {trace_path}")

    raise typer.Exit(code=artifacts.exit_code)


def generate_report_draft(
    edition_name: str,
    issue_number: int | None = None,
    reseed: bool = False,
    no_seed: bool = False,
    dry_run: bool = False,
    offline: bool = False,
    diff_mode: bool = False,
    as_of: datetime | None = None,
    edition_type_override: str | None = None,
    lookback_range: int | None = None,
    section_filter_ids: tuple[str, ...] = (),
    reports_root: Path | None = None,
    archive_root: Path | None = None,
    programs_root: Path | None = None,
    work_item_loader: WorkItemLoader | None = None,
    kusto_query_executor: KustoQueryExecutor | None = None,
    open_browser: bool = False,
    show_progress: bool = False,
    trace_logger: RunLoggerAdapter | None = None,
    correlation_id: str = "",
    workflow_id: str = "",
    run_id: str = "",
) -> DraftArtifacts:
    final_ctx = _execute_report_pipeline(
        _build_stage_request_context(
            edition_name=edition_name,
            issue_number=issue_number,
            reseed=reseed,
            no_seed=no_seed,
            dry_run=dry_run,
            offline=offline,
            diff_mode=diff_mode,
            as_of=as_of,
            edition_type_override=edition_type_override,
            lookback_range=lookback_range,
            section_filter_ids=section_filter_ids,
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=programs_root,
            work_item_loader=work_item_loader,
            kusto_query_executor=kusto_query_executor,
            open_browser=open_browser,
            correlation_id=correlation_id,
            workflow_id=workflow_id,
            run_id=run_id,
        ),
        show_progress=show_progress,
        trace_logger=trace_logger,
    )
    if final_ctx.artifacts is None:
        raise RuntimeError("Report pipeline completed without draft artifacts.")
    return final_ctx.artifacts


def generate_report_draft_v2(
    edition_name: str,
    issue_number: int | None = None,
    reseed: bool = False,
    no_seed: bool = False,
    dry_run: bool = False,
    offline: bool = False,
    diff_mode: bool = False,
    as_of: datetime | None = None,
    edition_type_override: str | None = None,
    lookback_range: int | None = None,
    section_filter_ids: tuple[str, ...] = (),
    reports_root: Path | None = None,
    archive_root: Path | None = None,
    programs_root: Path | None = None,
    work_item_loader: WorkItemLoader | None = None,
    kusto_query_executor: KustoQueryExecutor | None = None,
    open_browser: bool = False,
    show_progress: bool = False,
    trace_logger: RunLoggerAdapter | None = None,
) -> DraftArtifacts:
    """Deprecated alias for `generate_report_draft`.

    Historically a separate "incremental staged pipeline" body, but it executed
    the identical `_execute_report_pipeline(...)` call with no behavioral
    difference (specs/fix-data-flow.md Track H / PS-6). Duplicate body removed;
    retained under this name only because tests still call it by name.
    """
    return generate_report_draft(
        edition_name=edition_name,
        issue_number=issue_number,
        reseed=reseed,
        no_seed=no_seed,
        dry_run=dry_run,
        offline=offline,
        diff_mode=diff_mode,
        as_of=as_of,
        edition_type_override=edition_type_override,
        lookback_range=lookback_range,
        section_filter_ids=section_filter_ids,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        work_item_loader=work_item_loader,
        kusto_query_executor=kusto_query_executor,
        open_browser=open_browser,
        show_progress=show_progress,
        trace_logger=trace_logger,
    )


def _pin_gather_run_lineage(ctx: StageContext) -> StageContext:
    """D-17: resolve the latest committed gather run exactly once per report
    invocation, immediately after resolution, and pin it on the context so
    every downstream stage that builds a RunManifest or DraftState (standard
    or lookback path) stamps the same identical gather_run_id/gather_run_hash
    pair. Confirm never calls this -- it reads the pinned value back off the
    draft state instead, so it structurally cannot silently rebind to a
    newer committed run than the one the draft was generated from."""
    program_id = ctx.resolved_v2.program.id if ctx.resolved_v2 is not None else None
    if program_id is None:
        return ctx
    committed = resolve_latest_committed_manifest(program_id, programs_root=ctx.programs_root or PROGRAMS_ROOT)
    if committed is None:
        return ctx
    return replace(ctx, gather_run_id=committed.run_id, gather_run_hash=committed.manifest_hash)


def _execute_report_pipeline(
    request_ctx: StageContext,
    *,
    show_progress: bool = False,
    trace_logger: RunLoggerAdapter | None = None,
) -> StageContext:
    progress_callback = _build_report_pipeline_progress_callback(show_progress=show_progress, trace_logger=trace_logger)
    resolution_stage = ResolutionStage()
    resolution_started = perf_counter()
    resolved_ctx = resolution_stage.execute(request_ctx)
    resolved_ctx = _pin_gather_run_lineage(resolved_ctx)
    pipeline_stages = _report_pipeline_stages(resolved_ctx.resolved_edition_type)
    total_stages = 1 + len(pipeline_stages)
    if progress_callback is not None:
        progress_callback(
            resolution_stage,
            1,
            total_stages,
            request_ctx,
            resolved_ctx,
            perf_counter() - resolution_started,
        )
    return run_pipeline(
        pipeline_stages,
        resolved_ctx,
        progress_callback=progress_callback,
        start_index=2,
        total_stages=total_stages,
    )


def _report_pipeline_stages(resolved_edition_type: EditionType | None):
    if resolved_edition_type == EditionType.LOOKBACK:
        return (_LookbackStage(),)
    return (FetchStage(), ComputeStage(), MilestoneStage(), RiskStage(), ActionStage(), NarrativeStage(), AIStage(), RenderStage(), ValidationStage(), _OutputStage())


def _emit_report_pipeline_progress(stage, index: int, total: int, before_ctx: StageContext, after_ctx: StageContext, elapsed_seconds: float) -> None:
    del before_ctx
    detail = _format_report_pipeline_progress_detail(stage.name(), after_ctx)
    suffix = f" | {detail}" if detail else ""
    typer.echo(f"[{index}/{total}] {stage.name():<10} {elapsed_seconds:0.2f}s{suffix}")


def _build_report_pipeline_progress_callback(
    *,
    show_progress: bool,
    trace_logger: RunLoggerAdapter | None,
):
    if not show_progress and trace_logger is None:
        return None

    def _progress(stage, index: int, total: int, before_ctx: StageContext, after_ctx: StageContext, elapsed_seconds: float) -> None:
        if show_progress:
            _emit_report_pipeline_progress(stage, index, total, before_ctx, after_ctx, elapsed_seconds)
        if trace_logger is not None:
            detail = _format_report_pipeline_progress_detail(stage.name(), after_ctx)
            trace_logger.info(
                "report stage completed",
                extra={
                    "stage": stage.name(),
                    "step_index": index,
                    "step_count": total,
                    "elapsed_seconds": round(elapsed_seconds, 3),
                    "detail": detail,
                },
            )

    return _progress


def _format_report_pipeline_progress_detail(stage_name: str, ctx: StageContext) -> str:
    details: list[str] = []
    if stage_name == "resolution":
        if ctx.resolved_issue_number is not None:
            details.append(f"issue={ctx.resolved_issue_number:03d}")
        if ctx.resolved_edition_type is not None:
            details.append(f"type={ctx.resolved_edition_type.value}")
    elif stage_name == "fetch":
        details.append(f"items={len(ctx.items)}")
        if ctx.ado_calls is not None:
            details.append(f"ado_calls={ctx.ado_calls}")
        if ctx.offline_source_label is not None:
            details.append(ctx.offline_source_label)
    elif stage_name == "compute":
        details.append(f"items={len(ctx.items)}")
        scorecard_count = _safe_length(ctx.scorecards)
        if scorecard_count is not None:
            details.append(f"scorecards={scorecard_count}")
    elif stage_name == "milestone":
        milestone_count = _safe_length(ctx.milestone_assessments)
        if milestone_count is not None:
            details.append(f"milestones={milestone_count}")
        details.append(f"warnings={len(ctx.milestone_warnings)}")
    elif stage_name == "risk":
        risk_count = _safe_length(ctx.risks)
        if risk_count is not None:
            details.append(f"risks={risk_count}")
        details.append(f"stale={len(ctx.stale_risk_ids)}")
        details.append(f"warnings={len(ctx.risk_warnings)}")
    elif stage_name == "action":
        action_count = _safe_length(ctx.actions)
        if action_count is not None:
            details.append(f"actions={action_count}")
        details.append(f"overdue={len(ctx.overdue_action_ids)}")
    elif stage_name == "narrative":
        section_count = _safe_length(ctx.visible_section_ids)
        if section_count is not None:
            details.append(f"sections={section_count}")
        details.append(f"top_items={len(ctx.top_3_now)}")
    elif stage_name == "ai":
        blurb_count = _safe_length(ctx.render_workstream_blurbs)
        if blurb_count is not None:
            details.append(f"blurbs={blurb_count}")
        if ctx.offline:
            details.append("offline")
    elif stage_name == "render":
        if ctx.render_exec_summary_text is not None:
            details.append("exec_summary=ready")
        blurb_count = _safe_length(ctx.render_workstream_blurbs)
        if blurb_count is not None:
            details.append(f"blurbs={blurb_count}")
    elif stage_name == "validation":
        warning_count = _safe_length(getattr(ctx.validation_state, "warnings", None))
        if warning_count is not None:
            details.append(f"warnings={warning_count}")
        exit_code = getattr(ctx.validation_state, "exit_code", None)
        if exit_code is not None:
            details.append(f"exit={exit_code}")
        persona_coverage = getattr(ctx.validation_state, "persona_coverage", None)
        if persona_coverage is not None:
            details.append(
                "persona="
                f"{len(persona_coverage.passed)}/{persona_coverage.total_evaluations}"
            )
    elif stage_name in {"output", "lookback"}:
        if ctx.artifacts is not None:
            details.append(f"issue={ctx.artifacts.issue_number:03d}")
            details.append(f"warnings={len(ctx.artifacts.warnings)}")
            details.append(f"exit={ctx.artifacts.exit_code}")
    return ", ".join(details)


def _safe_length(value: object) -> int | None:
    if value is None:
        return None
    try:
        return len(value)  # type: ignore[arg-type]
    except TypeError:
        return None


def _build_stage_request_context(
    *,
    edition_name: str,
    issue_number: int | None,
    reseed: bool,
    no_seed: bool,
    dry_run: bool,
    offline: bool,
    diff_mode: bool,
    as_of: datetime | None,
    edition_type_override: str | None,
    lookback_range: int | None,
    section_filter_ids: tuple[str, ...],
    reports_root: Path | None,
    archive_root: Path | None,
    programs_root: Path | None = None,
    work_item_loader: WorkItemLoader | None,
    kusto_query_executor: KustoQueryExecutor | None,
    open_browser: bool,
    correlation_id: str = "",
    workflow_id: str = "",
    run_id: str = "",
) -> StageContext:
    started_at = datetime.now(timezone.utc)
    resolved_reports_root = reports_root or REPORTS_ROOT
    resolved_archive_root = archive_root or ARCHIVE_ROOT
    data_as_of = _parse_datetime(as_of) if as_of is not None else started_at
    if data_as_of is None:
        data_as_of = started_at

    return StageContext(
        edition_name=edition_name,
        issue_number=issue_number,
        reseed=reseed,
        no_seed=no_seed,
        dry_run=dry_run,
        offline=offline,
        diff_mode=diff_mode,
        as_of=as_of,
        edition_type_override=edition_type_override,
        lookback_range=lookback_range,
        section_filter_ids=section_filter_ids,
        reports_root=resolved_reports_root,
        archive_root=resolved_archive_root,
        programs_root=programs_root,
        work_item_loader=work_item_loader or _load_live_work_items,
        kusto_query_executor=kusto_query_executor,
        open_browser=open_browser,
        stage_support=_ReportStageSupport(
            load_program_reality=_load_program_reality,
            load_eta_forecasts=_load_eta_forecasts,
            build_continuity_deltas=_build_continuity_deltas,
            build_scorecard_packets=_build_scorecard_packets,
            build_scorecard_data=_build_scorecard_data,
            load_report_signal_context=_load_report_signal_context,
            build_exec_summary_text=_build_exec_summary_text,
            build_override_snapshot=_build_override_snapshot,
            build_top_items=_build_top_items,
            build_auto_suggested_top_items=_build_auto_suggested_top_items,
            build_newsletter_scoped_items=_build_newsletter_scoped_items,
            build_newsletter_narrative_covered_item_ids=_build_newsletter_narrative_covered_item_ids,
            visible_continuity_chapters=_visible_continuity_chapters,
            is_continuity_layout=_is_continuity_layout,
            build_chapter_templates=_build_chapter_templates,
            build_workstream_templates=_build_workstream_templates,
            build_continuity_exec_summary_template=_build_continuity_exec_summary_template,
            build_exec_summary_template=_build_exec_summary_template,
            active_chapter_notes=_active_chapter_notes,
            visible_detail_section_ids=_visible_detail_section_ids,
            active_workstream_blurbs=_active_workstream_blurbs,
            load_draft_ai_context=_load_draft_ai_context,
            synthesize_v2_ai_content=_synthesize_v2_ai_content,
            build_disabled_ai_synthesis_result=_build_disabled_ai_synthesis_result,
            ensure_review_status=_ensure_review_status,
            skipped_review_sections=_skipped_review_sections,
            build_model_program_context=_build_model_program_context,
            build_item_urls=_build_item_urls,
            build_workstream_data=_build_workstream_data,
            build_continuity_render_data=_build_continuity_render_data,
            workstream_narrative_warnings=_workstream_narrative_warnings,
            detect_stale_narratives=detect_stale_narratives,
            count_new_high_dimensions=_count_new_high_dimensions,
            decision_strip_ack_required=_decision_strip_ack_required,
            compute_read_time_minutes=_compute_read_time_minutes,
            compute_healthy_streak=_compute_healthy_streak,
            build_health_summary=_build_health_summary,
            build_report_milestone_rows=_build_report_milestone_rows,
            resolve_forwarding_context=_resolve_forwarding_context,
            build_v2_vitality_snapshot=_build_v2_vitality_snapshot,
            ado_saved_query_base_url=_ado_saved_query_base_url,
            ado_item_base_url=_ado_item_base_url,
            derive_qg_status=_derive_qg_status,
            format_edition_title=_format_edition_title,
            subject_signal=_subject_signal,
            build_email_subject=_build_email_subject,
            build_email_preheader=_build_email_preheader,
            format_prior_date_label=_format_prior_date_label,
            build_deck_assumption_rows=_build_deck_assumption_rows,
            build_deck_decision_rows=_build_deck_decision_rows,
            build_deck_ask_rows=_build_deck_ask_rows,
            build_deck_render_context=_build_deck_render_context,
            build_snapshot=_build_snapshot,
            group_scorecard_deltas=_group_scorecard_deltas,
            live_kusto_query_executor=build_live_kusto_query_executor,
            now_utc=lambda: datetime.now(timezone.utc),
            read_git_sha=_read_git_sha,
            build_draft_readiness=_build_draft_readiness,
            format_ban_violation=_format_ban_violation,
        ),
        started_at=started_at,
        data_as_of=data_as_of,
        correlation_id=correlation_id,
        workflow_id=workflow_id,
        run_id=run_id,
    )


class _LookbackStage:
    def name(self) -> str:
        return "lookback"

    def execute(self, ctx: StageContext) -> StageContext:
        if ctx.resolved_edition_type != EditionType.LOOKBACK or ctx.artifacts is not None:
            return ctx
        if (
            ctx.bundle is None
            or ctx.archive_index is None
            or ctx.resolved_issue_number is None
            or ctx.started_at is None
            or ctx.data_as_of is None
            or ctx.reports_root is None
            or ctx.archive_root is None
            or ctx.programs_root is None
        ):
            raise RuntimeError("ResolutionStage must execute before LookbackStage.")
        return replace(
            ctx,
            artifacts=_generate_lookback_draft(
                edition_name=ctx.edition_name,
                bundle=ctx.bundle,
                archive_index=ctx.archive_index,
                resolved_issue_number=ctx.resolved_issue_number,
                previous_dry_run_state=ctx.previous_dry_run_state,
                started_at=ctx.started_at,
                data_as_of=ctx.data_as_of,
                lookback_range=ctx.lookback_range,
                diff_mode=ctx.diff_mode,
                reports_root=ctx.reports_root,
                archive_root=ctx.archive_root,
                open_browser=ctx.open_browser,
                gather_run_id=ctx.gather_run_id,
                gather_run_hash=ctx.gather_run_hash,
            ),
        )


class _OutputStage:
    def name(self) -> str:
        return "output"

    def execute(self, ctx: StageContext) -> StageContext:
        if ctx.artifacts is not None:
            return ctx
        if (
            ctx.programs_root is None
            or ctx.resolved_issue_number is None
            or ctx.started_at is None
            or ctx.data_as_of is None
            or ctx.resolved_edition_type is None
            or ctx.overrides_path is None
            or ctx.narratives_dir is None
            or ctx.override_snapshot is None
            or ctx.render_exec_summary_text is None
            or ctx.render_workstream_blurbs is None
            or ctx.render_state is None
            or ctx.validation_state is None
            or ctx.reports_root is None
            or ctx.archive_root is None
            or ctx.editions_root is None
            or ctx.programs_root is None
        ):
            raise RuntimeError("ValidationStage must execute before OutputStage.")

        render_state = ctx.render_state
        validation_state = ctx.validation_state
        diff_summary = None
        if ctx.diff_mode and ctx.previous_dry_run_state is not None and ctx.evidence_by_item is not None:
            ado_lines = _build_draft_ado_diff_lines(
                previous_dry_run_state=ctx.previous_dry_run_state,
                current_items=ctx.items,
                current_evidence_by_item=ctx.evidence_by_item,
                current_issue_number=ctx.resolved_issue_number,
                current_data_as_of=ctx.data_as_of,
                current_edition_type=ctx.resolved_edition_type,
            )
            diff_summary = build_report_diff_summary(
                previous_dry_run_state=ctx.previous_dry_run_state,
                current_issue_number=ctx.resolved_issue_number,
                current_override_snapshot=ctx.override_snapshot,
                current_top_3_now=ctx.top_3_now,
                current_exec_summary_text=ctx.render_exec_summary_text,
                ado_lines=ado_lines,
            )

        html_path: Path | None = None
        md_path: Path | None = None
        manifest_path: Path | None = None
        snapshot_path: Path | None = None
        output_dir = get_program_output_dir(ctx.edition_name, programs_root=ctx.programs_root)
        output_dir.mkdir(parents=True, exist_ok=True)
        issue_dir = output_dir / f"issue_{ctx.resolved_issue_number:03d}"
        quality_matrix_md_path = _write_output_text(
            issue_dir / f"issue_{ctx.resolved_issue_number:03d}.quality_matrix.md",
            render_quality_matrix_markdown(render_state.quality_matrix),
        )
        quality_matrix_json_path = _write_output_json(
            issue_dir / f"issue_{ctx.resolved_issue_number:03d}.quality_matrix.json",
            render_state.quality_matrix,
        )
        remediation_md_path = _write_output_text(
            issue_dir / f"issue_{ctx.resolved_issue_number:03d}.remediation.md",
            render_remediation_markdown(render_state.remediation_report),
        )
        remediation_json_path = _write_output_json(
            issue_dir / f"issue_{ctx.resolved_issue_number:03d}.remediation.json",
            render_state.remediation_report,
        )
        workstream_snapshot_md_path, workstream_snapshot_json_path, workstream_associations_json_path = _write_workstream_snapshot_artifacts(
            program_id=(
                ctx.resolved_v2.program.id
                if ctx.resolved_v2 is not None
                else (ctx.signal_context.program_id if ctx.signal_context is not None else None)
            ),
            bundle=ctx.bundle,
            issue_number=ctx.resolved_issue_number,
            edition_name=ctx.edition_name,
            generated_at=ctx.started_at,
            quality_matrix=render_state.quality_matrix,
            markdown_body=render_state.markdown_body,
            items=ctx.items,
            output_dir=issue_dir,
            programs_root=getattr(ctx, "programs_root", PROGRAMS_ROOT),
        )
        eml_path: Path | None = None
        if ctx.resolved_edition_type == EditionType.DECK:
            md_path = _write_output_text(issue_dir / f"issue_{ctx.resolved_issue_number:03d}.deck.md", render_state.markdown_body)
        else:
            eml_path = write_eml(
                issue_dir / f"issue_{ctx.resolved_issue_number:03d}.eml",
                eml_bytes=_build_preview_eml_bytes(
                    ctx.bundle,
                    issue_number=ctx.resolved_issue_number,
                    as_of=ctx.data_as_of,
                    html_body=render_state.html_body,
                    markdown_body=render_state.markdown_body,
                    suggested_subject=render_state.email_subject,
                    generated_at=ctx.started_at,
                ),
            )
            html_path = _write_output_text(issue_dir / f"issue_{ctx.resolved_issue_number:03d}.html", render_state.html_body)
            md_path = _write_output_text(issue_dir / f"issue_{ctx.resolved_issue_number:03d}.md", render_state.markdown_body)
        snapshot_path = _write_output_json(issue_dir / f"issue_{ctx.resolved_issue_number:03d}.snapshot.json", render_state.snapshot)
        persona_signal_coverage_path = (
            _write_output_json(
                issue_dir / f"issue_{ctx.resolved_issue_number:03d}.persona_signal_coverage.json",
                validation_state.persona_coverage,
            )
            if validation_state.persona_coverage is not None
            else None
        )
        continuation_contract = build_continuation_contract(
            edition_name=ctx.edition_name,
            issue_number=ctx.resolved_issue_number,
            started_at=ctx.started_at,
            reports_root=ctx.reports_root,
            archive_root=ctx.archive_root,
            editions_root=ctx.editions_root,
            programs_root=ctx.programs_root,
            overrides_document=ctx.overrides_document,
            workstream_data=render_state.workstream_data,
            output_dir=output_dir,
            current_scorecard_dimensions=tuple(
                sorted(
                    (scorecard.name, dimension.name)
                    for scorecard in ctx.bundle.config.scorecards
                    for dimension in scorecard.dimensions
                )
            ),
            current_section_ids=(
                ctx.section_roster_current_ids
                or tuple(sorted(("exec_summary", *(section.section_id for section in render_state.workstream_data))))
            ),
            narrative_seeding=ctx.narrative_seeding,
            overrides_seeding=ctx.overrides_seeding,
        )
        continuation_contract_path = (
            _write_output_json(
                get_continuation_contract_path(output_dir, ctx.resolved_issue_number),
                continuation_contract,
            )
            if continuation_contract is not None
            else None
        )
        draft_program_id = (
            ctx.resolved_v2.program.id
            if ctx.resolved_v2 is not None
            else (ctx.signal_context.program_id if ctx.signal_context is not None else None)
        )
        _write_output_json(
            issue_dir / f"issue_{ctx.resolved_issue_number:03d}.draft.json",
            DraftState(
                issue_number=ctx.resolved_issue_number,
                generated_at=ctx.started_at,
                ado_data_as_of=ctx.data_as_of,
                edition_type=ctx.resolved_edition_type,
                items=ctx.items,
                workstream_blurbs=ctx.render_workstream_blurbs,
                ai_prompt_versions=dict(ctx.ai_synthesis.prompt_versions) if ctx.ai_synthesis is not None else {},
                ai_confidences=dict(ctx.ai_synthesis.ai_confidences) if ctx.ai_synthesis is not None else {},
                ai_trace_run_id=ctx.ai_synthesis.trace_run_id if ctx.ai_synthesis is not None else None,
                kusto_sections=render_state.kusto_sections,
                override_snapshot=ctx.override_snapshot,
                program_fact_snapshot=_pin_program_fact_snapshot(
                    draft_program_id,
                    edition_name=ctx.edition_name,
                    issue_number=ctx.resolved_issue_number,
                    generated_at=ctx.started_at,
                    db_root=_runtime_db_root_for_reports(ctx.reports_root),
                ),
                top_3_now=ctx.top_3_now,
                exec_summary_text=ctx.render_exec_summary_text,
                gather_run_id=ctx.gather_run_id,
                gather_run_hash=ctx.gather_run_hash,
            ),
        )
        adaptive_card_paths = _write_report_adaptive_cards(
            bundle=ctx.bundle,
            edition_name=ctx.edition_name,
            issue_number=ctx.resolved_issue_number,
            edition_type=ctx.resolved_edition_type,
            report=validation_state.report,
            programs_root=ctx.programs_root,
            report_html_path=html_path,
        )
        manifest_path = write_run_manifest(
            ctx.edition_name,
            ctx.resolved_issue_number,
            validation_state.manifest,
            programs_root=ctx.programs_root,
        )
        if ctx.open_browser and html_path is not None:
            webbrowser.open(html_path.resolve().as_uri())

        return replace(
            ctx,
            artifacts=DraftArtifacts(
                issue_number=ctx.resolved_issue_number,
                exit_code=validation_state.exit_code,
                report=validation_state.report,
                snapshot=render_state.snapshot,
                manifest=validation_state.manifest,
                quality_matrix=render_state.quality_matrix,
                remediation_report=render_state.remediation_report,
                html_body=render_state.html_body,
                markdown_body=render_state.markdown_body,
                eml_path=eml_path,
                html_path=html_path,
                md_path=md_path,
                manifest_path=manifest_path,
                snapshot_path=snapshot_path,
                quality_matrix_md_path=quality_matrix_md_path,
                quality_matrix_json_path=quality_matrix_json_path,
                remediation_md_path=remediation_md_path,
                remediation_json_path=remediation_json_path,
                overrides_path=ctx.overrides_path,
                narratives_dir=ctx.narratives_dir,
                review_status_path=render_state.review_status_path,
                kusto_sections=render_state.kusto_sections,
                warnings=validation_state.warnings,
                draft_readiness=validation_state.draft_readiness,
                email_subject=render_state.email_subject,
                email_preheader=render_state.email_preheader,
                subject_signal=render_state.subject_signal,
                diff_summary=diff_summary,
                adaptive_card_paths=adaptive_card_paths,
                persona_signal_coverage_path=persona_signal_coverage_path,
                workstream_snapshot_md_path=workstream_snapshot_md_path,
                workstream_snapshot_json_path=workstream_snapshot_json_path,
                workstream_associations_json_path=workstream_associations_json_path,
                continuation_contract_path=continuation_contract_path,
                title=render_state.title,
            ),
        )


def _maybe_auto_run_workiq_enrich(
    *,
    edition_name: str,
    dry_run: bool,
    offline: bool,
    show_progress: bool,
) -> None:
    """ADF-W1.5 / INV-ADF-2: report never performs WorkIQ NL discovery inline
    (historical XPF WorkIQ p50 latency >65min). A legacy
    ``workiq_enrich_schedule: pre_report`` config no longer triggers a live
    call here; the operator is told to run ``vertex enrich`` out-of-band."""
    if offline:
        return

    resolved = resolve_edition(edition_name, programs_root=PROGRAMS_ROOT)
    if resolved is None:
        return
    m365 = resolved.program.m365
    schedule = (m365.workiq_enrich_schedule or "").strip().lower() if m365 is not None else ""
    if not m365 or not m365.enabled or schedule != "pre_report":
        return

    # ADF-W5.8: this branch's own condition IS Section 8.2.5's "WorkIQ inline
    # invocation attempted" category -- fires regardless of --show-progress.
    try:
        append_or_suppress_alert(
            program_id=resolved.program.id, category="workiq_inline_invocation_attempted",
            entity_type="edition", entity_id=edition_name, severity="warn",
            message=f"{edition_name}'s workiq_enrich_schedule is 'pre_report' (INV-ADF-2 violation).",
            next_command=f"vertex enrich --edition {edition_name}", programs_root=PROGRAMS_ROOT,
        )
    except (OSError, StateError):
        pass

    if show_progress:
        typer.echo(
            "[WorkIQ] workiq_enrich_schedule: pre_report is configured but report no longer runs WorkIQ "
            "inline (INV-ADF-2). Run 'vertex enrich --edition "
            f"{edition_name}' separately (e.g. on a schedule) before report."
        )


def apply_overrides_command(
    edition: str = typer.Option(..., "--edition", help="Edition name, for example acme_weekly."),
) -> None:
    report_dir = get_program_output_dir(edition, programs_root=PROGRAMS_ROOT)
    overrides_path = apply_pending_overrides(edition, report_dir, reports_root=REPORTS_ROOT)
    if overrides_path is None:
        typer.echo("No pending overrides found. Run 'vertex report --dry-run --edition <name>' first.")
        raise typer.Exit(code=2)
    typer.echo(f"Applied overrides: {overrides_path}")

def _next_issue_number(index: Any) -> int:
    if not index.issues:
        return 1
    return max(entry.issue_number for entry in index.issues) + 1

