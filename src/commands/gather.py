from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
import json
import logging
import os

# Feature flag for chart gather — mirrors kusto_rendering.py
_VERTEX_CHARTS_ENABLED = os.environ.get("VERTEX_CHARTS", "1") == "1"
from pathlib import Path
from time import monotonic, perf_counter
from typing import Any, Callable
import uuid
from uuid import NAMESPACE_URL, uuid5

import typer

from src.commands.channel_wiring import resolve_channel_config
from src.commands.gather_pipeline.ado_pipeline_stage import load_pipeline_signals as _load_pipeline_signals
from src.commands.gather_pipeline.ado_pipeline_stage import pull_request_entity_refs as _pull_request_entity_refs
from src.commands.gather_pipeline.ado_pipeline_stage import pull_request_provider_ref as _pull_request_provider_ref
from src.commands.gather_pipeline.ado_pipeline_stage import summarize_pull_requests as _summarize_pull_requests
from src.commands.gather_pipeline.ado_analytics_primitives import (
    date_from_sk as _date_from_sk,
    date_to_sk as _date_to_sk,
    is_completed_state as _is_completed_state,
    parse_date_sk as _parse_date_sk,
)
from src.commands.gather_pipeline.ado_kpi_stage import build_kusto_kpi_signals as _build_kusto_kpi_signals_impl
from src.commands.gather_pipeline.ado_kpi_stage import execute_ado_pr_kpi_query as _execute_ado_pr_kpi_query_impl
from src.commands.gather_pipeline.ado_kpi_stage import execute_wiql_kpi_query as _execute_wiql_kpi_query_impl
from src.commands.gather_pipeline.ado_signal_builder_stage import build_analytics_signals as _build_analytics_signals_impl
from src.commands.gather_pipeline.ado_signal_builder_stage import build_sprint_signals as _build_sprint_signals_impl
from src.commands.gather_pipeline.ado_snapshot_stage import load_analytics_signals as _load_analytics_signals_impl
from src.commands.gather_pipeline.ado_snapshot_stage import load_sprint_signals as _load_sprint_signals_impl
from src.commands.gather_pipeline.ado_wiql_stage import load_wiql_golden_query_signals as _load_wiql_golden_query_signals_impl
from src.commands.gather_pipeline.ado_wiql_stage import record_ado_wiql_query_state as _record_ado_wiql_query_state_impl
from src.commands.gather_pipeline.ado_wiql_stage import resolve_wiql_query_text as _resolve_wiql_query_text_impl
from src.commands.gather_pipeline import (
    BackgroundSynthesisRunner,
    GatherArtifacts,
    M365DiscoveryStageInput,
    M365PromotionBlockedArtifact,
    M365PromotionCandidate,
    PersistenceStageInput,
    ProjectionStageInput,
    StateWriteStageInput,
    build_gather_channel_states as _stage_build_gather_channel_states,
    build_uil_ado_channel_state as _stage_build_uil_ado_channel_state,
    build_uil_channel_state as _stage_build_uil_channel_state,
    compute_and_persist_plane1_changes as _stage_compute_and_persist_plane1_changes,
    run_m365_discovery_stage,
    run_persistence_stage,
    run_projection_stage,
    run_state_write_stage,
)
from src.commands.gather_pipeline.run_telemetry_wiring import (
    build_run_telemetry_accumulator as _build_run_telemetry_accumulator,
    observe_step as _observe_run_telemetry_step,
    record_run_telemetry_for_gather as _record_run_telemetry_for_gather,
)
from src.commands.gather_pipeline.m365_discovery_stage import (
    build_current_m365_promotion_blocked_artifacts as _stage_build_current_m365_promotion_blocked_artifacts,
    build_current_m365_promotion_candidates as _stage_build_current_m365_promotion_candidates,
    build_m365_discovery_state as _stage_build_m365_discovery_state,
)
from src.commands.gather_pipeline.projection_stage import (
    background_synthesis_enabled,
    trajectory_point_from_item as _projection_trajectory_point_from_item,
)
from src.commands.gather_pipeline.kusto_query_helpers import build_kusto_signal as _build_kusto_signal_impl
from src.commands.gather_pipeline.kusto_query_helpers import build_kusto_signals as _build_kusto_signals_impl
from src.commands.gather_pipeline.kusto_query_helpers import record_kusto_query_state as _record_kusto_query_state_impl
from src.commands.gather_pipeline.icm_signal_stage import build_icm_query_signals as _build_icm_query_signals_impl
from src.commands.gather_pipeline.icm_signal_stage import build_icm_signals as _build_icm_signals_impl
from src.commands.gather_pipeline.icm_signal_stage import prefer_agency_icm as _prefer_agency_icm_impl
from src.commands.gather_pipeline.dependency_stage import _DependencyQueryItems
from src.commands.gather_pipeline.dependency_stage import build_dependency_signals as _build_dependency_signals_impl
from src.commands.gather_pipeline.dependency_stage import load_dependency_program_items as _load_dependency_program_items_impl
from src.commands.gather_pipeline.hypothesis_stage import run_hypothesis_proposer_stage as _run_hypothesis_proposer_stage_impl
from src.commands.gather_pipeline.query_loader_stage import load_ado_wiql_queries as _load_ado_wiql_queries_impl
from src.commands.gather_pipeline.query_loader_stage import load_kusto_queries as _load_kusto_queries_impl
from src.commands.gather_pipeline.slice_contract_helpers import slice_contract_saved_query_clauses as _slice_contract_saved_query_clauses_impl
from src.commands.gather_pipeline.source_ingestion_stage import build_signal_ingestion_captured_window as _build_signal_ingestion_captured_window_impl
from src.commands.gather_pipeline.source_ingestion_stage import build_signal_source_ingestion_run_id as _build_signal_source_ingestion_run_id_impl
from src.commands.gather_pipeline.source_ingestion_stage import record_optional_source_ingestion_runs as _record_optional_source_ingestion_runs_impl
from src.commands.gather_pipeline.support import (
    build_captured_window as _build_captured_window,
    confidence_from_string as _confidence_from_string,
    coerce_datetime as _coerce_datetime,
    coerce_datetime_or_none as _coerce_datetime_or_none,
    enrich_resources as _enrich_resources,
    hash_ingestion_query_text as _hash_ingestion_query_text,
    kusto_event_timestamp as _kusto_event_timestamp,
    kusto_kpi_value as _kusto_kpi_value,
    parse_date as _parse_date,
    roll_query_value_history as _roll_query_value_history,
    sanitize_discovery_result as _sanitize_discovery_result,
)
from src.commands.gather_pipeline.kpi_projection_stage import project_kpi_signals_to_observations as _project_kpi_signals_to_observations_impl
from src.commands.gather_pipeline.kpi_projection_stage import project_refresh_kpi_signals_to_observations as _project_refresh_kpi_signals_to_observations_impl
from src.commands.gather_pipeline.uil_loader_stage import load_ado_items_via_uil as _load_ado_items_via_uil_impl
from src.commands.gather_pipeline.uil_loader_stage import load_kusto_signals_via_uil as _load_kusto_signals_via_uil_impl
from src.commands.gather_pipeline.uil_loader_stage import load_signal_channel_via_uil as _load_signal_channel_via_uil_impl
from src.commands.gather_pipeline.m365_workstream_profile_stage import augment_m365_workstream_profiles as _augment_m365_workstream_profiles_impl
from src.commands.gather_pipeline.channel_runtime import run_channel as _run_channel_impl
from src.commands.gather_pipeline.channel_runtime import run_channel_with_extraction as _run_channel_with_extraction_impl
from src.commands.gather_pipeline.uil_binding_stage import resolve_uil_channel_binding_for_gather as _resolve_uil_channel_binding_for_gather_impl
from src.commands.gather_pipeline.workiq_prefetch_stage import resolve_workiq_signals as _resolve_workiq_signals
from src.commands.gather_workiq_helpers import WorkIQQueryPlan as _WorkIQQueryPlan, _truncate_signal_text, apply_structured_workiq_discovery as _apply_structured_workiq_discovery, build_workiq_fragment_signal_text as _build_workiq_fragment_signal_text, build_workiq_signal_text as _build_workiq_signal_text, extract_work_item_refs as _extract_work_item_refs, workiq_fragment_message_id as _workiq_fragment_message_id, workiq_message_id as _workiq_message_id, workiq_signal_fragments as _workiq_signal_fragments, workiq_source_type as _workiq_source_type, workiq_timestamp as _workiq_timestamp
from src.ai.m365_topic_router import M365TopicRouter, M365TopicRouterError
from src.core.ado_enrichment import ADO_ANALYTICS_HISTORY_FIELDS, ADO_CHILD_BATCH_FIELDS, ADO_RISK_ASSESSMENT_COMMENT_FIELD, ADO_RISK_ASSESSMENT_FIELD, build_analytics_history, build_child_work_items, build_significant_findings, extract_child_ids_by_parent, infer_ado_risk_level, normalize_risk_assessment, serialize_trajectory_points
from src.commands.gather_pipeline.provider_facade import ADOClient
from src.commands.gather_auxiliary import (
    archive_stale_weekly_journal_files as _archive_stale_weekly_journal_files,
    build_engms_signals as _build_engms_signals,
    build_integration_error_signal as _build_integration_error_signal,
    compute_adaptive_window_days as _compute_adaptive_window_days,
    run_sharepoint_ingest as _run_sharepoint_ingest,
)
from src.commands.gather_record_helpers import (
    build_revision_signal_text as _build_revision_signal_text,
    field_value as _field_value,
    infer_risk_level as _infer_risk_level,
    is_echo_chamber_comment as _record_is_echo_chamber_comment,
    is_echo_chamber_revision as _record_is_echo_chamber_revision,
    load_freshness_thresholds as _load_freshness_thresholds,
    parse_datetime as _parse_datetime,
    parse_identity as _parse_identity,
    parse_tags as _parse_tags,
    read_recent_signals as _read_recent_signals,
    record_workiq_provenance as _record_workiq_provenance,
    resolve_icm_workstream_id as _resolve_icm_workstream_id,
    tracked_field_name as _tracked_field_name,
    trajectory_point_from_item as _trajectory_point_from_item,
    vertex_service_identities as _vertex_service_identities,
)
from src.core.ado_discovery import expand_with_linked_items
from src.core.analytics_store import replace_contradiction_state
from src.core.channel_registry_store import ChannelRegistryStore, ShrinkageGuardError, compute_registry_delta, normalize_discovery_result_provider_instance
from src.core.program_paths import (
    get_channel_registry_path,
    resolve_channel_registry_path_for_read,
)
from src.core.claim_tracker import load_open_claims
from src.core.contradiction_engine import build_contradiction_packets
from src.core.decision_extractor_basic import extract_decisions_from_signals
from src.core.decision_register import upsert_decisions
from src.core.dependency_scout import load_dependency_proposals, merge_dependency_proposals, save_dependency_proposals, scout_dependency_proposals
from src.core.discovery_intent import DiscoveryAttempt, DiscoveryAttemptOutcome, SourceCandidate, SourceCandidateStatus, SourceIntent, SourceIntentStatus, SourceRefKind, build_discovery_attempt_id, build_source_candidate_id, normalize_intent_display_name
from src.core.discovery_resolution import ResolutionContext, passes_auto_resolution_gate
from src.core.discovery_service import (
    accept_candidate_and_resolve_intent as _accept_candidate_and_resolve_intent,
    channel_for_source_ref_kind as _service_channel_for_source_ref_kind,
    persist_seeded_source_discovery as _persist_seeded_source_discovery,
    registry_artifact_discovered_id as _service_registry_artifact_discovered_id,
    seeded_candidate_match_origin as _service_seeded_candidate_match_origin,
    seeded_source_attempt_outcome as _service_seeded_source_attempt_outcome,
    seeded_source_attempt_reason as _service_seeded_source_attempt_reason,
    select_seeded_source_auto_resolve_candidate as _service_select_seeded_source_auto_resolve_candidate,
)
from src.core.alerts import read_alerts as _read_alerts
from src.core.alerts import surface_alert_banner as _surface_alert_banner
from src.core.edition_resolver import PROGRAMS_ROOT, _parse_program
from src.commands.gather_pipeline.lifecycle_policy import (
    freshness_state,
    load_gather_runtime_policy,
)
from src.core.feedback.calibration_router import load_forecast_calibration_modifier
from src.core.freshness_engine import build_freshness_report
from src.core.gather_channel_support import (
    append_integration_error_once as _append_integration_error_once,
    binding_provider_instance_id as _binding_provider_instance_id,
    build_integration_error as _build_integration_error,
)
from src.core.gather_run_manifest import (
    ACTOR_IDENTITY_INTERACTIVE,
    ChannelOutcomeEntry,
    GATHER_MUTATION_DOMAIN,
    FailedRefEntry,
    GatherRunManifest,
    GatherRunStatus,
    QueryResultEntry,
    RequiredScopeStatus,
    commit_staging_run,
    create_staging_manifest,
    fail_staging_run,
    get_staging_run_dir,
    hash_ado_items,
    hash_query_results,
    quarantine_abandoned_staging_runs,
    resolve_latest_committed_manifest,
    resolve_latest_full_committed_manifest,
    resolve_oracle_result,
    write_ado_items,
    write_query_results_sidecar,
)
from src.core.gather_state_store import load_gather_state
from src.core.ledger.ulid import new_ulid
from src.core.workspace_lease import (
    LeaseFencingTokenStale,
    LeaseHeldByAnotherOwner,
    LeaseRenewalHeartbeat,
    LeaseRenewalFailed,
    acquire_lease,
    release_lease,
)
from src.core.incident_journal_store import append_incident_entry
from src.core.journal import archive_weekly_journal_files, archive_weekly_journal_files_by_retention, get_week_key
from src.core.journal_retention import load_signal_retention_policy
from src.core.knowledge_store import load_program_knowledge
from src.core.kusto_client import build_live_kusto_query_executor, build_live_kusto_query_probe
from src.core.kusto_query_loader import load_kpi_queries
from src.core.kusto_ref_utils import extract_kusto_entity_refs as _extract_kusto_entity_refs
from src.core.keyword_topic_router import KeywordM365TopicRouter, suggest_keyword_expansions
from src.core.m365_discovery_support import (
    RegistryIdCandidate,
    build_m365_discovery_queries as _core_build_m365_discovery_queries,
    build_m365_discovery_query as _core_build_m365_discovery_query,
    build_m365_discovery_query_for_workstream as _core_build_m365_discovery_query_for_workstream,
    build_workstream_match_aliases,
    candidate_match_score,
    normalize_match_text,
    registry_keywords_for_workstream as _core_registry_keywords_for_workstream,
    rank_registry_id_candidates,
    tokenize_match_text,
    use_match_aliases,
)
from src.core.m365_payload_support import (
    optional_string as _optional_string,
    sender_alias as _sender_alias,
    workiq_participant_aliases as _workiq_participant_aliases,
    workiq_payload_records as _workiq_payload_records,
    workiq_preview as _workiq_preview,
    workiq_sender as _workiq_sender,
    workiq_subject as _workiq_subject,
    workiq_thread_id as _workiq_thread_id,
)
from src.core.m365_router_interface import IM365TopicRouter, M365ReassignCorrection
from src.core.context_gap_store import append_context_gap
from src.core.exceptions import AuthError, ConfigError, CredentialExpired, QueryError, StateError
from src.core.leakage_detector import LeakageReport, detect_leakage, load_approved_workiq_signals
from src.core.m365_signal_corpus import build_m365_corpus_texts_by_workstream, build_m365_reassign_corrections_by_workstream, build_m365_rejected_texts_by_workstream, load_approved_m365_corpus_signals
from src.core.m365_identifiers import normalize_meeting_id, normalize_thread_id
from src.core.m365_registry_store import M365RegistryArtifact, _artifact_meets_auto_promotion_confidence_gate, build_auto_meeting_artifact_id, build_auto_thread_artifact_id, describe_current_m365_registry_promotion_blockers, ensure_m365_registry_bootstrap, is_current_m365_registry_promotion_candidate, load_m365_registry, read_m365_routing_feedback_events, refresh_m365_registry_metrics, tracked_registry_thread_ids, upsert_m365_registry_artifacts
from src.core.metric_models import MetricObservation
from src.core.integration_types import ChannelRegistration, DiscoveredRef, HydrationMode, RegistrationBinding, RegistrationStatus, RunContext
from src.core.models import Comment, Confidence, Revision, RiskLevel, SnapshotItem, WorkItem
from src.core.program_fact_store import (
    load_current_action_items,
    load_current_dependencies, load_current_milestones,
    load_current_risk_entries,
    load_current_workstreams,
    load_program_facts,
    persist_program_fact_snapshot,
    project_action_items,
    project_assumptions,
    project_decision_entries,
    project_dependencies,
    project_milestones,
    project_risk_entries,
    project_workstreams,
)
from src.core.plane1_changelog import append_plane1_changes, compute_plane1_changes, load_plane1_last_seen, shadow_write_plane1_snapshot, write_plane1_last_seen, build_plane1_snapshot
from src.core.models_v2 import AIProposalStatus, ActionItem, IncidentEntry, IntegrationError, KustoQuery, Milestone, Program, ReviewPolicy, Signal, SignalReviewDecision, Team, TrajectoryPoint, VitalityAggregate, VitalityScore, Workstream
from src.core.observability import RunLoggerAdapter, configure_file_logging, get_command_trace_path
from src.core.reality_store import RealityStore
from src.core.signal_review import signal_can_be_auto_approved, signal_is_approved_for_evidence, compute_auto_approval_policies, write_autonomy_audit_entries
from src.core.signal_ref_utils import merge_entity_refs, widen_ws_wi_refs
from src.core.signal_dedup import dedupe_signals, dedupe_signals_with_audit
from src.core.source_candidate_store import (
    CANDIDATE_REJECTION_SUPPRESSION_DAYS,
    SourceCandidateStore,
    candidate_evidence_json,
)
from src.core.source_intent_audit import append_intent_decision_log, intent_decision_payload
from src.core.engms_signal_extractor import EngMsSignalExtractor, hashes_from_artifacts
from src.core.store_factory import build_program_signal_store, build_program_trajectory_store, build_signal_store, build_trajectory_store
from src.core.uil_channel_flags import (
    UIL_CHANNEL_ENABLED_FUNCS as _UIL_CHANNEL_ENABLED_FUNCS,
    UIL_CHANNEL_ENV_FLAGS as _UIL_CHANNEL_ENV_FLAGS,
    uil_ado_enabled as _uil_ado_enabled,
    uil_channel_enabled as _uil_channel_enabled,
    uil_teams_enabled as _uil_teams_enabled,
)
from src.core.slice_contract_loader import SliceContract, load_slice_contract
from src.core.workstream_path_resolver import resolve_workstream_id_loose_longest as _resolve_workstream_id
from src.core.yaml_utils import load_yaml_mapping
from src.m365.agency_bridge import AgencyBridge, AgencyCapabilities
from src.m365.autonomous_registry_discovery import run_m365_discovery_pass as _m365_run_discovery_pass
from src.m365.discovery_diagnostics import describe_discovery_unavailable_reason
from src.commands.gather_pipeline.provider_facade import create_graph_calendar_client, create_graph_mail_client
from src.m365.registry_id_discovery import (
    discover_email_thread_candidates as _discover_email_thread_candidates,
    discover_meeting_id_candidates as _discover_meeting_id_candidates,
    discover_thread_id_candidates as _discover_thread_id_candidates,
)
from src.m365.teams_reader import TeamsReader
from src.m365.transcript_reader import TranscriptReader
from src.m365.workiq_ask_support import validate_structured_discovery_payload


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
_WORK_ITEM_BATCH_SIZE = 200
_SNAPSHOT_ITEM_FILTER_BATCH_SIZE = 50
_ADO_WIQL_TOP_CAP = 10000         # Saved-query WIQL can exceed 2k IDs; warn if truncated at this higher cap
_ADO_ITEM_COUNT_WARN_THRESHOLD = 5000  # log WARNING if OData filter matches more than this
_VERTEX_COMMENT_PREFIX = "📊 Vertex"
_FRESHNESS_SIGNAL_RULE_IDS = {"FR-21", "FR-22", "FR-43", "FR-46"}
_DEFAULT_WEEKLY_JOURNAL_RETENTION_WEEKS = 52
_DEFAULT_KUSTO_EXPECTED_MAX_AGE_HOURS = 24
_DEFAULT_ADO_ANALYTICS_EXPECTED_MAX_AGE_HOURS = 48
_M365_DISCOVERY_RESULT_LIMIT = 50
_SEEDED_SOURCE_DISCOVERY_LIMIT = 10
_SEEDED_SOURCE_ATTEMPT_TTL_HOURS = 24
_ADAPTIVE_WORKIQ_LEARNED_KEYWORD_LIMIT = 3
# Structural source-type labels that must not become adaptive search keywords —
# they are mined from artifact display names ("SCHIE Chat") but carry no content.
_ADAPTIVE_KEYWORD_STOPWORDS = frozenset({
    "chat", "chats", "channel", "channels", "mail", "email", "emails",
    "thread", "threads", "meeting", "meetings", "series", "sync", "weekly",
    "review", "reviews", "standup", "stand-up",
})
_ADAPTIVE_WORKIQ_EXPLORATION_TERM_LIMIT = 3

log = logging.getLogger(__name__)

GatherLoader = Callable[[Program, tuple[Workstream, ...], datetime], tuple[tuple[WorkItem, ...], int]]
BridgeFactory = Callable[[], AgencyBridge]
KustoQueryExecutor = Callable[[KustoQuery], list[dict[str, Any]]]
IcmClientFactory = Callable[..., Any]
AnalyticsSignalLoader = Callable[[Program, tuple[Workstream, ...], datetime], tuple[tuple[Signal, ...], int]]
SprintSignalLoader = Callable[..., tuple[tuple[Signal, ...], int]]
PipelineSignalLoader = Callable[..., tuple[tuple[Signal, ...], int]]
AIActionExtractor = Callable[[Program, tuple[Signal, ...]], tuple[ActionItem, ...]]

_ANALYTICS_SNAPSHOT_FIELDS = (
    "DateSK",
    "WorkItemId",
    "WorkItemType",
    "Title",
    "State",
    "AreaPath",
    "CompletedDateSK",
    "CycleTimeDays",
    "LeadTimeDays",
)

_TRAJECTORY_BACKFILL_WINDOW_DAYS = 180

_SPRINT_SNAPSHOT_FIELDS = (
    "DateSK",
    "WorkItemId",
    "State",
    "AreaPath",
    "IterationPath",
)

@dataclass(frozen=True, slots=True)
class GatherProgressEvent:
    step_index: int
    step_count: int
    step_name: str
    elapsed_seconds: float
    detail: str





GatherProgressCallback = Callable[[GatherProgressEvent], None]


_GATHER_CADENCE_PROFILES: dict[str, dict[str, bool]] = {
    "daily": {
        "workiq": False,
        "kusto": False,
        "analytics": False,
        "sprints": False,
        "pipelines": False,
        "icm": True,
        "dependency_scout": False,
        "sharepoint": False,
    },
    "weekly": {
        "workiq": True,
        "kusto": True,
        "analytics": True,
        "sprints": False,
        "pipelines": False,
        "icm": True,
        "dependency_scout": False,
        "sharepoint": True,  # SP1-1: auto-enable SharePoint gather on weekly cadence
    },
}


def _parse_source_export_counts(entries: list[str]) -> dict[str, int]:
    """D-19/AG-2.12: parse repeated ``--source-export <scope_id>=<count>``
    values into a ``{scope_id: count}`` map. Raises ``typer.BadParameter`` on
    a malformed entry (missing ``=`` or a non-integer count) so a typo fails
    fast rather than silently recording a wrong reconciliation.
    """
    counts: dict[str, int] = {}
    for entry in entries:
        scope_id, sep, raw_count = entry.partition("=")
        if not sep or not scope_id:
            raise typer.BadParameter(
                f"Invalid --source-export value '{entry}'. Expected '<scope_id>=<count>'."
            )
        try:
            counts[scope_id] = int(raw_count)
        except ValueError as error:
            raise typer.BadParameter(
                f"Invalid --source-export count for scope '{scope_id}': '{raw_count}' is not an integer."
            ) from error
    return counts


def _resolve_gather_flags(
    *,
    cadence: str | None,
    include_workiq: bool,
    include_kusto: bool,
    include_analytics: bool,
    include_sprints: bool,
    include_pipelines: bool,
    include_icm: bool,
    include_dependency_scout: bool,
    include_engms: bool = False,
    include_sharepoint: bool = False,
    include_lt_deck: bool = False,
    force_refresh: bool = False,
) -> dict[str, bool]:
    if cadence is None:
        profile: dict[str, bool] = {}
    else:
        normalized_cadence = cadence.strip().lower()
        _profile = _GATHER_CADENCE_PROFILES.get(normalized_cadence)
        if _profile is None:
            supported = ", ".join(sorted(_GATHER_CADENCE_PROFILES))
            raise typer.BadParameter(f"Unsupported cadence '{cadence}'. Expected one of: {supported}.")
        profile = _profile
    return {
        "workiq": profile.get("workiq", False) or include_workiq,
        "kusto": profile.get("kusto", False) or include_kusto,
        "analytics": profile.get("analytics", False) or include_analytics,
        "sprints": profile.get("sprints", False) or include_sprints,
        "pipelines": profile.get("pipelines", False) or include_pipelines,
        "icm": profile.get("icm", False) or include_icm,
        "dependency_scout": profile.get("dependency_scout", False) or include_dependency_scout,
        "engms": profile.get("engms", False) or include_engms,
        "sharepoint": profile.get("sharepoint", False) or include_sharepoint,  # SP1-1
        "lt_deck": include_lt_deck,   # SP1-1: no cadence profile — explicit flag only
        "force_refresh": force_refresh,  # SP1-1: bypass change detection
    }


def gather_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    cadence: str | None = typer.Option(None, "--cadence", help="Optional source profile: daily or weekly."),
    workiq: bool = typer.Option(False, "--workiq", help="Fetch WorkIQ evidence and append pending-review signals."),
    kusto: bool = typer.Option(False, "--kusto", help="Execute golden Kusto queries and append signals."),
    probe: bool = typer.Option(False, "--probe", help="Include unvalidated Kusto candidate queries during gather."),
    analytics: bool = typer.Option(False, "--analytics", help="Query ADO Analytics snapshots and append telemetry signals."),
    sprints: bool = typer.Option(False, "--sprints", help="Query current ADO iterations and append sprint summary signals."),
    pipelines: bool = typer.Option(False, "--pipelines", help="Query configured ADO pipeline runs and open pull requests and append auto-approved telemetry signals."),
    icm: bool = typer.Option(False, "--icm", help="Execute IcM incident queries and append auto-approved signals."),
    engms: bool = typer.Option(False, "--engms", help="Scan ADO work item descriptions for referenced eng.ms pages and append change signals."),
    sharepoint: bool = typer.Option(False, "--sharepoint", help="Gather SharePoint ref docs from engms_pages.yaml via WorkIQ."),
    lt_deck: bool = typer.Option(False, "--lt-deck", help="Also extract latest LT deck from program.yaml m365.sharepoint.lt_deck config."),
    force_refresh: bool = typer.Option(False, "--force-refresh", help="Bypass SharePoint change detection; re-extract all docs."),
    dependency_scout: bool = typer.Option(False, "--dependency-scout", help="Refresh dependency proposals from the current gather signals and trajectories."),
    verbose: bool = typer.Option(False, "--verbose", help="Write structured gather traces under publications/<program>/observability/."),
    facts_only: bool = typer.Option(False, "--facts-only", help="Skip full gather; only mirror current program facts into the fact store (FR-SG-61)."),
    extract_evidence: bool = typer.Option(False, "--extract-evidence", help="Run ContentExtractionAgent on transcript signals to populate WorkstreamEvidence. Requires --workiq. Off by default until validated."),
    force_discovery: bool = typer.Option(False, "--force-discovery", help="Sec 4.4: bypass the discovery-staleness check for UIL-backed channels (ado/kusto/teams/icm) and force discovery even if not yet due. Required after changing query bindings."),
    accept_shrinkage: bool = typer.Option(False, "--accept-shrinkage", help="Sec 4.4: accept a guarded registry shrinkage (>=30% removed, >=5 items) for UIL-backed channels this run instead of blocking the registry update; classified removals are printed."),
    source_export: list[str] = typer.Option(
        [],
        "--source-export",
        help="D-19/AG-2.12: record an operator-verified ADO source-export/UI membership count for a scope, as '<scope_id>=<count>'. Repeatable. Reconciles that scope's committed query_results entry beyond the weak same-endpoint rerun.",
    ),
) -> None:
    source_export_counts = _parse_source_export_counts(source_export)
    if facts_only:
        _now = datetime.now(timezone.utc)
        _programs_root = PROGRAMS_ROOT
        _compute_and_persist_plane1_changes(program, _programs_root, _now)
        typer.echo(f"Facts-only mirror complete for {program}.")
        raise typer.Exit(code=0)
    _gather_banner = _surface_alert_banner(program, programs_root=PROGRAMS_ROOT)
    if _gather_banner is not None:
        typer.echo(_gather_banner, err=True)
    trace_path = get_command_trace_path(program, "gather") if verbose else None
    trace_logger: RunLoggerAdapter | None = None
    if trace_path is not None:
        trace_logger = configure_file_logging(
            uuid.uuid4().hex,
            trace_path=trace_path,
            logger_name=f"vertex.gather.{program}",
        )
        trace_logger.info("gather command started", extra={"stage": "command", "program_id": program})

    def _emit_progress(event: GatherProgressEvent) -> None:
        typer.echo(f"[{event.step_index}/{event.step_count}] {event.step_name} {event.elapsed_seconds:.2f}s | {event.detail}")
        if trace_logger is not None:
            trace_logger.info(
                "gather stage completed",
                extra={
                    "stage": event.step_name,
                    "step_index": event.step_index,
                    "step_count": event.step_count,
                    "elapsed_seconds": round(event.elapsed_seconds, 3),
                    "detail": event.detail,
                },
            )

    resolved_flags = _resolve_gather_flags(
        cadence=cadence,
        include_workiq=workiq,
        include_kusto=kusto,
        include_analytics=analytics,
        include_sprints=sprints,
        include_pipelines=pipelines,
        include_icm=icm,
        include_dependency_scout=dependency_scout,
        include_engms=engms,
        include_sharepoint=sharepoint,
        include_lt_deck=lt_deck,
        force_refresh=force_refresh,
    )
    try:
        artifacts = gather_program(
            program,
            include_workiq=resolved_flags["workiq"],
            include_kusto=resolved_flags["kusto"],
            probe_kusto=resolved_flags["kusto"] and probe,
            include_analytics=resolved_flags["analytics"],
            include_sprints=resolved_flags["sprints"],
            include_pipelines=resolved_flags["pipelines"],
            include_icm=resolved_flags["icm"],
            include_dependency_scout=resolved_flags["dependency_scout"],
            include_engms=resolved_flags["engms"],
            include_sharepoint=resolved_flags["sharepoint"],
            include_lt_deck=resolved_flags["lt_deck"],
            force_refresh=resolved_flags["force_refresh"],
            extract_evidence=extract_evidence,
            progress_callback=_emit_progress,
            force_discovery=force_discovery,
            accept_shrinkage=accept_shrinkage,
            source_export_counts=source_export_counts,
        )
    except (GatherLeaseConflict, LeaseFencingTokenStale, LeaseRenewalFailed) as error:
        # D-7/AG-7.3: an overlapping or fenced-out gather is not a usage
        # error (Typer's default code 2); it is scheduler-actionable state.
        typer.echo(str(error), err=True)
        raise typer.Exit(code=4) from error
    typer.echo(
        f"Gathered {artifacts.discovered_signals} signals ({artifacts.new_signals} new, {artifacts.pending_review} pending review) for {artifacts.program_id}"
    )
    typer.echo(
        f"Scanned {artifacts.scanned_items} work items, wrote {artifacts.trajectory_updates} trajectory updates, {artifacts.auto_reviews_written} auto-review entries, {artifacts.ado_calls} ADO calls."
    )
    if artifacts.dependency_proposals_refreshed:
        typer.echo(f"Dependency scout refreshed {artifacts.dependency_proposals_refreshed} proposal(s).")
    if artifacts.archived_journal_files:
        typer.echo(f"Archived {artifacts.archived_journal_files} stale weekly journal file(s) before gather.")
    if artifacts.background_proposals:
        typer.echo(f"Auto-generated {artifacts.background_proposals} AI proposal(s) from gather triggers.")
    if artifacts.integration_errors:
        typer.echo(f"Optional integration failures: {artifacts.integration_error_count}")
        for detail in artifacts.integration_errors:
            action_suffix = f" | Next: {detail.operator_action}" if detail.operator_action else ""
            typer.echo(f"  - {detail.source}/{detail.stage}: {detail.message}{action_suffix}")
    for candidate in artifacts.promotion_candidates:
        display_name = candidate.display_name or candidate.artifact_id
        typer.echo(f"[PROMOTION CANDIDATE] artifact '{candidate.artifact_id}' ({display_name}) is ready for current promotion.")
        typer.echo(
            f"  workstream: {candidate.workstream_id}, confidence: {candidate.confidence:.2f}, yield: {list(candidate.signal_yield_last_3)}"
        )
        typer.echo(
            f"  Run 'vertex registry promote {candidate.artifact_id} --program {artifacts.program_id}' to add to workstreams.yaml."
        )
    for blocked_artifact in artifacts.promotion_blocked_artifacts:
        display_name = blocked_artifact.display_name or blocked_artifact.artifact_id
        typer.echo(
            f"[PROMOTION BLOCKED] artifact '{blocked_artifact.artifact_id}' ({display_name}) is not ready for current promotion."
        )
        typer.echo(
            f"  workstream: {blocked_artifact.workstream_id}, blocker: {blocked_artifact.blocker_reason}"
        )
    if trace_logger is not None and trace_path is not None:
        trace_logger.info(
            "gather command completed",
            extra={
                "stage": "command",
                "program_id": artifacts.program_id,
                "discovered_signals": artifacts.discovered_signals,
                "new_signals": artifacts.new_signals,
                "ado_calls": artifacts.ado_calls,
            },
        )
        typer.echo(f"Trace: {trace_path}")
    raise typer.Exit(code=_semantic_gather_exit_code(artifacts))


def _semantic_gather_exit_code(artifacts: GatherArtifacts) -> int:
    """D-7's successful-process outcome mapping.

    The ADO channel defines required delivery scope; all other currently
    optional integrations may degrade without invalidating the manual refresh.
    Framework exceptions deliberately remain exceptions (the scheduler treats
    their conventional non-zero exit as failed/non-current per D-7).
    """
    if _has_required_scope_degradation(artifacts):
        return 3
    if not artifacts.integration_errors:
        return 0
    return 2


class GatherLeaseConflict(typer.BadParameter):
    """A gather-domain lease conflict with D-7's scheduler exit semantics.

    This remains a ``typer.BadParameter`` subclass so Python/API callers keep
    the established validation-error surface, while ``gather_command`` can
    distinguish it from ordinary CLI argument errors and return exit 4.
    """


def _has_required_ado_degradation(artifacts: GatherArtifacts) -> bool:
    """ADO is the authoritative Armada delivery-scope channel.

    Treat every ADO integration error as required-scope degradation. A
    stage-name allowlist would inevitably miss a newly introduced ADO stage
    and silently promote a partial run to FULL.
    """
    return any(
        error.source == "ado"
        for error in artifacts.integration_errors
    )


def _has_required_scope_degradation(artifacts: GatherArtifacts) -> bool:
    """Required scope is degraded by ADO failure, incomplete capture, or skew."""
    capture_times = tuple(result.captured_at for result in artifacts.ado_query_results)
    capture_skew_exceeded = (
        len(capture_times) >= 2
        and (max(capture_times) - min(capture_times)).total_seconds() > 300
    )
    query_capture_incomplete = any(
        result.cap_reached
        or result.completeness_state != "FULL"
        or result.failure_category is not None
        for result in artifacts.ado_query_results
    )
    return _has_required_ado_degradation(artifacts) or query_capture_incomplete or capture_skew_exceeded


def _alert_ledger_state(
    program_id: str, *, programs_root: Path
) -> tuple[dict[str, tuple[int, datetime | None, datetime | None]], bool]:
    """Read the alert ledger without allowing observability I/O to block gather.

    The boolean is intentionally carried to the manifest: failure to observe
    alert lifecycle evidence is operationally meaningful, but it must not
    turn a successfully gathered authoritative scope into a failed run.
    """
    try:
        return (
            {
                record.alert_id: (record.occurrence_count, record.last_seen, record.resolved_at)
                for record in _read_alerts(program_id, programs_root=programs_root, include_resolved=True)
            },
            False,
        )
    except (OSError, StateError, ValueError):
        return {}, True


def _full_discovery_timestamp(manifest: GatherRunManifest | None) -> datetime | None:
    if manifest is None:
        return None
    return manifest.last_successful_full_discovery_at or manifest.last_query_captured_at or manifest.finished_at


def gather_program(
    program_id: str,
    *,
    as_of: datetime | None = None,
    programs_root: Path | None = None,
    loader: GatherLoader | None = None,
    freshness_loader: GatherLoader | None = None,
    include_workiq: bool = False,
    bridge_factory: BridgeFactory | None = None,
    m365_topic_router: IM365TopicRouter | None = None,
    include_kusto: bool = False,
    probe_kusto: bool = False,
    kusto_query_executor: KustoQueryExecutor | None = None,
    include_analytics: bool = False,
    analytics_signal_loader: AnalyticsSignalLoader | None = None,
    include_sprints: bool = False,
    sprint_signal_loader: SprintSignalLoader | None = None,
    include_pipelines: bool = False,
    pipeline_signal_loader: PipelineSignalLoader | None = None,
    include_icm: bool = False,
    icm_client_factory: IcmClientFactory | None = None,
    include_engms: bool = False,
    engms_extractor: EngMsSignalExtractor | None = None,
    include_sharepoint: bool = False,  # SP1-1
    include_lt_deck: bool = False,     # SP1-1
    force_refresh: bool = False,       # SP1-1
    sharepoint_pipeline_runner: Any | None = None,  # SP1-2 DI: injectable for tests
    include_dependency_scout: bool = False,
    background_synthesis_runner: BackgroundSynthesisRunner | None = None,
    ai_action_extractor: AIActionExtractor | None = None,
    extract_evidence: bool = False,
    progress_callback: GatherProgressCallback | None = None,
    force_discovery: bool = False,
    accept_shrinkage: bool = False,
    actor_identity_type: str = ACTOR_IDENTITY_INTERACTIVE,
    source_export_counts: dict[str, int] | None = None,
) -> GatherArtifacts:
    """D-13/Sec 4.6 lifecycle wrapper around the actual gather implementation.

    Before delegating: reconciles any ``gather_runs/staging`` manifests
    abandoned by a crashed prior process (D-13 rule 8), then acquires the
    program's ``gather`` workspace lease -- failing fast with a clear error
    if another process currently holds it (no silent queuing/retry) -- and
    creates a ``status=running`` manifest. After delegating: on success,
    populates the manifest's aggregate fields from the returned
    ``GatherArtifacts`` and commits it; on any exception (including the
    early config-validation ``typer.BadParameter`` raised below), fails the
    manifest instead. The lease is always released before returning or
    re-raising. See specs/armada.md D-13 through D-17 and Sec 4.6.

    Legacy injected loaders cannot provide authoritative discovery membership.
    They therefore commit as ``PARTIAL`` with ``discovered_count=0`` rather
    than inferring scope from hydration. UIL ADO gathers carry immutable
    per-query membership captures; all enabled source phases carry bounded
    channel outcomes. Per-connector retry/throttle counters remain a future
    enrichment where a connector does not expose those counters.

    ``source_export_counts`` (D-19/AG-2.12): an optional ``{scope_id: count}``
    map of operator-recorded sanitized ADO source-export counts, used to
    reconcile each scope's discovered ``raw_count`` against an
    explicitly-recorded external observation rather than only the weak
    same-endpoint rerun. Recorded per scope on ``query_results[].oracle_result``;
    never affects discovery/hydration behavior itself.
    """
    resolved_programs_root = programs_root or PROGRAMS_ROOT
    resolved_source_export_counts = source_export_counts or {}
    runtime_policy = load_gather_runtime_policy(
        program_id,
        programs_root=resolved_programs_root,
    )

    def run_implementation(gather_run_id: str | None) -> GatherArtifacts:
        return _gather_program_impl(
            program_id,
            as_of=as_of,
            programs_root=programs_root,
            loader=loader,
            freshness_loader=freshness_loader,
            include_workiq=include_workiq,
            bridge_factory=bridge_factory,
            m365_topic_router=m365_topic_router,
            include_kusto=include_kusto,
            probe_kusto=probe_kusto,
            kusto_query_executor=kusto_query_executor,
            include_analytics=include_analytics,
            analytics_signal_loader=analytics_signal_loader,
            include_sprints=include_sprints,
            sprint_signal_loader=sprint_signal_loader,
            include_pipelines=include_pipelines,
            pipeline_signal_loader=pipeline_signal_loader,
            include_icm=include_icm,
            icm_client_factory=icm_client_factory,
            include_engms=include_engms,
            engms_extractor=engms_extractor,
            include_sharepoint=include_sharepoint,
            include_lt_deck=include_lt_deck,
            force_refresh=force_refresh,
            sharepoint_pipeline_runner=sharepoint_pipeline_runner,
            include_dependency_scout=include_dependency_scout,
            background_synthesis_runner=background_synthesis_runner,
            ai_action_extractor=ai_action_extractor,
            extract_evidence=extract_evidence,
            progress_callback=progress_callback,
            force_discovery=force_discovery,
            accept_shrinkage=accept_shrinkage,
            gather_run_id=gather_run_id,
        )

    # D-24's off mode is an explicit compatibility path. It neither creates
    # lifecycle artifacts nor stamps records with a gather-run ID.
    if runtime_policy.run_manifest_mode == "off":
        return run_implementation(None)

    lifecycle_started_at = datetime.now(timezone.utc)
    quarantine_abandoned_staging_runs(
        program_id,
        finished_at=lifecycle_started_at,
        programs_root=resolved_programs_root,
    )

    lease_owner = f"vertex_gather:{uuid.uuid4().hex[:12]}"
    try:
        lease = acquire_lease(
            program_id,
            lease_owner,
            mutation_domain=GATHER_MUTATION_DOMAIN,
            programs_root=resolved_programs_root,
        )
    except LeaseHeldByAnotherOwner as exc:
        raise GatherLeaseConflict(
            f"Program '{program_id}' already has a gather running (lease held by "
            f"{exc.holder!r} until {exc.expires_at.isoformat()}). Wait for it to finish "
            "or expire before retrying."
        ) from exc

    alert_ledger_before, alert_tracking_failed = _alert_ledger_state(
        program_id, programs_root=resolved_programs_root
    )
    previous_committed_run = resolve_latest_committed_manifest(
        program_id, programs_root=resolved_programs_root
    )
    previous_full_run = resolve_latest_full_committed_manifest(
        program_id, programs_root=resolved_programs_root
    )

    run_id = f"gather-{new_ulid(lifecycle_started_at)}"
    running_manifest = GatherRunManifest(
        run_id=run_id,
        status=GatherRunStatus.RUNNING,
        program_id=program_id,
        actor_identity_type=actor_identity_type,
        lease_owner=lease.owner,
        lease_fencing_token=lease.fencing_token,
        started_at=lifecycle_started_at,
        scope_as_of=as_of or lifecycle_started_at,
        required_scope_status=RequiredScopeStatus.FULL,
    )
    create_staging_manifest(running_manifest, programs_root=resolved_programs_root)
    # D-13/AG-7.3: a gather can legitimately exceed the lease TTL while a
    # remote channel is bounded but slow. Renew in the background and retain
    # a synchronous pre-promotion fence so a superseded worker cannot publish
    # a manifest after a newer holder takes the gather domain.
    lease_heartbeat = LeaseRenewalHeartbeat(lease, programs_root=resolved_programs_root)
    lease_heartbeat.start()

    try:
        artifacts = run_implementation(run_id)
    except BaseException:
        try:
            fail_staging_run(
                running_manifest,
                finished_at=datetime.now(timezone.utc),
                programs_root=resolved_programs_root,
            )
        finally:
            # A failure recording the failure must not strand the lease.
            lease_heartbeat.stop()
            release_lease(lease_heartbeat.handle, programs_root=resolved_programs_root)
        raise

    # The immediate renewal is the promotion fence: it fails if a TTL expiry
    # allowed another process to acquire a newer fencing token while the
    # gather was still executing. Treat that loss exactly like any other
    # handled lifecycle failure: do not leave a runnable staging directory or
    # a background heartbeat behind.
    try:
        lease = lease_heartbeat.renew_now()
    except BaseException:
        try:
            fail_staging_run(
                running_manifest,
                finished_at=datetime.now(timezone.utc),
                programs_root=resolved_programs_root,
            )
        finally:
            lease_heartbeat.stop()
            release_lease(lease_heartbeat.handle, programs_root=resolved_programs_root)
        raise
    finished_at = datetime.now(timezone.utc)
    alert_ledger_after, post_gather_alert_tracking_failed = _alert_ledger_state(
        program_id, programs_root=resolved_programs_root
    )
    alert_tracking_failed = alert_tracking_failed or post_gather_alert_tracking_failed
    alert_ids = tuple(sorted(
        alert_id
        for alert_id, state in alert_ledger_after.items()
        if state != alert_ledger_before.get(alert_id)
    ))
    failed_refs = tuple(
        FailedRefEntry(ref_kind=error.source, ref_id=error.stage, reason=error.message)
        for error in artifacts.integration_errors
    )
    query_results = tuple(
        QueryResultEntry(
            query_id=result.query_id,
            scope_id=result.scope_id,
            wiql_hash=result.wiql_hash,
            captured_at=result.captured_at,
            raw_count=result.raw_count,
            membership_ids=result.membership_ids,
            membership_hash=result.membership_hash,
            cap_reached=result.cap_reached,
            completeness_state=result.completeness_state,
            oracle_result=resolve_oracle_result(
                result.scope_id, result.raw_count, resolved_source_export_counts
            ),
            failure_category=result.failure_category,
        )
        for result in artifacts.ado_query_results
    )
    query_capture_times = tuple(result.captured_at for result in query_results)
    first_query_captured_at = min(query_capture_times) if query_capture_times else None
    last_query_captured_at = max(query_capture_times) if query_capture_times else None
    query_capture_skew_seconds = (
        (last_query_captured_at - first_query_captured_at).total_seconds()
        if first_query_captured_at is not None and last_query_captured_at is not None
        else None
    )
    query_capture_skew_exceeded = (
        query_capture_skew_seconds is not None and query_capture_skew_seconds > 300
    )
    authoritative_discovery_captured = bool(query_results)
    required_scope_status = (
        RequiredScopeStatus.PARTIAL
        if (
            _has_required_scope_degradation(artifacts)
            or query_capture_skew_exceeded
            or not authoritative_discovery_captured
        )
        else RequiredScopeStatus.FULL
    )
    last_successful_full_discovery_at = (
        (last_query_captured_at or finished_at)
        if required_scope_status is RequiredScopeStatus.FULL
        else _full_discovery_timestamp(previous_full_run)
    )
    next_expected_run_at = (
        last_successful_full_discovery_at + timedelta(hours=runtime_policy.full_discovery_cadence_hours)
        if last_successful_full_discovery_at is not None
        else None
    )
    consecutive_failed_runs = (
        0
        if required_scope_status is RequiredScopeStatus.FULL
        else (previous_committed_run.consecutive_failed_runs if previous_committed_run is not None else 0) + 1
    )
    ado_items = [{"work_item_id": item_id} for item_id in artifacts.hydrated_work_item_ids]
    staging_dir = get_staging_run_dir(program_id, run_id, programs_root=resolved_programs_root)
    # The sidecars are written before the final manifest so the hashes cover
    # exactly the data atomically promoted with that manifest.
    write_ado_items(staging_dir, ado_items)
    write_query_results_sidecar(staging_dir, query_results)
    final_manifest = replace(
        running_manifest,
        required_scope_status=required_scope_status,
        first_query_captured_at=first_query_captured_at,
        last_query_captured_at=last_query_captured_at,
        query_capture_skew_seconds=query_capture_skew_seconds,
        query_results=query_results,
        discovered_count=len(artifacts.discovered_work_item_ids),
        hydrated_count=len(artifacts.hydrated_work_item_ids),
        ado_call_count=artifacts.ado_calls,
        failed_refs=failed_refs,
        channel_outcomes=tuple(artifacts.channel_outcomes),
        alert_ids=alert_ids,
        alert_delivery_failed=alert_tracking_failed,
        last_successful_full_discovery_at=last_successful_full_discovery_at,
        last_attempt_at=finished_at,
        next_expected_run_at=next_expected_run_at,
        consecutive_failed_runs=consecutive_failed_runs,
        freshness_state=freshness_state(
            last_successful_full_discovery_at=last_successful_full_discovery_at,
            # The stored state describes this immutable capture as-of its
            # declared scope. Consumers recompute it against their own clock.
            now=running_manifest.scope_as_of,
            warn_hours=runtime_policy.freshness_warn_hours,
            block_hours=runtime_policy.freshness_block_hours,
        ),
        cap_reached=any(result.cap_reached for result in query_results),
        ado_items_hash=hash_ado_items(ado_items),
        query_results_hash=hash_query_results(query_results),
        latency=(finished_at - lifecycle_started_at).total_seconds(),
    )
    # Promotion is filesystem I/O and may fail. Do not strand the
    # single-writer lease until its TTL expires; recovery will quarantine the
    # still-staged run on the next attempt.
    try:
        commit_staging_run(final_manifest, finished_at=finished_at, programs_root=resolved_programs_root)
    finally:
        lease_heartbeat.stop()
        release_lease(lease_heartbeat.handle, programs_root=resolved_programs_root)
    return artifacts


def _gather_program_impl(
    program_id: str,
    *,
    as_of: datetime | None = None,
    programs_root: Path | None = None,
    loader: GatherLoader | None = None,
    freshness_loader: GatherLoader | None = None,
    include_workiq: bool = False,
    bridge_factory: BridgeFactory | None = None,
    m365_topic_router: IM365TopicRouter | None = None,
    include_kusto: bool = False,
    probe_kusto: bool = False,
    kusto_query_executor: KustoQueryExecutor | None = None,
    include_analytics: bool = False,
    analytics_signal_loader: AnalyticsSignalLoader | None = None,
    include_sprints: bool = False,
    sprint_signal_loader: SprintSignalLoader | None = None,
    include_pipelines: bool = False,
    pipeline_signal_loader: PipelineSignalLoader | None = None,
    include_icm: bool = False,
    icm_client_factory: IcmClientFactory | None = None,
    include_engms: bool = False,
    engms_extractor: EngMsSignalExtractor | None = None,
    include_sharepoint: bool = False,  # SP1-1
    include_lt_deck: bool = False,     # SP1-1
    force_refresh: bool = False,       # SP1-1
    sharepoint_pipeline_runner: Any | None = None,  # SP1-2 DI: injectable for tests
    include_dependency_scout: bool = False,
    background_synthesis_runner: BackgroundSynthesisRunner | None = None,
    ai_action_extractor: AIActionExtractor | None = None,
    extract_evidence: bool = False,
    progress_callback: GatherProgressCallback | None = None,
    force_discovery: bool = False,
    accept_shrinkage: bool = False,
    gather_run_id: str | None = None,
) -> GatherArtifacts:
    resolved_programs_root = programs_root or PROGRAMS_ROOT
    # ADF-W2.12: one correlation id per gather cycle (mirrors report.py's
    # StageContext threading), so this run's fact writes can be traced back
    # to the cycle that produced them via OperationTrace.
    correlation_id = uuid.uuid4().hex
    program, workstreams = _load_program_context(program_id, resolved_programs_root)
    m365_workstream_profiles = _augment_m365_workstream_profiles(
        program_id,
        workstreams=workstreams,
        programs_root=resolved_programs_root,
    )
    signal_store = build_program_signal_store(program, programs_root=resolved_programs_root)
    trajectory_store = build_program_trajectory_store(program, programs_root=resolved_programs_root)
    if program.ado is None:
        raise typer.BadParameter(f"Program '{program_id}' is missing ado configuration.")

    progress_steps = _build_gather_progress_steps(
        include_workiq=include_workiq,
        include_kusto=include_kusto,
        include_analytics=include_analytics,
        include_sprints=include_sprints,
        include_pipelines=include_pipelines,
        include_icm=include_icm,
        include_dependency_scout=include_dependency_scout,
        include_background_synthesis=background_synthesis_enabled(program),
        include_engms=include_engms,
        include_sharepoint=include_sharepoint,
    )
    progress_index = 0
    integration_error_details: list[IntegrationError] = []
    query_states: dict[str, dict[str, Any]] = {}
    ado_discovery_results: list[Any] = []
    channel_outcomes: list[ChannelOutcomeEntry] = []
    workiq_signals: tuple[Signal, ...] = ()
    kusto_signals: tuple[Signal, ...] = ()
    kusto_kpi_signals: tuple[Signal, ...] = ()
    icm_signals: tuple[Signal, ...] = ()
    previous_gather_state = load_gather_state(program_id, programs_root=resolved_programs_root)
    previous_query_states = previous_gather_state.query_states if previous_gather_state is not None else {}
    run_telemetry_accumulator = _build_run_telemetry_accumulator()  # WS-17

    def _complete_progress_step(step_name: str, started_at: float, detail: str) -> None:
        nonlocal progress_index
        # WS-17: feed per-channel wall-time to the run_telemetry accumulator.
        _observe_run_telemetry_step(run_telemetry_accumulator, step_name=step_name, started_at=started_at)
        if progress_callback is None:
            return
        progress_index += 1
        progress_callback(
            GatherProgressEvent(
                step_index=progress_index,
                step_count=len(progress_steps),
                step_name=step_name,
                elapsed_seconds=perf_counter() - started_at,
                detail=detail,
            )
        )

    def _complete_progress_step_elapsed(step_name: str, elapsed_seconds: float, detail: str) -> None:
        nonlocal progress_index
        if progress_callback is None:
            return
        progress_index += 1
        progress_callback(
            GatherProgressEvent(
                step_index=progress_index,
                step_count=len(progress_steps),
                step_name=step_name,
                elapsed_seconds=elapsed_seconds,
                detail=detail,
            )
        )

    def _record_channel_outcome(
        channel: str,
        started_at: float,
        *,
        error_start_index: int,
        ado_call_count: int = 0,
    ) -> None:
        """Append one bounded manifest outcome for a direct source phase.

        UIL providers record their own outcome. Direct phases are observed at
        this orchestration boundary so every enabled source has the same
        operational visibility without changing connector interfaces.
        """
        if any(entry.channel == channel for entry in channel_outcomes):
            return
        new_errors = tuple(
            error.message
            for error in integration_error_details[error_start_index:]
            if error.source == channel
        )
        channel_outcomes.append(
            ChannelOutcomeEntry(
                channel=channel,
                degraded=bool(new_errors),
                degrade_reason=new_errors[0] if new_errors else None,
                elapsed_seconds=perf_counter() - started_at,
                ado_call_count=ado_call_count,
            )
        )

    current_time = as_of or datetime.now(timezone.utc)
    gather_started_at = datetime.now(timezone.utc)  # WS-17: anchor run_telemetry
    gather_v2_enabled = _gather_v2_enabled()
    if program.m365 is not None and program.m365.enabled:
        ensure_m365_registry_bootstrap(
            program_id,
            workstreams=workstreams,
            programs_root=resolved_programs_root,
            as_of=current_time,
        )
    prepare_started_at = perf_counter()
    archived_journal_paths = _archive_stale_weekly_journal_files(
        program_id,
        as_of=current_time,
        programs_root=resolved_programs_root,
        default_retention_weeks=_DEFAULT_WEEKLY_JOURNAL_RETENTION_WEEKS,
    )
    signal_window_start = current_time - timedelta(
        days=_compute_adaptive_window_days(
            program_id,
            signal_store=signal_store,
            as_of=current_time,
            default_days=program.ado.date_window_days,
        )
    )
    _complete_progress_step(
        "prepare",
        prepare_started_at,
        f"workstreams={len(workstreams)}, archived={len(archived_journal_paths)}",
    )

    fetch_started_at = perf_counter()
    fetch_error_start = len(integration_error_details)
    ado_uil_binding = (
        _resolve_uil_channel_binding_for_gather(program, workstreams, "ado", programs_root=resolved_programs_root)
        if loader is None
        else None
    )
    if loader is None and ado_uil_binding is not None:
        items, freshness_items, uil_ado_calls = _load_ado_items_via_uil(
            program,
            workstreams,
            current_time,
            since=signal_window_start,
            programs_root=resolved_programs_root,
            binding=ado_uil_binding,
            integration_error_sink=integration_error_details,
            gather_v2_enabled=gather_v2_enabled,
            force_discovery=force_discovery,
            accept_shrinkage=accept_shrinkage,
            discovery_result_sink=ado_discovery_results,
            channel_outcome_sink=channel_outcomes,
        )
        ado_calls = uil_ado_calls
        freshness_ado_calls = 0
    elif loader is None:
        items, freshness_items, ado_calls, freshness_ado_calls = (), (), 0, 0
    else:
        items, ado_calls = loader(program, workstreams, current_time)
    if freshness_loader is not None:
        freshness_items, freshness_ado_calls = freshness_loader(program, workstreams, current_time)
    elif loader is not None:
        freshness_items, freshness_ado_calls = loader(program, workstreams, current_time)
    stale_warn_days, stale_block_days = _load_freshness_thresholds(program_id, resolved_programs_root)
    existing_signals = _read_recent_signals(
        program_id,
        start=signal_window_start,
        end=current_time,
        programs_root=resolved_programs_root,
        signal_store=signal_store,
    )
    if loader is not None:
        _record_channel_outcome(
            "ado",
            fetch_started_at,
            error_start_index=fetch_error_start,
            ado_call_count=ado_calls + freshness_ado_calls,
        )
    _complete_progress_step(
        "fetch",
        fetch_started_at,
        f"items={len(items)}, freshness_items={len(freshness_items)}, ado_calls={ado_calls + freshness_ado_calls}",
    )

    signals_started_at = perf_counter()
    ado_revision_signals = _build_ado_revision_signals(
        items,
        program_id=program_id,
        workstreams=workstreams,
        since=signal_window_start,
    )
    ado_comment_signals = _build_ado_comment_signals(
        items,
        program_id=program_id,
        workstreams=workstreams,
        since=signal_window_start,
    )
    freshness_signals = _build_freshness_signals(
        freshness_items,
        program_id=program_id,
        workstreams=workstreams,
        as_of=current_time,
        stale_warn_days=stale_warn_days,
        stale_block_days=stale_block_days,
    )
    dependency_items, dependency_ado_calls = _load_dependency_program_items(
        program,
        workstreams,
        current_time,
    )
    dependency_signals = _build_dependency_signals(
        dependency_items,
        program_id=program_id,
        workstreams=workstreams,
        as_of=current_time,
        stale_warn_days=stale_warn_days,
        stale_block_days=stale_block_days,
        query_state_sink=query_states,
        previous_query_states=previous_query_states,
    )
    candidate_signals = (*ado_revision_signals, *ado_comment_signals, *freshness_signals, *dependency_signals)
    _complete_progress_step(
        "signals",
        signals_started_at,
        f"ado={len(ado_revision_signals)}, comments={len(ado_comment_signals)}, freshness={len(freshness_signals)}, dependency={len(dependency_signals)}",
    )

    if include_workiq:
        workiq_started_at = perf_counter()
        workiq_error_start = len(integration_error_details)
        # Bound the *whole* WorkIQ phase (all query plans combined) by the
        # program's configured retrieval budget -- without this, a live run
        # with N query plans could take up to WORKIQ_TIMEOUT (or the slower
        # ~90-180s CLI fallback) *per plan* with no overall cap, stalling
        # gather for tens of minutes. Falls back to WorkIQRetrievalConfig's
        # own default (600s) when the program has no explicit m365.retrieval.
        workiq_retrieval = program.m365.retrieval if program.m365 is not None else None
        workiq_total_budget_seconds = (
            workiq_retrieval.max_wall_clock_seconds if workiq_retrieval is not None else 600
        )
        try:
            # Per-program match aliases (sourced from workstreams.yaml) canonicalize
            # program-specific abbreviations during discovery matching; core stays generic.
            with use_match_aliases(build_workstream_match_aliases(m365_workstream_profiles)):
                # ADF-W1.5/W1.10 remainder (Section 10.6): prefer an unexpired
                # committed `vertex prefetch` snapshot over a live WorkIQ call.
                workiq_signals = _resolve_workiq_signals(
                    program_id=program_id,
                    programs_root=resolved_programs_root,
                    live_fetch_fn=lambda: _build_workiq_signals(
                        program=program,
                        program_id=program_id,
                        as_of=current_time,
                        items=items,
                        workstreams=m365_workstream_profiles,
                        bridge=(bridge_factory or AgencyBridge),
                        m365_topic_router=m365_topic_router,
                        programs_root=resolved_programs_root,
                        integration_error_sink=integration_error_details,
                        # Cap each individual plan at AgencyBridge's own designed
                        # per-call ceiling so the *first* plan can't silently
                        # consume the entire total_budget_seconds as its own
                        # timeout (leaving nothing for the remaining plans).
                        timeout_seconds=AgencyBridge.WORKIQ_TIMEOUT,
                        total_budget_seconds=workiq_total_budget_seconds,
                    ),
                )
        except (AuthError, QueryError, TimeoutError, typer.BadParameter) as exc:
            _emit_credential_expired_banner(exc, "workiq")
            integration_error_details.append(_build_integration_error(source="workiq", stage="gather", error=str(exc)))
            workiq_signals = (
                _build_integration_error_signal(
                    program_id=program_id,
                    source="workiq",
                    error=str(exc),
                    as_of=current_time,
                ),
            )
            log.warning("WorkIQ gather failed for %s: %s", program_id, exc)
        if resolved_programs_root is not None and workiq_signals:
            _record_workiq_provenance(
                workiq_signals=workiq_signals,
                program_id=program_id,
                programs_root=resolved_programs_root,
                run_at=current_time,
            )
        if extract_evidence and resolved_programs_root is not None:
            from src.commands.gather_pipeline.evidence_extraction_stage import run_evidence_extraction_stage
            from src.m365.agency_bridge import AgencyBridge as _AgencyBridge
            _extraction_bridge = (bridge_factory or _AgencyBridge)()
            _extraction_started_at = perf_counter()
            extraction_results = run_evidence_extraction_stage(
                workiq_signals=workiq_signals,
                program_id=program_id,
                programs_root=resolved_programs_root,
                ask_ai_fn=lambda prompt: _extraction_bridge.ask_workiq(prompt),  # type: ignore[arg-type, return-value]
                as_of=current_time,
            )
            _complete_progress_step("evidence_extract", _extraction_started_at, f"lanes={len(extraction_results)}")
        candidate_signals = (
            *candidate_signals,
            *workiq_signals,
        )
        _record_channel_outcome(
            "workiq",
            workiq_started_at,
            error_start_index=workiq_error_start,
        )
        _complete_progress_step("workiq", workiq_started_at, f"signals={len(workiq_signals)}")
    if _uil_teams_enabled():
        teams_uil_started_at = perf_counter()
        teams_error_start = len(integration_error_details)
        teams_uil_count = 0
        try:
            teams_uil_binding = _resolve_uil_channel_binding_for_gather(
                program,
                workstreams,
                "teams",
                programs_root=resolved_programs_root,
            )
            if teams_uil_binding is not None:
                teams_uil_signals, _ = _load_teams_signals_via_uil(
                    program,
                    workstreams,
                    current_time,
                    programs_root=resolved_programs_root,
                    binding=teams_uil_binding,
                    integration_error_sink=integration_error_details,
                    gather_v2_enabled=gather_v2_enabled,
                    force_discovery=force_discovery,
                    accept_shrinkage=accept_shrinkage,
                )
                candidate_signals = (*candidate_signals, *teams_uil_signals)
                teams_uil_count = len(teams_uil_signals)
        except Exception as exc:
            # Teams UIL is an auxiliary channel. Preserve the error as an explicit
            # gather signal instead of aborting ADO-backed source truth collection.
            integration_error_details.append(_build_integration_error(source="teams", stage="gather", error=str(exc)))
            log.warning("Teams UIL gather failed for %s: %s", program_id, exc)
        _record_channel_outcome(
            "teams",
            teams_uil_started_at,
            error_start_index=teams_error_start,
        )
        # Emit the progress step exactly once whenever the channel is enabled so the
        # registered step count (built by _build_gather_progress_steps) stays in lockstep
        # with emissions across the binding-absent and failure paths.
        _complete_progress_step("teams_uil", teams_uil_started_at, f"signals={teams_uil_count}")
    if include_kusto:
        kusto_started_at = perf_counter()
        kusto_error_start = len(integration_error_details)
        try:
            kusto_uil_binding = _resolve_uil_channel_binding_for_gather(
                program,
                workstreams,
                "kusto",
                programs_root=resolved_programs_root,
            )
            if kusto_uil_binding is not None:
                kusto_signals, _ = _load_kusto_signals_via_uil(
                    program,
                    workstreams,
                    current_time,
                    programs_root=resolved_programs_root,
                    binding=kusto_uil_binding,
                    integration_error_sink=integration_error_details,
                    kusto_query_executor=kusto_query_executor,
                    include_unvalidated=probe_kusto,
                    query_state_sink=query_states,
                    previous_query_states=previous_query_states,
                    gather_v2_enabled=gather_v2_enabled,
                    force_discovery=force_discovery,
                    accept_shrinkage=accept_shrinkage,
                )
            else:
                if kusto_query_executor is None:
                    live_kusto_queries = tuple(
                        dict.fromkeys(
                            query
                            for query in (
                                *_load_kusto_queries(
                                    program_id,
                                    program=program,
                                    programs_root=resolved_programs_root,
                                    workstreams=workstreams,
                                    apply_signal_source_scope=True,
                                    filter_for_gather=True,
                                    include_unvalidated=probe_kusto,
                                ),
                                *_load_refresh_kpi_queries(
                                    program_id,
                                    programs_root=resolved_programs_root,
                                    include_unvalidated=probe_kusto,
                                    engine="kusto",
                                ),
                            )
                            if not _is_icm_query(query)
                        )
                    )
                    if live_kusto_queries:
                        failed_targets = build_live_kusto_query_probe()(live_kusto_queries)
                        if failed_targets:
                            for cluster, db in sorted(failed_targets):
                                integration_error_details.append(
                                    _build_integration_error(
                                        source="kusto",
                                        stage="probe",
                                        error=(
                                            f"Kusto pre-flight failed for {cluster}/{db}. "
                                            "Run 'vertex admin auth setup' and verify cluster access or JIT."
                                        ),
                                    )
                                )
                            # Remove queries on inaccessible clusters so the rest can proceed.
                            live_kusto_queries = tuple(
                                q for q in live_kusto_queries
                                if (q.cluster.strip(), q.database.strip()) not in failed_targets
                            )
                            # Wrap executor to silently skip queries on failed clusters.
                            kusto_query_executor = _make_cluster_filtered_executor(
                                kusto_query_executor or build_live_kusto_query_executor(),
                                failed_targets,
                            )
                kusto_signals = _build_kusto_signals(
                    program=program,
                    program_id=program_id,
                    programs_root=resolved_programs_root,
                    as_of=current_time,
                    workstreams=workstreams,
                    executor=kusto_query_executor or build_live_kusto_query_executor(),
                    filter_for_gather=True,
                    include_unvalidated=probe_kusto,
                    query_state_sink=query_states,
                    previous_query_states=previous_query_states,
                )
            kusto_kpi_signals = _build_kusto_kpi_signals(
                program=program,
                program_id=program_id,
                programs_root=resolved_programs_root,
                as_of=current_time,
                workstreams=workstreams,
                executor=kusto_query_executor or build_live_kusto_query_executor(),
                include_unvalidated=probe_kusto,
                query_state_sink=query_states,
                previous_query_states=previous_query_states,
            )
        except (AuthError, QueryError, TimeoutError, typer.BadParameter) as exc:
            _emit_credential_expired_banner(exc, "kusto")
            integration_error_details.append(_build_integration_error(source="kusto", stage="gather", error=str(exc)))
            kusto_signals = (
                _build_integration_error_signal(
                    program_id=program_id,
                    source="kusto",
                    error=str(exc),
                    as_of=current_time,
                ),
            )
            kusto_kpi_signals = ()
            log.warning("Kusto gather failed for %s: %s", program_id, exc)
        candidate_signals = (
            *candidate_signals,
            *kusto_signals,
            *kusto_kpi_signals,
        )
        _record_channel_outcome(
            "kusto",
            kusto_started_at,
            error_start_index=kusto_error_start,
        )
        _complete_progress_step("kusto", kusto_started_at, f"signals={len(kusto_signals) + len(kusto_kpi_signals)}")

    # Phase 2: Chart gather — independent of kusto signal gathering
    chart_gather_results: tuple[Any, ...] = ()
    if _VERTEX_CHARTS_ENABLED:
        chart_started_at = perf_counter()
        chart_error_start = len(integration_error_details)
        try:
            from src.commands.chart_gather import gather_chart_data

            chart_results, chart_errors = gather_chart_data(
                program_id,
                programs_root=resolved_programs_root,
                program=program,
                workstreams=workstreams,
                executor=kusto_query_executor,  # may be None if --kusto was not set
                current_time=current_time,
                integration_error_sink=integration_error_details,  # type: ignore[arg-type]
            )
            chart_gather_results = chart_results
            # chart_errors already appended to integration_error_sink inside gather_chart_data
        except (OSError, ValueError, RuntimeError, AttributeError, KeyError, TypeError) as exc:
            # Chart gather is best-effort: Kusto + file I/O + PNG rendering can fail in
            # many ways; preserve all errors in the sink without aborting ADO source truth.
            log.warning("Chart gather failed for %s: %s", program_id, exc)
            integration_error_details.append(
                _build_integration_error(source="charts", stage="gather", error=str(exc))
            )
        _record_channel_outcome(
            "charts",
            chart_started_at,
            error_start_index=chart_error_start,
        )
        _complete_progress_step(
            "chart_gather",
            chart_started_at,
            f"charts={len(chart_gather_results)}",
        )

    analytics_ado_calls = 0
    if include_analytics:
        analytics_started_at = perf_counter()
        analytics_error_start = len(integration_error_details)
        try:
            if analytics_signal_loader is None:
                analytics_signals, analytics_ado_calls = _load_analytics_signals(
                    program,
                    workstreams,
                    current_time,
                    programs_root=resolved_programs_root,
                    query_state_sink=query_states,
                    previous_query_states=previous_query_states,
                )
            else:
                analytics_signals, analytics_ado_calls = analytics_signal_loader(
                    program,
                    workstreams,
                    current_time,
                )
        except (QueryError, TimeoutError, typer.BadParameter) as exc:
            integration_error_details.append(_build_integration_error(source="analytics", stage="gather", error=str(exc)))
            analytics_signals = (
                _build_integration_error_signal(
                    program_id=program_id,
                    source="analytics",
                    error=str(exc),
                    as_of=current_time,
                ),
            )
            log.warning("ADO Analytics gather failed for %s: %s", program_id, exc)
        candidate_signals = (*candidate_signals, *analytics_signals)
        _record_channel_outcome(
            "analytics",
            analytics_started_at,
            error_start_index=analytics_error_start,
            ado_call_count=analytics_ado_calls,
        )
        _complete_progress_step(
            "analytics",
            analytics_started_at,
            f"signals={len(analytics_signals)}, ado_calls={analytics_ado_calls}",
        )
    sprint_ado_calls = 0
    if include_sprints:
        sprints_started_at = perf_counter()
        sprints_error_start = len(integration_error_details)
        sprint_signals, sprint_ado_calls = (sprint_signal_loader or _load_sprint_signals)(
            program,
            workstreams,
            freshness_items,
            current_time,
                query_state_sink=query_states,
                previous_query_states=previous_query_states,
        )
        candidate_signals = (*candidate_signals, *sprint_signals)
        _record_channel_outcome(
            "sprints",
            sprints_started_at,
            error_start_index=sprints_error_start,
            ado_call_count=sprint_ado_calls,
        )
        _complete_progress_step(
            "sprints",
            sprints_started_at,
            f"signals={len(sprint_signals)}, ado_calls={sprint_ado_calls}",
        )
    pipeline_ado_calls = 0
    if include_pipelines:
        pipelines_started_at = perf_counter()
        pipelines_error_start = len(integration_error_details)
        pipeline_signals, pipeline_ado_calls = (pipeline_signal_loader or _load_pipeline_signals)(
            program,
            workstreams,
            current_time,
            query_state_sink=query_states,
            previous_query_states=previous_query_states,
        )
        candidate_signals = (*candidate_signals, *pipeline_signals)
        _record_channel_outcome(
            "pipelines",
            pipelines_started_at,
            error_start_index=pipelines_error_start,
            ado_call_count=pipeline_ado_calls,
        )
        _complete_progress_step(
            "pipelines",
            pipelines_started_at,
            f"signals={len(pipeline_signals)}, ado_calls={pipeline_ado_calls}",
        )
    if include_icm:
        icm_started_at = perf_counter()
        icm_error_start = len(integration_error_details)
        try:
            icm_uil_binding = _resolve_uil_channel_binding_for_gather(
                program,
                workstreams,
                "icm",
                programs_root=resolved_programs_root,
            )
            if icm_uil_binding is not None:
                icm_signals, _ = _load_icm_signals_via_uil(
                    program,
                    workstreams,
                    current_time,
                    programs_root=resolved_programs_root,
                    binding=icm_uil_binding,
                    integration_error_sink=integration_error_details,
                    gather_v2_enabled=gather_v2_enabled,
                    force_discovery=force_discovery,
                    accept_shrinkage=accept_shrinkage,
                )
            else:
                icm_signals = _build_icm_signals(
                    program=program,
                    program_id=program_id,
                    programs_root=resolved_programs_root,
                    as_of=current_time,
                    workstreams=workstreams,
                    executor=kusto_query_executor or build_live_kusto_query_executor(),
                    bridge=(bridge_factory or AgencyBridge),
                    icm_client_factory=icm_client_factory,
                )
        except (AuthError, QueryError, TimeoutError, typer.BadParameter) as exc:
            _emit_credential_expired_banner(exc, "icm")
            integration_error_details.append(_build_integration_error(source="icm", stage="gather", error=str(exc)))
            icm_signals = (
                _build_integration_error_signal(
                    program_id=program_id,
                    source="icm",
                    error=str(exc),
                    as_of=current_time,
                ),
            )
            log.warning("IcM gather failed for %s: %s", program_id, exc)
        candidate_signals = (
            *candidate_signals,
            *icm_signals,
        )
        _record_channel_outcome(
            "icm",
            icm_started_at,
            error_start_index=icm_error_start,
        )
        _complete_progress_step("icm", icm_started_at, f"signals={len(icm_signals)}")
    if include_engms:
        engms_started_at = perf_counter()
        engms_error_start = len(integration_error_details)
        engms_signals, engms_hash_state = _build_engms_signals(
            items=items,
            program_id=program_id,
            previous_query_states=previous_query_states,
            extractor=engms_extractor,
            integration_error_sink=integration_error_details,
        )
        query_states["engms"] = engms_hash_state
        candidate_signals = (*candidate_signals, *engms_signals)
        _record_channel_outcome(
            "engms",
            engms_started_at,
            error_start_index=engms_error_start,
        )
        _complete_progress_step("engms", engms_started_at, f"signals={len(engms_signals)}")
    if include_sharepoint:  # SP1-1/SP1-2/SP1-3
        sp_started_at = perf_counter()
        sharepoint_error_start = len(integration_error_details)
        sp_result = _run_sharepoint_ingest(
            program_id=program_id,
            programs_root=resolved_programs_root,
            existing_signals=existing_signals,
            signal_store=signal_store,
            previous_gather_state=previous_gather_state,
            include_lt_deck=include_lt_deck,
            force_refresh=force_refresh,
            as_of=current_time,
            pipeline_runner=sharepoint_pipeline_runner,
            integration_error_sink=integration_error_details,
        )
        if sp_result is not None:
            query_states["sharepoint"] = {
                "last_run": current_time.isoformat().replace("+00:00", "Z"),
                "signals_created": sp_result.signals_created,
                "docs_processed": sp_result.docs_processed,
            }
        _record_channel_outcome(
            "sharepoint",
            sp_started_at,
            error_start_index=sharepoint_error_start,
        )
        _complete_progress_step(
            "sharepoint",
            sp_started_at,
            f"signals={sp_result.signals_created if sp_result else 0}",
        )
    persist_started_at = perf_counter()
    persistence_result = run_persistence_stage(
        PersistenceStageInput(
            program=program,
            program_id=program_id,
            workstreams=workstreams,
            candidate_signals=candidate_signals,
            existing_signals=existing_signals,
            signal_store=signal_store,
            current_time=current_time,
            programs_root=resolved_programs_root,
            ai_action_extractor=ai_action_extractor,
            correlation_id=correlation_id,
            gather_run_id=gather_run_id,
        )
    )
    new_signals = persistence_result.new_signals
    auto_reviews_written = persistence_result.auto_reviews_written
    pending_review = persistence_result.pending_review

    _record_optional_source_ingestion_runs(
        program_id=program_id,
        as_of=current_time,
        programs_root=resolved_programs_root,
        include_workiq=include_workiq,
        include_analytics=include_analytics,
        include_sprints=include_sprints,
        include_pipelines=include_pipelines,
        include_icm=include_icm,
        signals=new_signals,
        integration_error_details=tuple(integration_error_details),
    )

    _project_kpi_signals_to_observations(
        program_id,
        programs_root=resolved_programs_root,
        as_of=current_time,
        kpi_signals=kusto_kpi_signals,
        query_states=query_states,
        include_unvalidated=probe_kusto,
    )

    # Must run after signal_store.append() so the contradiction engine reads the
    # post-persistence signal set rather than the pre-dedupe baseline.
    _refresh_contradiction_state(
        program_id,
        items=items,
        workstreams=workstreams,
        signal_store=signal_store,
        signal_window_start=signal_window_start,
        as_of=current_time,
        programs_root=resolved_programs_root,
    )

    _complete_progress_step(
        "persist",
        persist_started_at,
        f"new_signals={len(new_signals)}, actions={persistence_result.extracted_action_count}, pending_review={pending_review}",
    )

    projection_result = run_projection_stage(
        ProjectionStageInput(
            program=program,
            program_id=program_id,
            workstreams=workstreams,
            items=items,
            signal_store=signal_store,
            trajectory_store=trajectory_store,
            as_of=current_time,
            programs_root=resolved_programs_root,
            include_dependency_scout=include_dependency_scout,
            background_synthesis_runner=background_synthesis_runner,
            resolve_workstream_id=_resolve_workstream_id,
        )
    )
    trajectory_updates = projection_result.trajectory_updates
    dependency_proposals_refreshed = projection_result.dependency_proposals_refreshed
    background_proposals = projection_result.background_proposals
    _complete_progress_step_elapsed(
        "trajectories",
        projection_result.trajectory_elapsed_seconds,
        projection_result.trajectory_detail,
    )
    if projection_result.dependency_detail is not None and projection_result.dependency_elapsed_seconds is not None:
        _complete_progress_step_elapsed(
            "dependencies",
            projection_result.dependency_elapsed_seconds,
            projection_result.dependency_detail,
        )
    if projection_result.synthesis_detail is not None and projection_result.synthesis_elapsed_seconds is not None:
        _complete_progress_step_elapsed(
            "synthesis",
            projection_result.synthesis_elapsed_seconds,
            projection_result.synthesis_detail,
        )

    proposed_hypotheses = _run_hypothesis_proposers(
        program_id,
        programs_root=resolved_programs_root,
        proposed_at=current_time,
    )

    total_ado_calls = ado_calls + freshness_ado_calls + dependency_ado_calls + analytics_ado_calls + sprint_ado_calls + pipeline_ado_calls
    gather_flags = {
        "workiq": include_workiq,
        "kusto": include_kusto,
        "analytics": include_analytics,
        "sprints": include_sprints,
        "pipelines": include_pipelines,
        "icm": include_icm,
        "dependency_scout": include_dependency_scout,
        "engms": include_engms,
        "sharepoint": include_sharepoint,  # SP1-1
        "lt_deck": include_lt_deck,
    }
    channel_states = _build_gather_channel_states(
        program_id=program_id,
        programs_root=resolved_programs_root,
        workstreams=workstreams,
        ado_signals=(*ado_revision_signals, *ado_comment_signals, *freshness_signals, *dependency_signals),
        kusto_signals=(*kusto_signals, *kusto_kpi_signals),
        workiq_signals=workiq_signals if include_workiq else (),
        icm_signals=icm_signals if include_icm else (),
        gather_flags=gather_flags,
        previous_channels=previous_gather_state.channels if previous_gather_state is not None else None,
        integration_error_details=tuple(integration_error_details),
    )
    m365_discovery_result = run_m365_discovery_stage(
        M365DiscoveryStageInput(
            program=program,
            program_id=program_id,
            workstreams=m365_workstream_profiles,
            items=items,
            workiq_signals=workiq_signals if include_workiq else (),
            gather_flags=gather_flags,
            integration_error_details=tuple(integration_error_details),
            as_of=current_time,
            previous_entry=previous_gather_state.m365_discovery if previous_gather_state is not None else None,
            programs_root=resolved_programs_root,
            count_transcript_series_state=_count_transcript_series_state,
            count_chat_thread_state=_count_chat_thread_state,
            tracked_m365_artifact_ids=_tracked_m365_artifact_ids,
            observed_m365_thread_ids=_observed_m365_thread_ids,
            load_discovery_milestones=lambda stage_program_id, stage_programs_root: _load_discovery_milestones(
                stage_program_id,
                programs_root=stage_programs_root,
            ),
            build_workiq_query_plans=lambda **kwargs: tuple(_build_workiq_query_plans(**kwargs)),
            build_m365_discovery_queries=lambda **kwargs: tuple(_build_m365_discovery_queries(**kwargs)),
            build_seeded_source_discovery_state=lambda *, program_id, programs_root, as_of: _build_seeded_source_discovery_state(
                program_id=program_id,
                programs_root=programs_root,
                as_of=as_of,
            ),
            build_adaptive_workiq_state=lambda **kwargs: _build_adaptive_workiq_state(**kwargs),
        )
    )
    promotion_candidates = m365_discovery_result.promotion_candidates
    m365_discovery_state = m365_discovery_result.m365_discovery_state
    ado_query_results = tuple(
        query_result
        for discovery_result in ado_discovery_results
        for query_result in getattr(discovery_result, "query_results", ())
    )
    discovered_work_item_ids = tuple(sorted({
        ref.registration.ref_id
        for discovery_result in ado_discovery_results
        for ref in getattr(discovery_result, "discovered_refs", ())
        if ref.registration.ref_kind == "work_item"
    }, key=int))
    hydrated_work_item_ids = tuple(sorted({str(item.id) for item in items}, key=int))
    finalize_started_at = perf_counter()
    state_write_result = run_state_write_stage(
        StateWriteStageInput(
            program_id=program_id,
            gathered_at=current_time,
            scanned_items=len(items),
            discovered_signals=len(candidate_signals),
            new_signals=len(new_signals),
            pending_review=pending_review,
            trajectory_updates=trajectory_updates,
            auto_reviews_written=auto_reviews_written,
            ado_calls=total_ado_calls,
            archived_journal_files=len(archived_journal_paths),
            background_proposals=background_proposals,
            dependency_proposals_refreshed=dependency_proposals_refreshed,
            integration_error_details=tuple(integration_error_details),
            gather_flags=gather_flags,
            channels=channel_states,
            m365_discovery=m365_discovery_state,
            previous_gathered_at=previous_gather_state.gathered_at if previous_gather_state is not None else None,
            previous_query_states=previous_query_states,
            previous_channels=previous_gather_state.channels if previous_gather_state is not None else None,
            previous_m365_discovery=previous_gather_state.m365_discovery if previous_gather_state is not None else None,
            query_states=query_states,
            programs_root=resolved_programs_root,
            promotion_candidates=promotion_candidates,
            promotion_blocked_artifacts=m365_discovery_result.promotion_blocked_artifacts,
            chart_results=chart_gather_results,
            hypothesis_count=len(proposed_hypotheses),
            correlation_id=correlation_id,
            ado_query_results=ado_query_results,
            discovered_work_item_ids=discovered_work_item_ids,
            hydrated_work_item_ids=hydrated_work_item_ids,
            channel_outcomes=tuple(channel_outcomes),
        )
    )
    _complete_progress_step(
        "finalize",
        finalize_started_at,
        state_write_result.finalize_detail,
    )
    # WS-17: emit per-channel run_telemetry record (never blocks gather).
    _record_run_telemetry_for_gather(
        program_id=program_id,
        programs_root=resolved_programs_root,
        accumulator=run_telemetry_accumulator,
        started_at=gather_started_at,
        include_workiq=include_workiq,
        include_kusto=include_kusto,
        include_icm=include_icm,
    )
    return state_write_result.artifacts


def _refresh_contradiction_state(
    program_id: str,
    *,
    items: tuple[WorkItem, ...],
    workstreams: tuple[Workstream, ...],
    signal_store: Any,
    signal_window_start: datetime,
    as_of: datetime,
    programs_root: Path,
) -> None:
    review_states = signal_store.read_reviews(program_id)
    approved_signals = tuple(
        signal
        for signal in signal_store.read(program_id, start=signal_window_start, end=as_of)
        if signal_is_approved_for_evidence(signal, review_states)
    )
    packets = build_contradiction_packets(
        items=items,
        claims=load_open_claims(program_id, programs_root=programs_root),
        signals=approved_signals,
        workstreams=workstreams,
        as_of=as_of,
        calibration_modifier=load_forecast_calibration_modifier(program_id, programs_root=programs_root),
        dependencies=load_current_dependencies(program_id, programs_root=programs_root),
        risks=load_current_risk_entries(program_id, programs_root=programs_root),
        milestones=load_current_milestones(program_id, programs_root=programs_root),
        actions=load_current_action_items(program_id, programs_root=programs_root),
    )
    replace_contradiction_state(program_id, packets, programs_root=programs_root)


def _run_hypothesis_proposers(
    program_id: str,
    *,
    programs_root: Path,
    proposed_at: datetime,
    reality_store: RealityStore | None = None,
) -> tuple[object, ...]:
    store = reality_store or _build_reality_store(program_id, programs_root=programs_root)
    return _run_hypothesis_proposer_stage_impl(
        program_id,
        store=store,
        claims=load_open_claims(program_id, programs_root=programs_root),
        proposed_at=proposed_at,
    )


def _emit_credential_expired_banner(exc: BaseException, source: str) -> None:
    """Emit an [ACTION REQUIRED] banner to stderr when a credential has expired.

    Called from each gather channel exception handler. Non-CredentialExpired
    exceptions are silently ignored so callers need not pre-check the type.
    """
    if not isinstance(exc, CredentialExpired):
        return
    connector = exc.connector or source.upper()
    auth_method = exc.auth_method or "unknown"
    typer.echo(
        f"[ACTION REQUIRED] {connector} credential expired (auth: {auth_method}). "
        f"Renew the token and re-run gather. "
        f"Run: vertex doctor --adapter-cert",
        err=True,
    )


def _build_gather_progress_steps(
    *,
    include_workiq: bool,
    include_kusto: bool,
    include_analytics: bool,
    include_sprints: bool,
    include_pipelines: bool,
    include_icm: bool,
    include_dependency_scout: bool,
    include_background_synthesis: bool,
    include_engms: bool = False,
    include_sharepoint: bool = False,  # SP1-1
) -> tuple[str, ...]:
    steps = ["prepare", "fetch", "signals"]
    if include_workiq:
        steps.append("workiq")
    if _uil_teams_enabled():
        steps.append("teams_uil")
    if include_kusto:
        steps.append("kusto")
    if _VERTEX_CHARTS_ENABLED:
        steps.append("chart_gather")
    if include_analytics:
        steps.append("analytics")
    if include_sprints:
        steps.append("sprints")
    if include_pipelines:
        steps.append("pipelines")
    if include_icm:
        steps.append("icm")
    if include_engms:
        steps.append("engms")
    if include_sharepoint:
        steps.append("sharepoint")  # SP1-1
    steps.extend(["persist", "trajectories"])
    if include_dependency_scout:
        steps.append("dependencies")
    if include_background_synthesis:
        steps.append("synthesis")
    steps.append("finalize")
    return tuple(steps)


def _compute_and_persist_plane1_changes(
    program_id: str,
    programs_root: Path,
    gathered_at: datetime,
) -> None:
    _stage_compute_and_persist_plane1_changes(
        program_id,
        programs_root,
        gathered_at,
        load_program_facts=load_program_facts,
        project_milestones=project_milestones,
        project_risk_entries=project_risk_entries,
        project_decision_entries=project_decision_entries,
        project_assumptions=project_assumptions,
        project_workstreams=project_workstreams,
        load_plane1_last_seen=load_plane1_last_seen,
        compute_plane1_changes=compute_plane1_changes,
        append_plane1_changes=append_plane1_changes,
        build_plane1_snapshot=build_plane1_snapshot,
        shadow_write_plane1_snapshot=shadow_write_plane1_snapshot,
        persist_program_fact_snapshot=persist_program_fact_snapshot,
        write_plane1_last_seen=write_plane1_last_seen,
    )


def _load_program_context(program_id: str, programs_root: Path) -> tuple[Program, tuple[Workstream, ...]]:
    program_dir = programs_root / program_id
    if not program_dir.exists():
        raise typer.BadParameter(f"Unknown program '{program_id}'.")
    raw_program = load_yaml_mapping(program_dir / "program.yaml")
    return (
        _parse_program(raw_program, program_dir / "program.yaml"),
        tuple(load_current_workstreams(program_id, programs_root=programs_root)),
    )


def _augment_m365_workstream_profiles(
    program_id: str,
    *,
    workstreams: tuple[Workstream, ...],
    programs_root: Path,
) -> tuple[Workstream, ...]:
    return _augment_m365_workstream_profiles_impl(
        program_id,
        workstreams=workstreams,
        programs_root=programs_root,
    )


def _load_ado_items_via_uil(
    program: Program,
    workstreams: tuple[Workstream, ...],
    as_of: datetime,
    *,
    since: datetime,
    programs_root: Path,
    binding: Any | None = None,
    integration_error_sink: list[IntegrationError] | None = None,
    gather_v2_enabled: bool | None = None,
    force_discovery: bool = False,
    accept_shrinkage: bool = False,
    discovery_result_sink: list[Any] | None = None,
    channel_outcome_sink: list[Any] | None = None,
) -> tuple[tuple[WorkItem, ...], tuple[WorkItem, ...], int]:
    if binding is None:
        from src.commands.channel_wiring import resolve_channel_binding

        binding = resolve_channel_binding(program, workstreams, "ado", programs_root=programs_root)
    if binding is None:
        return (), (), 0
    use_gather_v2 = _gather_v2_enabled() if gather_v2_enabled is None else gather_v2_enabled
    if use_gather_v2:
        from src.commands.gather_pipeline import run_channel
        run_channel_fn = run_channel
    else:
        run_channel_fn = _run_channel
    return _load_ado_items_via_uil_impl(
        program,
        as_of,
        since=since,
        programs_root=programs_root,
        binding=binding,
        integration_error_sink=integration_error_sink,
        env_flag_fn=_uil_discovery_flag_fn(force_discovery=force_discovery, accept_shrinkage=accept_shrinkage),
        run_channel_fn=run_channel_fn,
        discovery_result_sink=discovery_result_sink,
        channel_outcome_sink=channel_outcome_sink,
    )


def _load_kusto_signals_via_uil(
    program: Program,
    workstreams: tuple[Workstream, ...],
    as_of: datetime,
    *,
    programs_root: Path,
    binding: Any | None = None,
    integration_error_sink: list[IntegrationError] | None = None,
    kusto_query_executor: KustoQueryExecutor | None = None,
    include_unvalidated: bool = False,
    query_state_sink: dict[str, dict[str, Any]] | None = None,
    previous_query_states: dict[str, dict[str, Any]] | None = None,
    gather_v2_enabled: bool | None = None,
    force_discovery: bool = False,
    accept_shrinkage: bool = False,
) -> tuple[tuple[Signal, ...], int]:
    from src.commands.channel_wiring import resolve_channel_binding

    if binding is None:
        binding = resolve_channel_binding(program, workstreams, "kusto", programs_root=programs_root)
    if binding is None:
        return (), 0
    use_gather_v2 = _gather_v2_enabled() if gather_v2_enabled is None else gather_v2_enabled
    if use_gather_v2:
        from src.commands.gather_pipeline import run_channel_with_extraction
        run_channel_with_extraction_fn: Callable[..., tuple[Any | None, Any | None, Any | None]] = run_channel_with_extraction
    else:
        run_channel_with_extraction_fn = _run_channel_with_extraction
    return _load_kusto_signals_via_uil_impl(
        program,
        as_of,
        programs_root=programs_root,
        binding=binding,
        integration_error_sink=integration_error_sink,
        kusto_query_executor=kusto_query_executor,
        include_unvalidated=include_unvalidated,
        query_state_sink=query_state_sink,
        previous_query_states=previous_query_states,
        env_flag_fn=_uil_discovery_flag_fn(force_discovery=force_discovery, accept_shrinkage=accept_shrinkage),
        run_channel_with_extraction_fn=run_channel_with_extraction_fn,
        record_kusto_query_state_fn=_record_kusto_query_state,
    )


def _load_teams_signals_via_uil(
    program: Program,
    workstreams: tuple[Workstream, ...],
    as_of: datetime,
    *,
    programs_root: Path,
    binding: Any | None = None,
    integration_error_sink: list[IntegrationError] | None = None,
    gather_v2_enabled: bool | None = None,
    force_discovery: bool = False,
    accept_shrinkage: bool = False,
) -> tuple[tuple[Signal, ...], int]:
    from src.commands.channel_wiring import resolve_channel_binding

    if binding is None:
        binding = resolve_channel_binding(program, workstreams, "teams", programs_root=programs_root)
    if binding is None:
        return (), 0
    use_gather_v2 = _gather_v2_enabled() if gather_v2_enabled is None else gather_v2_enabled
    if use_gather_v2:
        from src.commands.gather_pipeline import run_channel_with_extraction
        run_channel_with_extraction_fn: Callable[..., tuple[Any | None, Any | None, Any | None]] = run_channel_with_extraction
    else:
        run_channel_with_extraction_fn = _run_channel_with_extraction
    return _load_signal_channel_via_uil_impl(
        program,
        as_of,
        programs_root=programs_root,
        binding=binding,
        integration_error_sink=integration_error_sink,
        env_flag_fn=_uil_discovery_flag_fn(force_discovery=force_discovery, accept_shrinkage=accept_shrinkage),
        run_channel_with_extraction_fn=run_channel_with_extraction_fn,
    )


def _load_icm_signals_via_uil(
    program: Program,
    workstreams: tuple[Workstream, ...],
    as_of: datetime,
    *,
    programs_root: Path,
    binding: Any | None = None,
    integration_error_sink: list[IntegrationError] | None = None,
    gather_v2_enabled: bool | None = None,
    force_discovery: bool = False,
    accept_shrinkage: bool = False,
) -> tuple[tuple[Signal, ...], int]:
    from src.commands.channel_wiring import resolve_channel_binding

    if binding is None:
        binding = resolve_channel_binding(program, workstreams, "icm", programs_root=programs_root)
    if binding is None:
        return (), 0
    use_gather_v2 = _gather_v2_enabled() if gather_v2_enabled is None else gather_v2_enabled
    if use_gather_v2:
        from src.commands.gather_pipeline import run_channel_with_extraction
        run_channel_with_extraction_fn: Callable[..., tuple[Any | None, Any | None, Any | None]] = run_channel_with_extraction
    else:
        run_channel_with_extraction_fn = _run_channel_with_extraction
    return _load_signal_channel_via_uil_impl(
        program,
        as_of,
        programs_root=programs_root,
        binding=binding,
        integration_error_sink=integration_error_sink,
        env_flag_fn=_uil_discovery_flag_fn(force_discovery=force_discovery, accept_shrinkage=accept_shrinkage),
        run_channel_with_extraction_fn=run_channel_with_extraction_fn,
    )


def _run_channel(
    binding: Any,
    store: ChannelRegistryStore,
    *,
    program_id: str,
    since: datetime,
    verified_at: datetime,
    run_ctx: RunContext,
    integration_error_sink: list[IntegrationError] | None = None,
    discovery_result_sink: list[Any] | None = None,
    channel_outcome_sink: list[Any] | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[Any | None, Any | None]:
    return _run_channel_impl(
        binding,
        store,
        program_id=program_id,
        since=since,
        verified_at=verified_at,
        run_ctx=run_ctx,
        integration_error_sink=integration_error_sink,
        discovery_result_sink=discovery_result_sink,
        channel_outcome_sink=channel_outcome_sink,
        programs_root=programs_root,
    )


def _run_channel_with_extraction(
    binding: Any,
    store: ChannelRegistryStore,
    *,
    program_id: str,
    since: datetime,
    verified_at: datetime,
    run_ctx: RunContext,
    integration_error_sink: list[IntegrationError] | None = None, programs_root: Path = PROGRAMS_ROOT,
) -> tuple[Any | None, Any | None, Any | None]:
    return _run_channel_with_extraction_impl(
        binding,
        store,
        program_id=program_id,
        since=since,
        verified_at=verified_at,
        run_ctx=run_ctx,
        integration_error_sink=integration_error_sink,
    )


def _gather_v2_enabled() -> bool:
    return _env_flag("VERTEX_GATHER_V2")


def _env_flag(name: str) -> bool:
    return os.getenv(name, "0").strip().lower() in {"1", "true", "yes", "on"}


def _uil_discovery_flag_fn(*, force_discovery: bool, accept_shrinkage: bool) -> Callable[[str], bool]:
    """Sec 4.4: OR the first-class ``--force-discovery``/``--accept-shrinkage``
    CLI flags with the pre-existing ``VERTEX_UIL_*`` environment-variable
    compatibility path -- "Environment flags remain compatibility paths, not
    the documented operator interface."""

    def _fn(name: str) -> bool:
        if name == "VERTEX_UIL_FORCE_DISCOVERY":
            return force_discovery or _env_flag(name)
        if name == "VERTEX_UIL_ACCEPT_SHRINKAGE":
            return accept_shrinkage or _env_flag(name)
        return _env_flag(name)

    return _fn


def _resolve_uil_channel_binding_for_gather(
    program: Program,
    workstreams: tuple[Workstream, ...],
    channel: str,
    *,
    programs_root: Path,
) -> Any | None:
    return _resolve_uil_channel_binding_for_gather_impl(
        program,
        workstreams,
        channel,
        programs_root=programs_root,
        enabled_funcs=_UIL_CHANNEL_ENABLED_FUNCS,
        uil_channel_enabled_fn=_uil_channel_enabled,
    )

def _load_program_slice_contracts(program_id: str, programs_root: Path) -> tuple[SliceContract, ...] | None:
    path = programs_root / program_id / "slice_contracts.yaml"
    if not path.exists():
        return None
    return load_slice_contract(path)


def _slice_contract_saved_query_clauses(slice_contracts: tuple[SliceContract, ...] | None) -> dict[str, str]:
    return _slice_contract_saved_query_clauses_impl(slice_contracts)


def _load_dependency_program_items(
    program: Program,
    workstreams: tuple[Workstream, ...],
    as_of: datetime,
) -> tuple[tuple[_DependencyQueryItems, ...], int]:
    return _load_dependency_program_items_impl(
        program,
        workstreams,
        as_of,
        ado_client_factory=ADOClient,
        batch_fields=_BATCH_FIELDS,
        work_item_from_sources_fn=_work_item_from_sources,
    )


def _build_dependency_signals(
    dependency_items: tuple[_DependencyQueryItems, ...],
    *,
    program_id: str,
    workstreams: tuple[Workstream, ...],
    as_of: datetime,
    stale_warn_days: int,
    stale_block_days: int,
    query_state_sink: dict[str, dict[str, Any]] | None = None,
    previous_query_states: dict[str, dict[str, Any]] | None = None,
) -> tuple[Signal, ...]:
    return _build_dependency_signals_impl(
        dependency_items,
        program_id=program_id,
        workstreams=workstreams,
        as_of=as_of,
        stale_warn_days=stale_warn_days,
        stale_block_days=stale_block_days,
        freshness_signal_rule_ids=_FRESHNESS_SIGNAL_RULE_IDS,
        query_state_sink=query_state_sink,
        previous_query_states=previous_query_states,
    )


def _load_analytics_signals(
    program: Program,
    workstreams: tuple[Workstream, ...],
    as_of: datetime,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    query_state_sink: dict[str, dict[str, Any]] | None = None,
    previous_query_states: dict[str, dict[str, Any]] | None = None,
) -> tuple[tuple[Signal, ...], int]:
    return _load_analytics_signals_impl(
        program,
        workstreams,
        as_of,
        programs_root=programs_root,
        query_state_sink=query_state_sink,
        previous_query_states=previous_query_states,
        ado_client_factory=ADOClient,
        date_to_sk_fn=_date_to_sk,
        analytics_snapshot_fields=_ANALYTICS_SNAPSHOT_FIELDS,
        build_analytics_signals_fn=_build_analytics_signals,
        load_wiql_golden_query_signals_fn=_load_wiql_golden_query_signals,
        expected_max_age_hours=_DEFAULT_ADO_ANALYTICS_EXPECTED_MAX_AGE_HOURS,
    )


def _load_wiql_golden_query_signals(
    program: Program,
    workstreams: tuple[Workstream, ...],
    as_of: datetime,
    *,
    programs_root: Path,
    query_state_sink: dict[str, dict[str, Any]] | None = None,
    previous_query_states: dict[str, dict[str, Any]] | None = None,
) -> tuple[tuple[Signal, ...], int]:
    queries = _load_ado_wiql_queries(
        program.id,
        program=program,
        programs_root=programs_root,
        filter_for_gather=True,
    )
    return _load_wiql_golden_query_signals_impl(
        program,
        workstreams,
        as_of,
        queries=queries,
        query_state_sink=query_state_sink,
        previous_query_states=previous_query_states,
        ado_client_factory=ADOClient,
        normalize_ado_team_name_fn=_normalize_ado_team_name,
        expand_with_linked_items_fn=lambda client, seed_ids: expand_with_linked_items(client, seed_ids, max_depth=1),
    )


def _resolve_wiql_query_text(
    query: KustoQuery,
    *,
    program: Program,
    workstreams: tuple[Workstream, ...],
    client: ADOClient,
    current_iteration_path_by_team: dict[str | None, str | None],
) -> tuple[str, int]:
    return _resolve_wiql_query_text_impl(
        query,
        program=program,
        workstreams=workstreams,
        client=client,
        current_iteration_path_by_team=current_iteration_path_by_team,
        normalize_ado_team_name_fn=_normalize_ado_team_name,
    )


def _load_sprint_signals(
    program: Program,
    workstreams: tuple[Workstream, ...],
    items: tuple[WorkItem, ...],
    as_of: datetime,
    query_state_sink: dict[str, dict[str, Any]] | None = None,
    previous_query_states: dict[str, dict[str, Any]] | None = None,
) -> tuple[tuple[Signal, ...], int]:
    return _load_sprint_signals_impl(
        program,
        workstreams,
        items,
        as_of,
        query_state_sink=query_state_sink,
        previous_query_states=previous_query_states,
        ado_client_factory=ADOClient,
        normalize_ado_team_name_fn=_normalize_ado_team_name,
        date_to_sk_fn=_date_to_sk,
        sprint_snapshot_fields=_SPRINT_SNAPSHOT_FIELDS,
        build_sprint_signals_fn=_build_sprint_signals,
        snapshot_item_filter_batch_size=_SNAPSHOT_ITEM_FILTER_BATCH_SIZE,
        expected_max_age_hours=_DEFAULT_ADO_ANALYTICS_EXPECTED_MAX_AGE_HOURS,
    )


def _build_sprint_signals(
    *,
    iterations_by_team: dict[str | None, tuple[dict[str, Any], ...]],
    capacities_by_team_iteration: dict[tuple[str | None, str | None], tuple[dict[str, Any], ...]],
    sprint_snapshot_rows: list[dict[str, Any]],
    items: tuple[WorkItem, ...],
    program_id: str,
    workstreams: tuple[Workstream, ...],
    as_of: datetime,
) -> tuple[Signal, ...]:
    return _build_sprint_signals_impl(
        iterations_by_team=iterations_by_team,
        capacities_by_team_iteration=capacities_by_team_iteration,
        sprint_snapshot_rows=sprint_snapshot_rows,
        items=items,
        program_id=program_id,
        workstreams=workstreams,
        as_of=as_of,
        normalize_ado_team_name_fn=_normalize_ado_team_name,
    )


def _normalize_ado_team_name(raw_value: str | None) -> str | None:
    if raw_value is None:
        return None
    normalized = raw_value.strip()
    return normalized or None


def _build_analytics_signals(
    *,
    rows: list[dict[str, Any]],
    program_id: str,
    workstreams: tuple[Workstream, ...],
    start_date_sk: int,
    end_date_sk: int,
    as_of: datetime,
) -> tuple[Signal, ...]:
    return _build_analytics_signals_impl(
        rows=rows,
        program_id=program_id,
        workstreams=workstreams,
        start_date_sk=start_date_sk,
        end_date_sk=end_date_sk,
        as_of=as_of,
    )


def _work_item_from_sources(
    *,
    raw: dict[str, Any],
    batch_row: dict[str, Any],
    revision_rows: list[dict[str, Any]],
    comment_rows: list[dict[str, Any]],
    fetched_at: datetime,
) -> WorkItem:
    fields = batch_row.get("fields", {}) if isinstance(batch_row, dict) else {}
    work_item_id = int(raw.get("WorkItemId") or raw.get("id") or batch_row.get("id") or fields.get("System.Id") or 0)
    assigned_to, assigned_to_email = _parse_identity(fields.get("System.AssignedTo"))
    tags = _parse_tags(fields.get("System.Tags") or raw.get("Tags"))
    state = str(fields.get("System.State") or raw.get("State") or "Active")
    risk_assessment = normalize_risk_assessment(fields.get(ADO_RISK_ASSESSMENT_FIELD))
    return WorkItem(
        id=work_item_id,
        type=str(fields.get("System.WorkItemType") or raw.get("WorkItemType") or "WorkItem"),
        title=str(fields.get("System.Title") or raw.get("Title") or f"Work Item {work_item_id}"),
        state=state,
        assigned_to=assigned_to,
        assigned_to_email=assigned_to_email,
        area_path=str(fields.get("System.AreaPath") or raw.get("AreaPath") or raw.get("Area", {}).get("AreaPath") or ""),
        iteration_path=str(fields.get("System.IterationPath") or raw.get("IterationPath") or ""),
        target_date=_parse_date(fields.get("Microsoft.VSTS.Scheduling.TargetDate") or raw.get("TargetDate")),
        risk_level=_infer_risk_level(state, tags, risk_assessment),
        tags=tags,
        custom_fields={},
        revisions=_parse_revisions(work_item_id, revision_rows),
        comments=_parse_comments(work_item_id, comment_rows),
        fetched_at=fetched_at,
        risk_assessment=risk_assessment,
        risk_assessment_comment=_optional_string(fields.get(ADO_RISK_ASSESSMENT_COMMENT_FIELD)),
    )


def _parse_comments(work_item_id: int, rows: list[dict[str, Any]]) -> list[Comment]:
    comments: list[Comment] = []
    for row in rows:
        created_by, created_by_email = _parse_identity(row.get("createdBy"))
        created_date = _parse_datetime(row.get("publishedDate") or row.get("createdDate"))
        if created_date is None:
            continue
        comments.append(
            Comment(
                work_item_id=work_item_id,
                comment_id=int(row.get("id") or row.get("commentId") or 0),
                created_by=created_by or "Unknown",
                created_by_email=created_by_email or "",
                created_date=created_date,
                text=str(row.get("text") or row.get("renderedText") or ""),
            )
        )
    return comments


def _parse_revisions(work_item_id: int, rows: list[dict[str, Any]]) -> list[Revision]:
    revisions: list[Revision] = []
    previous_fields: dict[str, Any] | None = None
    for row in sorted(rows, key=lambda entry: int(entry.get("rev") or 0)):
        fields = row.get("fields", {}) if isinstance(row.get("fields"), dict) else {}
        changed_date = _parse_datetime(fields.get("System.ChangedDate"))
        if changed_date is None:
            continue
        changed_by, changed_by_email = _parse_identity(fields.get("System.ChangedBy"))
        field_changes: dict[str, tuple[str | None, str | None]] = {}
        if previous_fields is not None:
            for key in set(previous_fields) | set(fields):
                old_value = _field_value(previous_fields.get(key))
                new_value = _field_value(fields.get(key))
                if old_value != new_value:
                    field_changes[key] = (old_value, new_value)
        revisions.append(
            Revision(
                work_item_id=work_item_id,
                rev_number=int(row.get("rev") or 0),
                changed_by=changed_by or "Unknown",
                changed_by_email=changed_by_email or "",
                changed_date=changed_date,
                fields_changed=field_changes,
            )
        )
        previous_fields = fields
    return revisions


def _build_ado_revision_signals(
    items: tuple[WorkItem, ...],
    *,
    program_id: str,
    workstreams: tuple[Workstream, ...],
    since: datetime,
) -> tuple[Signal, ...]:
    vertex_identities = _vertex_service_identities()
    signals: list[Signal] = []
    for item in items:
        workstream_id = _resolve_workstream_id(item.area_path, workstreams)
        for revision in sorted(item.revisions, key=lambda entry: entry.changed_date):
            if revision.changed_date < since:
                continue
            if _is_echo_chamber_revision(revision, vertex_identities):
                continue
            for field_name, values in revision.fields_changed.items():
                canonical_field = _tracked_field_name(field_name)
                if canonical_field is None:
                    continue
                prior, current = values
                raw_ref = f"wi:{item.id}:rev:{revision.rev_number}:{canonical_field.lower()}"
                signals.append(
                    Signal(
                        id=str(uuid5(NAMESPACE_URL, f"{program_id}|{raw_ref}|{revision.changed_date.isoformat()}")),
                        timestamp=revision.changed_date,
                        source="ado/revision",
                        program_id=program_id,
                        workstream_id=workstream_id,
                        entity_refs=merge_entity_refs(
                            provider_refs=(f"WI:{item.id}",),
                            workstream_id=workstream_id,
                        ),
                        text=_build_revision_signal_text(item.id, canonical_field, prior, current),
                        raw_ref=raw_ref,
                        confidence=Confidence.HIGH,
                        metadata={
                            "work_item_id": item.id,
                            "revision_number": revision.rev_number,
                            "field": canonical_field,
                            "prior": prior,
                            "current": current,
                        },
                    )
                )
    return tuple(signals)


def _build_ado_comment_signals(
    items: tuple[WorkItem, ...],
    *,
    program_id: str,
    workstreams: tuple[Workstream, ...],
    since: datetime,
) -> tuple[Signal, ...]:
    vertex_identities = _vertex_service_identities()
    signals: list[Signal] = []
    for item in items:
        workstream_id = _resolve_workstream_id(item.area_path, workstreams)
        for comment in sorted(item.comments, key=lambda entry: entry.created_date):
            if comment.created_date < since:
                continue
            if _is_echo_chamber_comment(comment, vertex_identities):
                continue
            if not comment.text.strip():
                continue
            raw_ref = f"wi:{item.id}:comment:{comment.comment_id}"
            signals.append(
                Signal(
                    id=str(uuid5(NAMESPACE_URL, f"{program_id}|{raw_ref}|{comment.created_date.isoformat()}")),
                    timestamp=comment.created_date,
                    source="ado/comment",
                    program_id=program_id,
                    workstream_id=workstream_id,
                    entity_refs=merge_entity_refs(
                        provider_refs=(f"WI:{item.id}",),
                        workstream_id=workstream_id,
                    ),
                    text=_truncate_signal_text(f"Comment on WI {item.id}: {comment.text.strip()}"),
                    raw_ref=raw_ref,
                    confidence=Confidence.HIGH,
                    metadata={
                        "work_item_id": item.id,
                        "comment_id": comment.comment_id,
                        "author": comment.created_by,
                    },
                )
            )
    return tuple(signals)


def _build_freshness_signals(
    items: tuple[WorkItem, ...],
    *,
    program_id: str,
    workstreams: tuple[Workstream, ...],
    as_of: datetime,
    stale_warn_days: int,
    stale_block_days: int,
) -> tuple[Signal, ...]:
    report = build_freshness_report(
        current_items=items,
        issue_number=0,
        as_of=as_of,
        stale_warn_days=stale_warn_days,
        stale_block_days=stale_block_days,
    )
    item_lookup = {item.id: item for item in items}
    signals: list[Signal] = []
    for finding in report.items:
        if finding.rule_id not in _FRESHNESS_SIGNAL_RULE_IDS:
            continue
        item = item_lookup.get(finding.work_item_id)
        if item is None:
            continue
        workstream_id = _resolve_workstream_id(item.area_path, workstreams)
        capture_date = as_of.date().isoformat()
        raw_ref = f"wi:{item.id}:freshness:{finding.rule_id}:{capture_date}"
        label = finding.action_label or finding.rule_id
        signals.append(
            Signal(
                id=str(uuid5(NAMESPACE_URL, f"{program_id}|{raw_ref}|{capture_date}")),
                timestamp=as_of,
                source="vertex/freshness",
                program_id=program_id,
                workstream_id=workstream_id,
                entity_refs=merge_entity_refs(
                    provider_refs=(f"WI:{item.id}",),
                    workstream_id=workstream_id,
                ),
                text=f"{label}: {finding.message}",
                raw_ref=raw_ref,
                confidence=Confidence.HIGH,
                metadata={
                    "work_item_id": item.id,
                    "finding_type": finding.rule_id,
                    "severity": finding.severity,
                    "date": capture_date,
                },
            )
        )
    return tuple(signals)


def _build_kusto_signals(
    *,
    program: Program,
    program_id: str,
    programs_root: Path,
    as_of: datetime,
    workstreams: tuple[Workstream, ...],
    executor: KustoQueryExecutor,
    filter_for_gather: bool = False,
    include_unvalidated: bool = False,
    query_state_sink: dict[str, dict[str, Any]] | None = None,
    previous_query_states: dict[str, dict[str, Any]] | None = None,
) -> tuple[Signal, ...]:
    if program.kusto is None or not program.kusto.enabled:
        raise typer.BadParameter(f"Program '{program_id}' does not have Kusto enabled.")

    return _build_kusto_signals_impl(
        queries=_dedupe_queries_by_id(
            tuple(
                query
                for query in _load_kusto_queries(
                    program_id,
                    program=program,
                    programs_root=programs_root,
                    workstreams=workstreams,
                    apply_signal_source_scope=True,
                    filter_for_gather=filter_for_gather,
                    include_unvalidated=include_unvalidated,
                )
                if not _is_icm_query(query)
            )
        ),
        program_id=program_id,
        as_of=as_of,
        executor=executor,
        query_state_sink=query_state_sink,
        previous_query_states=previous_query_states,
    )


def _load_refresh_kpi_queries(
    program_id: str,
    *,
    programs_root: Path,
    include_unvalidated: bool = False,
    engine: str | None = None,
) -> tuple[KustoQuery, ...]:
    all_queries = [
        query
        for query in load_kpi_queries(program_id, programs_root=programs_root)
        if query.refresh_on_gather
        and not _is_icm_query(query)
        and (engine is None or query.engine == engine)
    ]
    included: list[KustoQuery] = []
    for query in all_queries:
        if include_unvalidated or query.validated:
            included.append(query)
        else:
            # §21: emit context gap for each skipped unvalidated KPI query
            try:
                append_context_gap(
                    feature="gather",
                    program=program_id,
                    lane=None,
                    field="kpis.validated",
                    severity="quality_degraded",
                    message=f"KPI query '{query.id}' skipped on gather (validated=false); run with --probe to include",
                    impact_estimate="medium",
                    programs_root=programs_root,
                )
            except Exception:
                pass
    return tuple(included)


def _build_kusto_kpi_signals(
    *,
    program: Program,
    program_id: str,
    programs_root: Path,
    as_of: datetime,
    workstreams: tuple[Workstream, ...],
    executor: KustoQueryExecutor,
    include_unvalidated: bool = False,
    query_state_sink: dict[str, dict[str, Any]] | None = None,
    previous_query_states: dict[str, dict[str, Any]] | None = None,
) -> tuple[Signal, ...]:
    return _build_kusto_kpi_signals_impl(
        queries=_dedupe_queries_by_id(
            _load_refresh_kpi_queries(
                program_id,
                programs_root=programs_root,
                include_unvalidated=include_unvalidated,
            )
        ),
        program=program,
        program_id=program_id,
        as_of=as_of,
        workstreams=workstreams,
        executor=executor,
        ado_client_factory=ADOClient,
        normalize_ado_team_name_fn=_normalize_ado_team_name,
        record_kusto_query_state_fn=_record_kusto_query_state,
        build_kusto_kpi_signal_fn=_build_kusto_kpi_signal,
        summarize_pull_requests_fn=_summarize_pull_requests,
        pull_request_provider_ref_fn=_pull_request_provider_ref,
        pull_request_entity_refs_fn=_pull_request_entity_refs,
        batch_fields=_BATCH_FIELDS,
        query_state_sink=query_state_sink,
        previous_query_states=previous_query_states,
    )


def _execute_wiql_kpi_query(
    query: KustoQuery,
    *,
    program: Program,
    workstreams: tuple[Workstream, ...],
    client: ADOClient,
    as_of: datetime,
    current_iteration_path_by_team: dict[str | None, str | None],
) -> list[dict[str, Any]]:
    return _execute_wiql_kpi_query_impl(
        query,
        program=program,
        workstreams=workstreams,
        client=client,
        as_of=as_of,
        current_iteration_path_by_team=current_iteration_path_by_team,
        normalize_ado_team_name_fn=_normalize_ado_team_name,
        batch_fields=_BATCH_FIELDS,
    )


def _execute_ado_pr_kpi_query(
    query: KustoQuery,
    *,
    workstreams: tuple[Workstream, ...],
    client: ADOClient,
    as_of: datetime,
) -> list[dict[str, Any]]:
    return _execute_ado_pr_kpi_query_impl(
        query,
        workstreams=workstreams,
        client=client,
        as_of=as_of,
        summarize_pull_requests_fn=_summarize_pull_requests,
        pull_request_provider_ref_fn=_pull_request_provider_ref,
        pull_request_entity_refs_fn=_pull_request_entity_refs,
    )


def _make_cluster_filtered_executor(
    executor: KustoQueryExecutor,
    failed_targets: frozenset[tuple[str, str]],
) -> KustoQueryExecutor:
    """Wrap an executor to return empty rows for queries on pre-flight-failed clusters."""
    def filtered(query: KustoQuery) -> list[dict[str, Any]]:
        if (query.cluster.strip(), query.database.strip()) in failed_targets:
            return []
        return executor(query)
    return filtered


def _build_icm_signals(
    *,
    program: Program,
    program_id: str,
    programs_root: Path,
    as_of: datetime,
    workstreams: tuple[Workstream, ...],
    executor: KustoQueryExecutor,
    bridge: type[AgencyBridge] | AgencyBridge | Callable[[], AgencyBridge],
    icm_client_factory: IcmClientFactory | None = None,
) -> tuple[Signal, ...]:
    knowledge = load_program_knowledge(program_id, programs_root=programs_root)
    signals = _build_icm_signals_impl(
        program=program,
        program_id=program_id,
        programs_root=programs_root,
        as_of=as_of,
        teams=knowledge.teams,
        workstreams=workstreams,
        executor=executor,
        bridge=bridge,
        resolve_icm_workstream_id_fn=_resolve_icm_workstream_id,
        load_kusto_queries_fn=_load_kusto_queries,
        icm_client_factory=icm_client_factory,
        warn_fn=log.warning,
    )
    if not signals and (program.kusto is None or not program.kusto.enabled):
        log.warning(
            "IcM gather unavailable for %s: Agency direct path is unavailable and no Kusto-backed IcM queries are enabled.",
            program_id,
        )
    return signals


def _load_kusto_queries(
    program_id: str,
    *,
    program: Program,
    programs_root: Path,
    workstreams: tuple[Workstream, ...] = (),
    apply_signal_source_scope: bool = False,
    filter_for_gather: bool = False,
    include_unvalidated: bool = False,
) -> tuple[KustoQuery, ...]:
    knowledge = load_program_knowledge(program_id, programs_root=programs_root)
    return _load_kusto_queries_impl(
        program_id,
        program=program,
        knowledge=knowledge,
        workstreams=workstreams,
        apply_signal_source_scope=apply_signal_source_scope,
        filter_for_gather=filter_for_gather,
        include_unvalidated=include_unvalidated,
        is_icm_query_fn=_is_icm_query,
    )


def _load_ado_wiql_queries(
    program_id: str,
    *,
    program: Program,
    programs_root: Path,
    filter_for_gather: bool = False,
    include_unvalidated: bool = False,
) -> tuple[KustoQuery, ...]:
    knowledge = load_program_knowledge(program_id, programs_root=programs_root)
    return _load_ado_wiql_queries_impl(
        program_id,
        program=program,
        knowledge=knowledge,
        filter_for_gather=filter_for_gather,
        include_unvalidated=include_unvalidated,
    )


def _dedupe_queries_by_id(queries: tuple[KustoQuery, ...]) -> tuple[KustoQuery, ...]:
    deduped: dict[str, KustoQuery] = {}
    for query in queries:
        deduped.setdefault(query.id, query)
    return tuple(deduped.values())


def _record_kusto_query_state(
    query_state_sink: dict[str, dict[str, Any]] | None,
    query: KustoQuery,
    *,
    rows: list[dict[str, Any]],
    as_of: datetime,
    duration_ms: int,
    error: str | None = None,
    previous_state: dict[str, Any] | None = None,
) -> None:
    _record_kusto_query_state_impl(
        query_state_sink,
        query,
        rows=rows,
        as_of=as_of,
        duration_ms=duration_ms,
        error=error,
        previous_state=previous_state,
    )


def _record_ado_wiql_query_state(
    query_state_sink: dict[str, dict[str, Any]] | None,
    query: KustoQuery,
    *,
    work_item_count: int,
    as_of: datetime,
    duration_ms: int,
    error: str | None = None,
    previous_state: dict[str, Any] | None = None,
) -> None:
    _record_ado_wiql_query_state_impl(
        query_state_sink,
        query,
        work_item_count=work_item_count,
        as_of=as_of,
        duration_ms=duration_ms,
        error=error,
        previous_state=previous_state,
    )




def _is_icm_query(query: KustoQuery) -> bool:
    return query.database.strip().lower() == "icmdatawarehouse" or query.cluster.strip().lower().startswith("https://icmcluster.")


def _prefer_agency_icm(program: Program) -> bool:
    return _prefer_agency_icm_impl(program)


def _build_kusto_signal(
    *,
    query: KustoQuery,
    rows: list[dict[str, Any]],
    program_id: str,
    as_of: datetime,
) -> Signal | None:
    return _build_kusto_signal_impl(query=query, rows=rows, program_id=program_id, as_of=as_of)


def _build_kusto_kpi_signal(
    *,
    query: KustoQuery,
    rows: list[dict[str, Any]],
    program_id: str,
    as_of: datetime,
    entity_refs: tuple[str, ...] | None = None,
) -> Signal | None:
    if not rows:
        return None

    timestamp = _kusto_event_timestamp(rows, as_of=as_of)
    event_timestamp = timestamp.isoformat()
    raw_ref = f"kusto_kpi:{query.id}:{event_timestamp}"
    first_row = rows[0]
    result_value = _kusto_kpi_value(query, first_row)
    label = query.label or query.section or query.id
    value_label = result_value if result_value is not None else "rows available"
    # BL-F2 (D-19): see kusto_query_helpers.build_kusto_signal for this fix's rationale.
    workstream_id = query.workstream_ids[0] if query.workstream_ids else None
    signal_entity_refs = merge_entity_refs(
        provider_refs=entity_refs if entity_refs is not None else _extract_kusto_entity_refs(rows),
        workstream_id=workstream_id,
    )
    return Signal(
        id=str(uuid5(NAMESPACE_URL, f"{program_id}|{raw_ref}|{query.id}")),
        timestamp=timestamp,
        source="kusto_kpi",
        program_id=program_id,
        workstream_id=workstream_id,
        workstream_ids=query.workstream_ids,
        entity_refs=signal_entity_refs,
        text=_truncate_signal_text(f"KPI {label}: {value_label}"),
        raw_ref=raw_ref,
        confidence=_confidence_from_string(query.confidence),
        metadata={
            "query_id": query.id,
            "cluster": query.cluster,
            "database": query.database,
            "engine": query.engine,
            "validated": query.validated,
            "event_timestamp": event_timestamp,
            "row_count": len(rows),
            "label": query.label,
            "result_column": query.result_column,
            "result_value": result_value,
            "result_json": json.dumps(first_row, default=str, sort_keys=True),
        },
    )


def _project_kpi_signals_to_observations(
    program_id: str,
    *,
    programs_root: Path,
    as_of: datetime,
    kpi_signals: tuple[Signal, ...],
    query_states: dict[str, dict[str, Any]],
    include_unvalidated: bool = False,
    reality_store: RealityStore | None = None,
) -> tuple[MetricObservation, ...]:
    store = reality_store or _build_reality_store(program_id, programs_root=programs_root)
    return _project_refresh_kpi_signals_to_observations_impl(
        program_id,
        programs_root=programs_root,
        as_of=as_of,
        kpi_signals=kpi_signals,
        query_states=query_states,
        store=store,
        include_unvalidated=include_unvalidated,
        load_refresh_kpi_queries_fn=_load_refresh_kpi_queries,
        dedupe_queries_fn=_dedupe_queries_by_id,
    )


def _build_signal_source_ingestion_run_id(program_id: str, source_ref: str, as_of: datetime) -> str:
    return _build_signal_source_ingestion_run_id_impl(program_id, source_ref, as_of)


def _build_signal_ingestion_captured_window(signals: tuple[Signal, ...], source_names: tuple[str, ...]) -> str | None:
    return _build_signal_ingestion_captured_window_impl(signals, source_names)


def _record_optional_source_ingestion_runs(
    program_id: str,
    *,
    as_of: datetime,
    programs_root: Path,
    include_workiq: bool,
    include_analytics: bool,
    include_sprints: bool,
    include_pipelines: bool,
    include_icm: bool,
    signals: tuple[Signal, ...],
    integration_error_details: tuple[IntegrationError, ...],
    reality_store: RealityStore | None = None,
) -> None:
    store = reality_store or _build_reality_store(program_id, programs_root=programs_root)
    _record_optional_source_ingestion_runs_impl(
        program_id,
        as_of=as_of,
        include_workiq=include_workiq,
        include_analytics=include_analytics,
        include_sprints=include_sprints,
        include_pipelines=include_pipelines,
        include_icm=include_icm,
        signals=signals,
        integration_error_details=integration_error_details,
        store=store,
    )


def _build_reality_store(program_id: str, *, programs_root: Path | None = None) -> RealityStore:
    if os.environ.get("VERTEX_DB_PATH"):
        return RealityStore(program_id)
    if programs_root is not None:
        return RealityStore(program_id, db_root=programs_root.parent / "vertex-db")
    return RealityStore(program_id)


def _build_gather_channel_states(
    *,
    program_id: str,
    programs_root: Path,
    workstreams: tuple[Workstream, ...],
    ado_signals: tuple[Signal, ...],
    kusto_signals: tuple[Signal, ...],
    workiq_signals: tuple[Signal, ...],
    icm_signals: tuple[Signal, ...],
    gather_flags: dict[str, bool],
    previous_channels: dict[str, dict[str, Any]] | None = None,
    integration_error_details: tuple[IntegrationError, ...] = (),
) -> dict[str, dict[str, Any]]:
    return _stage_build_gather_channel_states(
        program_id=program_id,
        programs_root=programs_root,
        workstreams=workstreams,
        ado_signals=ado_signals,
        kusto_signals=kusto_signals,
        workiq_signals=workiq_signals,
        icm_signals=icm_signals,
        gather_flags=gather_flags,
        previous_channels=previous_channels,
        integration_error_details=integration_error_details,
        format_optional_datetime=_format_optional_datetime,
    )


def _build_uil_ado_channel_state(program_id: str, *, programs_root: Path) -> dict[str, Any]:
    return _stage_build_uil_ado_channel_state(
        program_id,
        programs_root=programs_root,
        format_optional_datetime=_format_optional_datetime,
    )


def _build_uil_channel_state(
    program_id: str,
    channel: str,
    *,
    enabled: bool,
    programs_root: Path,
) -> dict[str, Any]:
    return _stage_build_uil_channel_state(
        program_id,
        channel,
        enabled=enabled,
        programs_root=programs_root,
        format_optional_datetime=_format_optional_datetime,
    )


def _format_optional_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _build_m365_discovery_state(
    *,
    program_id: str,
    programs_root: Path,
    program: Program,
    workstreams: tuple[Workstream, ...],
    workiq_signals: tuple[Signal, ...],
    gather_flags: dict[str, bool],
    items: tuple[WorkItem, ...] = (),
    integration_error_details: tuple[IntegrationError, ...] = (),
    registry_artifacts: tuple[M365RegistryArtifact, ...] | None = None,
    feedback_events: tuple[Any, ...] = (),
    as_of: datetime | None = None,
    previous_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _stage_build_m365_discovery_state(
        program_id=program_id,
        programs_root=programs_root,
        program=program,
        workstreams=workstreams,
        workiq_signals=workiq_signals,
        gather_flags=gather_flags,
        items=items,
        integration_error_details=integration_error_details,
        registry_artifacts=registry_artifacts,
        feedback_events=feedback_events,
        as_of=as_of,
        previous_entry=previous_entry,
        count_transcript_series_state=_count_transcript_series_state,
        count_chat_thread_state=_count_chat_thread_state,
        tracked_m365_artifact_ids=_tracked_m365_artifact_ids,
        observed_m365_thread_ids=_observed_m365_thread_ids,
        load_discovery_milestones=lambda stage_program_id, stage_programs_root: _load_discovery_milestones(
            stage_program_id,
            programs_root=stage_programs_root,
        ),
        build_workiq_query_plans=lambda **kwargs: tuple(_build_workiq_query_plans(**kwargs)),
        build_m365_discovery_queries=lambda **kwargs: tuple(_build_m365_discovery_queries(**kwargs)),
        build_seeded_source_discovery_state=lambda *, program_id, programs_root, as_of: _build_seeded_source_discovery_state(
            program_id=program_id,
            programs_root=programs_root,
            as_of=as_of,
        ),
        build_adaptive_workiq_state=lambda **kwargs: _build_adaptive_workiq_state(**kwargs),
    )


def _build_seeded_source_discovery_state(
    *,
    program_id: str,
    programs_root: Path,
    as_of: datetime | None,
) -> dict[str, Any]:
    del as_of
    db_path = resolve_channel_registry_path_for_read(program_id, programs_root=programs_root)
    if not db_path.exists():
        return {
            "intent_count": 0,
            "attempt_count": 0,
            "attempted_intent_count": 0,
            "candidate_count": 0,
            "pending_candidate_count": 0,
            "first_attempted_at": None,
            "latest_attempted_at": None,
            "outcome_counts": {},
            "latest_attempts": [],
        }

    store = SourceCandidateStore(db_path, program_id, ensure_schema=False)
    intents = store.list_intents()
    first_attempted_at: datetime | None = None
    latest_attempted_at: datetime | None = None
    attempt_count = 0
    attempted_intent_count = 0
    candidate_count = 0
    pending_candidate_count = 0
    outcome_counts: dict[str, int] = {}
    latest_attempts: list[dict[str, Any]] = []
    for intent in intents:
        candidates = store.list_candidates_for_intent(intent.intent_id)
        if candidates:
            candidate_count += len(candidates)
            pending_candidate_count += sum(1 for candidate in candidates if candidate.status == SourceCandidateStatus.PENDING)
        attempts = store.get_attempts(intent.intent_id, exclude_expired=False)
        if not attempts:
            continue
        attempted_intent_count += 1
        attempt_count += len(attempts)
        for attempt in attempts:
            outcome_key = attempt.outcome.value
            outcome_counts[outcome_key] = outcome_counts.get(outcome_key, 0) + 1
            if first_attempted_at is None or attempt.attempted_at < first_attempted_at:
                first_attempted_at = attempt.attempted_at
            if latest_attempted_at is None or attempt.attempted_at > latest_attempted_at:
                latest_attempted_at = attempt.attempted_at
        latest_attempt = attempts[0]
        latest_attempts.append(
            {
                "intent_id": intent.intent_id,
                "display_name": intent.display_name,
                "ref_kind": intent.ref_kind.value,
                "workstream_id": intent.workstream_id,
                "outcome": latest_attempt.outcome.value,
                "reason": latest_attempt.reason,
                "result_count": latest_attempt.result_count,
                "attempted_at": _format_optional_datetime(latest_attempt.attempted_at),
            }
        )
    return {
        "intent_count": len(intents),
        "attempt_count": attempt_count,
        "attempted_intent_count": attempted_intent_count,
        "candidate_count": candidate_count,
        "pending_candidate_count": pending_candidate_count,
        "first_attempted_at": _format_optional_datetime(first_attempted_at),
        "latest_attempted_at": _format_optional_datetime(latest_attempted_at),
        "outcome_counts": outcome_counts,
        "latest_attempts": sorted(
            latest_attempts,
            key=lambda item: (
                str(item.get("attempted_at") or ""),
                str(item.get("intent_id") or ""),
            ),
            reverse=True,
        )[:20],
    }


def _build_current_m365_promotion_candidates(
    *,
    registry_artifacts: tuple[M365RegistryArtifact, ...],
    gather_flags: dict[str, bool],
    feedback_events: tuple[Any, ...] = (),
    as_of: datetime | None = None,
) -> tuple[M365PromotionCandidate, ...]:
    return _stage_build_current_m365_promotion_candidates(
        registry_artifacts=registry_artifacts,
        gather_flags=gather_flags,
        feedback_events=feedback_events,
        as_of=as_of,
    )


def _build_current_m365_promotion_blocked_artifacts(
    *,
    registry_artifacts: tuple[M365RegistryArtifact, ...],
    gather_flags: dict[str, bool],
    feedback_events: tuple[Any, ...] = (),
    as_of: datetime | None = None,
) -> tuple[M365PromotionBlockedArtifact, ...]:
    return _stage_build_current_m365_promotion_blocked_artifacts(
        registry_artifacts=registry_artifacts,
        gather_flags=gather_flags,
        feedback_events=feedback_events,
        as_of=as_of,
    )


def _count_transcript_series_state(workstreams: tuple[Workstream, ...]) -> tuple[int, int]:
    total = 0
    missing = 0
    for workstream in workstreams:
        signal_sources = workstream.signal_sources
        if signal_sources is None:
            continue
        for series in signal_sources.teams_meeting_series:
            if not series.include_transcripts:
                continue
            total += 1
            if not series.series_id:
                missing += 1
    return total, missing


def _count_chat_thread_state(workstreams: tuple[Workstream, ...]) -> tuple[int, int]:
    total = 0
    missing = 0
    for workstream in workstreams:
        signal_sources = workstream.signal_sources
        if signal_sources is None:
            continue
        for chat in signal_sources.teams_chats:
            total += 1
            if not chat.thread_id:
                missing += 1
    return total, missing


def _configured_m365_artifact_ids(workstreams: tuple[Workstream, ...]) -> set[str]:
    configured_ids: set[str] = set()
    for workstream in workstreams:
        signal_sources = workstream.signal_sources
        if signal_sources is None:
            continue
        for series in signal_sources.teams_meeting_series:
            normalized_series_id = normalize_thread_id(series.series_id)
            if normalized_series_id:
                configured_ids.add(normalized_series_id)
        for chat in signal_sources.teams_chats:
            normalized_thread_id = normalize_thread_id(chat.thread_id)
            if normalized_thread_id:
                configured_ids.add(normalized_thread_id)
        for email_thread in signal_sources.email_threads:
            normalized_thread_id = normalize_thread_id(email_thread.thread_id)
            if normalized_thread_id:
                configured_ids.add(normalized_thread_id)
    return configured_ids


def _configured_work_item_entity_refs_by_m365_id(
    workstreams: tuple[Workstream, ...],
) -> dict[str, tuple[str, ...]]:
    refs_by_m365_id: dict[str, list[str]] = {}
    for workstream in workstreams:
        signal_sources = workstream.signal_sources
        if signal_sources is None:
            continue
        for meeting_series in signal_sources.teams_meeting_series:
            normalized_series_id = normalize_thread_id(meeting_series.series_id)
            if not normalized_series_id or not meeting_series.work_item_ids:
                continue
            refs_by_m365_id.setdefault(
                normalized_series_id,
                [],
            ).extend(f"WI:{work_item_id}" for work_item_id in meeting_series.work_item_ids)
        for teams_chat in signal_sources.teams_chats:
            normalized_thread_id = normalize_thread_id(teams_chat.thread_id)
            if not normalized_thread_id or not teams_chat.work_item_ids:
                continue
            refs_by_m365_id.setdefault(
                normalized_thread_id,
                [],
            ).extend(f"WI:{work_item_id}" for work_item_id in teams_chat.work_item_ids)
        for email_thread in signal_sources.email_threads:
            normalized_thread_id = normalize_thread_id(email_thread.thread_id)
            if not normalized_thread_id or not email_thread.work_item_ids:
                continue
            refs_by_m365_id.setdefault(
                normalized_thread_id,
                [],
            ).extend(f"WI:{work_item_id}" for work_item_id in email_thread.work_item_ids)
    return {
        artifact_id: tuple(dict.fromkeys(entity_refs))
        for artifact_id, entity_refs in refs_by_m365_id.items()
    }


def _tracked_m365_artifact_ids(
    workstreams: tuple[Workstream, ...],
    registry_artifacts: tuple[M365RegistryArtifact, ...],
    *,
    feedback_events: tuple[Any, ...] = (),
    as_of: datetime | None = None,
) -> set[str]:
    tracked_ids = _configured_m365_artifact_ids(workstreams)
    tracked_ids.update(
        tracked_registry_thread_ids(
            registry_artifacts,
            feedback_events=feedback_events,
            as_of=as_of,
        )
    )
    return tracked_ids


def _tracked_workstream_ids_by_m365_id(
    registry_artifacts: tuple[M365RegistryArtifact, ...],
    *,
    feedback_events: tuple[Any, ...] = (),
    as_of: datetime | None = None,
) -> dict[str, str]:
    tracked_workstream_ids: dict[str, str] = {}
    for artifact in registry_artifacts:
        if artifact.confidence_source == "pm_rejected":
            continue
        if "recent_rejection" in describe_current_m365_registry_promotion_blockers(
            artifact,
            feedback_events=feedback_events,
            as_of=as_of,
        ):
            continue
        for artifact_id in (
            normalize_thread_id(artifact.thread_id),
            normalize_thread_id(artifact.series_id),
        ):
            if artifact_id is None:
                continue
            tracked_workstream_ids[artifact_id] = artifact.inferred_workstream
    return tracked_workstream_ids


def _observed_m365_thread_ids(signals: tuple[Signal, ...]) -> set[str]:
    observed_ids: set[str] = set()
    for signal in signals:
        thread_id = _signal_thread_id(signal)
        if thread_id is not None:
            observed_ids.add(thread_id)
    return observed_ids


def _observed_m365_series_ids(signals: tuple[Signal, ...]) -> set[str]:
    return {
        observed_id
        for signal in signals
        if signal.source == "workiq/transcript"
        for observed_id in (_signal_thread_id(signal),)
        if observed_id is not None
    }


def _signal_thread_id(signal: Signal) -> str | None:
    if signal.thread_id is not None and signal.thread_id.strip():
        return signal.thread_id.strip()
    metadata = signal.metadata
    if isinstance(metadata, dict):
        raw_thread_id = metadata.get("thread_id")
        if isinstance(raw_thread_id, str) and raw_thread_id.strip():
            return raw_thread_id.strip()
    return None


def _build_icm_query_signals(
    *,
    query: KustoQuery,
    rows: list[dict[str, Any]],
    program_id: str,
    as_of: datetime,
    teams: tuple[Team, ...],
    workstreams: tuple[Workstream, ...],
) -> tuple[Signal, ...]:
    return _build_icm_query_signals_impl(
        query=query,
        rows=rows,
        program_id=program_id,
        as_of=as_of,
        teams=teams,
        workstreams=workstreams,
        resolve_icm_workstream_id_fn=_resolve_icm_workstream_id,
    )


def _build_workiq_signals(
    *,
    program: Program,
    program_id: str,
    as_of: datetime,
    items: tuple[WorkItem, ...],
    workstreams: tuple[Workstream, ...],
    bridge: type[AgencyBridge] | AgencyBridge | Callable[[], AgencyBridge],
    m365_topic_router: IM365TopicRouter | None = None,
    programs_root: Path | None = None,
    timeout_seconds: int | None = None,
    total_budget_seconds: int | None = None,
    integration_error_sink: list[IntegrationError] | None = None,
) -> tuple[Signal, ...]:
    if program.m365 is None or not program.m365.enabled:
        raise typer.BadParameter(f"Program '{program_id}' is missing enabled m365 configuration.")

    bridge_client = bridge() if callable(bridge) else bridge
    capabilities = bridge_client.probe()
    if not capabilities.available or not capabilities.has_workiq:
        raise typer.BadParameter("WorkIQ access is unavailable. Enable Agency CLI WorkIQ support or omit --workiq.")

    signals: list[Signal] = []
    items_by_id = {item.id: item for item in items}
    registry_artifacts = load_m365_registry(program_id, programs_root).artifacts if programs_root is not None else ()
    resolved_topic_router = _resolve_m365_topic_router(program=program, explicit_router=m365_topic_router)
    recent_confirmed_signals_by_workstream: dict[str, tuple[str, ...]] | None = None
    recent_rejected_signals_by_workstream: dict[str, tuple[str, ...]] | None = None
    recent_reassign_corrections_by_workstream: dict[str, tuple[M365ReassignCorrection, ...]] | None = None
    milestones: tuple[Milestone, ...] = ()
    if programs_root is not None:
        feedback_events = read_m365_routing_feedback_events(program_id, programs_root)
        milestones = _load_discovery_milestones(program_id, programs_root=programs_root)
        recent_confirmed_signals_by_workstream = build_m365_corpus_texts_by_workstream(
            workstreams=workstreams,
            registry_artifacts=registry_artifacts,
            feedback_events=feedback_events,
            approved_signals=load_approved_m365_corpus_signals(program_id, as_of=as_of, programs_root=programs_root),
            as_of=as_of,
        )
        recent_rejected_signals_by_workstream = build_m365_rejected_texts_by_workstream(
            workstreams=workstreams,
            registry_artifacts=registry_artifacts,
            feedback_events=feedback_events,
            as_of=as_of,
        )
        recent_reassign_corrections_by_workstream = build_m365_reassign_corrections_by_workstream(
            workstreams=workstreams,
            registry_artifacts=registry_artifacts,
            feedback_events=feedback_events,
            as_of=as_of,
        )
    query_plans = _build_workiq_query_plans(
        program=program,
        workstreams=workstreams,
        items=items,
        milestones=milestones,
        registry_artifacts=registry_artifacts,
        approved_signals_by_workstream=recent_confirmed_signals_by_workstream or {},
        rejected_signals_by_workstream=recent_rejected_signals_by_workstream or {},
        feedback_events=feedback_events if programs_root is not None else (),
        as_of=as_of,
    )
    if not query_plans:
        raise typer.BadParameter(f"Program '{program_id}' is missing workiq query configuration.")
    started_at = monotonic()
    tracked_workstream_ids_by_m365_id = _tracked_workstream_ids_by_m365_id(
        registry_artifacts,
        feedback_events=feedback_events if programs_root is not None else (),
        as_of=as_of,
    )
    for plan in query_plans:
        effective_timeout = timeout_seconds
        if total_budget_seconds is not None:
            remaining_budget = total_budget_seconds - (monotonic() - started_at)
            if remaining_budget <= 0:
                break
            budget_capped_timeout = max(1, int(remaining_budget))
            effective_timeout = budget_capped_timeout if effective_timeout is None else min(effective_timeout, budget_capped_timeout)
        if plan.mcp_tool is None:
            try:
                payload = bridge_client.ask_workiq(
                    plan.question or "",
                    timeout_seconds=effective_timeout,
                    use_cache=not plan.bypass_ask_cache,
                )
            except TypeError:
                try:
                    payload = bridge_client.ask_workiq(plan.question or "", timeout_seconds=effective_timeout)
                except TypeError:
                    payload = bridge_client.ask_workiq(plan.question or "")
            if plan.structured_window_start is not None and plan.structured_window_end is not None:
                payload = validate_structured_discovery_payload(
                    payload,
                    window_start=plan.structured_window_start,
                    window_end=plan.structured_window_end,
                    limit=plan.structured_result_limit or 8,
                )
        elif plan.query_name.startswith("meeting_series:") or plan.mcp_tool == "calendar_gather":
            calendar_client = create_graph_calendar_client(bridge_client)
            page = calendar_client.search_events(
                query=plan.question or "",
                limit=int((plan.tool_args or {}).get("limit", _M365_DISCOVERY_RESULT_LIMIT)),
            )
            transcript_reader = TranscriptReader(bridge_client) if plan.include_transcripts else None
            allowed_ids = {normalize_thread_id(thread_id) for thread_id in plan.allowed_thread_ids if normalize_thread_id(thread_id)}
            meetings_payload: list[dict[str, Any]] = []
            for record in page.records:
                record_meeting_id = normalize_thread_id(record.meeting_id or record.source_id or record.web_url)
                if allowed_ids and record_meeting_id not in allowed_ids:
                    continue
                transcript = (
                    transcript_reader.get_transcript(meeting_id=record.meeting_id)
                    if transcript_reader is not None and record.meeting_id
                    else None
                )
                meetings_payload.append(
                    {
                        "eventId": record.source_id,
                        "meetingId": transcript.meeting_id if transcript and transcript.meeting_id else record.meeting_id,
                        "subject": transcript.title if transcript and transcript.title else record.subject,
                        "summary": transcript.content if transcript and transcript.content else None,
                        "webUrl": transcript.web_url if transcript and transcript.web_url else record.web_url,
                        "startDateTime": transcript.captured_at if transcript and transcript.captured_at else record.start_at,
                        "endDateTime": record.end_at,
                    }
                )
            payload = {
                "meetings": meetings_payload
            }
        else:
            tool_args = dict(plan.tool_args or {})
            if plan.mcp_tool == "search_emails":
                mail_client = create_graph_mail_client(bridge_client)
                mail_page = mail_client.search_emails(
                    query=plan.question or str(tool_args.get("query") or ""),
                    limit=int(tool_args.get("limit", _M365_DISCOVERY_RESULT_LIMIT)),
                )
                payload = {"emails": [_mail_record_payload(record) for record in mail_page.records]}
            elif plan.mcp_tool == "teams_chat":
                teams_reader = TeamsReader(bridge_client)
                teams_page = teams_reader.search_messages(
                    channel=str(tool_args.get("channel") or "all"),
                    query=plan.question or str(tool_args.get("query") or ""),
                    since=_optional_string(tool_args.get("since")),
                    limit=int(tool_args.get("limit", _M365_DISCOVERY_RESULT_LIMIT)),
                )
                payload = {"messages": [_teams_record_payload(record) for record in teams_page.records]}
            else:
                payload = bridge_client.invoke_mcp_tool("workiq", plan.mcp_tool, tool_args)
        signals.extend(
            _signals_from_workiq_payload(
                payload=payload,
                query_name=plan.query_name,
                question=plan.question or "",
                program_id=program_id,
                as_of=as_of,
                items_by_id=items_by_id,
                workstreams=workstreams,
                default_workstream_id=plan.workstream_id,
                tracked_workstream_ids_by_m365_id=tracked_workstream_ids_by_m365_id,
                topic_router=resolved_topic_router if programs_root is not None else None,
                recent_confirmed_signals=recent_confirmed_signals_by_workstream,
                recent_rejected_signals=recent_rejected_signals_by_workstream,
                recent_reassign_corrections=recent_reassign_corrections_by_workstream,
                exclude_keywords=plan.exclude_keywords,
                allowed_thread_ids=plan.allowed_thread_ids,
            )
        )
    deduped_signals = _dedupe_workiq_signals(tuple(signals))
    if programs_root is not None:
        for seeded_resolution_error in _run_seeded_source_resolution_pass(
            program=program,
            program_id=program_id,
            as_of=as_of,
            workstreams=workstreams,
            registry_artifacts=registry_artifacts,
            bridge_client=bridge_client,
            programs_root=programs_root,
        ):
            _append_integration_error_once(
                integration_error_sink,
                source="workiq",
                stage="discovery",
                error=seeded_resolution_error,
            )
        deduped_signals = _reroute_low_confidence_registry_artifacts(
            program_id=program_id,
            as_of=as_of,
            workstreams=workstreams,
            topic_router=resolved_topic_router,
            observed_signals=deduped_signals,
            programs_root=programs_root,
        )
        discovered_artifacts, discovery_errors = _run_m365_discovery_pass(
            program_id=program_id,
            as_of=as_of,
            workstreams=workstreams,
            bridge_client=bridge_client,
            topic_router=resolved_topic_router,
            programs_root=programs_root,
        )
        for discovery_error in discovery_errors:
            _append_integration_error_once(
                integration_error_sink,
                source="workiq",
                stage="discovery",
                error=discovery_error,
            )
        if discovered_artifacts:
            updated_registry_artifacts = load_m365_registry(program_id, programs_root).artifacts
            for seeded_resolution_error in _run_seeded_source_resolution_pass(
                program=program,
                program_id=program_id,
                as_of=as_of,
                workstreams=workstreams,
                registry_artifacts=updated_registry_artifacts,
                bridge_client=bridge_client,
                programs_root=programs_root,
            ):
                _append_integration_error_once(
                    integration_error_sink,
                    source="workiq",
                    stage="discovery",
                    error=seeded_resolution_error,
                )
        observed_thread_ids = set(_observed_m365_thread_ids(deduped_signals))
        observed_thread_ids.update(
            artifact.thread_id
            for artifact in discovered_artifacts
            if artifact.thread_id is not None
        )
        observed_series_ids = _observed_m365_series_ids(deduped_signals)
        observed_series_ids.update(
            artifact.series_id
            for artifact in discovered_artifacts
            if artifact.series_id is not None
        )
        refresh_m365_registry_metrics(
            program_id,
            as_of=as_of,
            observed_thread_ids=tuple(sorted(observed_thread_ids)),
            observed_series_ids=tuple(sorted(observed_series_ids)),
            programs_root=programs_root,
        )
    return deduped_signals


def _run_seeded_source_resolution_pass(
    *,
    program: Program,
    program_id: str,
    as_of: datetime,
    workstreams: tuple[Workstream, ...],
    registry_artifacts: tuple[M365RegistryArtifact, ...],
    bridge_client: AgencyBridge,
    programs_root: Path,
) -> tuple[str, ...]:
    if not registry_artifacts and not any(workstream.signal_sources is not None for workstream in workstreams):
        return ()

    candidate_store = SourceCandidateStore(get_channel_registry_path(program_id, programs_root=programs_root), program_id)
    candidate_store.bootstrap_intents(
        workstreams=workstreams,
        registry_artifacts=registry_artifacts,
        as_of=as_of,
    )
    capabilities = bridge_client.probe()
    available_tools = _available_workiq_tools(capabilities)
    runtime_errors: list[str] = []
    autonomous_run_id = f"seeded-resolution-{uuid.uuid4().hex[:12]}"
    for intent in candidate_store.list_intents():
        if not _should_attempt_seeded_source_resolution(intent=intent, candidate_store=candidate_store, as_of=as_of):
            continue
        topics = _seeded_source_topics_for_intent(
            intent=intent,
            workstreams=workstreams,
            registry_artifacts=registry_artifacts,
        )
        owner_aliases = _seeded_source_owner_aliases_for_intent(intent=intent, workstreams=workstreams)
        started_at = monotonic()
        if intent.ref_kind == SourceRefKind.MEETING_SERIES:
            candidates = _discover_meeting_id_candidates(
                intent.display_name,
                limit=_SEEDED_SOURCE_DISCOVERY_LIMIT,
                topics=topics,
                owner_aliases=owner_aliases,
                bridge=bridge_client,
            )
            artifact_type = "meeting_series"
        elif intent.ref_kind == SourceRefKind.EMAIL_THREAD:
            candidates = _discover_email_thread_candidates(
                intent.display_name,
                limit=_SEEDED_SOURCE_DISCOVERY_LIMIT,
                topics=topics,
                owner_aliases=owner_aliases,
                bridge=bridge_client,
            )
            artifact_type = "email_thread"
        else:
            candidates = _discover_thread_id_candidates(
                intent.display_name,
                limit=_SEEDED_SOURCE_DISCOVERY_LIMIT,
                topics=topics,
                bridge=bridge_client,
            )
            artifact_type = "teams_chat"
        registry_candidates = _registry_seeded_source_candidates(
            intent=intent,
            registry_artifacts=registry_artifacts,
            topics=topics,
        )
        candidates = _merge_registry_id_candidates(candidates, registry_candidates)
        duration_ms = int((monotonic() - started_at) * 1000)
        last_error_getter = getattr(bridge_client, "last_mcp_error", None)
        runtime_error = last_error_getter() if callable(last_error_getter) else None
        if runtime_error:
            runtime_errors.append(runtime_error)
        unavailable_reason = None
        if not candidates:
            unavailable_reason = describe_discovery_unavailable_reason(
                artifact_type=artifact_type,
                agency_available=capabilities.available,
                has_workiq=capabilities.has_workiq,
                workiq_cli_available=capabilities.has_workiq_cli,
                available_tools=available_tools,
                runtime_error=runtime_error,
            )
        persistence = _persist_seeded_source_discovery(
            candidate_store=candidate_store,
            program_id=program_id,
            intent=intent,
            candidates=tuple(candidates),
            topics=topics,
            owner_aliases=owner_aliases,
            available_tools=available_tools,
            registry_artifacts=registry_artifacts,
            as_of=as_of,
            autonomous_run_id=autonomous_run_id,
            unavailable_reason=unavailable_reason,
            duration_ms=duration_ms,
            discovery_limit=_SEEDED_SOURCE_DISCOVERY_LIMIT,
            attempt_ttl_hours=_SEEDED_SOURCE_ATTEMPT_TTL_HOURS,
        )
        auto_resolve_candidate = persistence.auto_resolve_candidate
        if auto_resolve_candidate is not None:
            stale_plan = _attempt_seeded_source_auto_resolution(
                program=program,
                programs_root=programs_root,
                candidate_store=candidate_store,
                intent=intent,
                candidate=auto_resolve_candidate,
                as_of=as_of,
            )
            if stale_plan:
                candidate_store.record_attempt(
                    DiscoveryAttempt(
                        attempt_id=build_discovery_attempt_id(
                            program_id=program_id,
                            intent_id=intent.intent_id,
                            source_provider="seeded_resolution",
                            query_hash=f"{persistence.query_hash}:stale-plan",
                            attempted_at=as_of,
                        ),
                        program_id=program_id,
                        intent_id=intent.intent_id,
                        workstream_id=intent.workstream_id,
                        channel=_channel_for_source_ref_kind(intent.ref_kind),
                        provider_instance_id="default",
                        ref_kind=intent.ref_kind,
                        source_provider="seeded_resolution",
                        query_hash=persistence.query_hash,
                        config_hash=persistence.config_hash,
                        autonomous_run_id=autonomous_run_id,
                        outcome=DiscoveryAttemptOutcome.STALE_PLAN,
                        reason="decision_version changed during gather plan",
                        result_count=len(persistence.allowed_candidates),
                        duration_ms=duration_ms,
                        attempted_at=as_of,
                        expires_at=as_of + timedelta(hours=_SEEDED_SOURCE_ATTEMPT_TTL_HOURS),
                    )
                )
    return tuple(dict.fromkeys(runtime_errors))


def _registry_seeded_source_candidates(
    *,
    intent: SourceIntent,
    registry_artifacts: tuple[M365RegistryArtifact, ...],
    topics: tuple[str, ...],
) -> tuple[RegistryIdCandidate, ...]:
    expected_name = normalize_match_text(intent.display_name)
    expected_topic_tokens = {
        token
        for topic in topics
        for token in tokenize_match_text(topic, drop_generic=True)
        if token
    }
    scored_candidates: list[tuple[RegistryIdCandidate, float]] = []
    for artifact in registry_artifacts:
        ref_kind = _ref_kind_for_registry_artifact(artifact)
        if ref_kind != intent.ref_kind:
            continue
        discovered_id = _registry_artifact_discovered_id(artifact)
        label = (artifact.display_name or artifact.artifact_id).strip()
        if discovered_id is None or not label:
            continue
        exact_match = normalize_match_text(label) == expected_name
        topic_tokens = {
            token
            for topic in artifact.topics
            for token in tokenize_match_text(topic, drop_generic=True)
            if token
        }
        topic_overlap = 0.0
        if expected_topic_tokens:
            topic_overlap = len(expected_topic_tokens & topic_tokens) / len(expected_topic_tokens)
        workstream_bonus = 0.08 if artifact.inferred_workstream == intent.workstream_id else 0.0
        confidence_bonus = min(max(float(artifact.confidence), 0.0), 1.0) * 0.05
        score = 1.0 if exact_match else min(
            0.98,
            candidate_match_score(intent.display_name, label) * 0.82
            + topic_overlap * 0.10
            + workstream_bonus
            + confidence_bonus,
        )
        if not exact_match and score < 0.72:
            continue
        scored_candidates.append(
            (
                RegistryIdCandidate(
                    discovered_id=discovered_id,
                    label=label,
                    source_url=None,
                    exact_match=exact_match,
                    match_score=score,
                ),
                score,
            )
        )
    return rank_registry_id_candidates(scored_candidates)


def _registry_artifact_discovered_id(artifact: M365RegistryArtifact) -> str | None:
    return _service_registry_artifact_discovered_id(artifact)


def _merge_registry_id_candidates(*candidate_sets: tuple[RegistryIdCandidate, ...]) -> tuple[RegistryIdCandidate, ...]:
    scored_candidates: list[tuple[RegistryIdCandidate, float]] = []
    for candidates in candidate_sets:
        for candidate in candidates:
            score = 1.0 if candidate.exact_match else float(candidate.match_score)
            scored_candidates.append((candidate, score))
    return rank_registry_id_candidates(scored_candidates)


def _seeded_candidate_match_origin(
    candidate: RegistryIdCandidate,
    *,
    registry_artifacts: tuple[M365RegistryArtifact, ...],
) -> str:
    return _service_seeded_candidate_match_origin(candidate, registry_artifacts=registry_artifacts)


def _select_seeded_source_auto_resolve_candidate(
    candidates: tuple[Any, ...], *, ref_kind: SourceRefKind
) -> Any | None:
    return _service_select_seeded_source_auto_resolve_candidate(tuple(candidates), ref_kind=ref_kind)


def _attempt_seeded_source_auto_resolution(
    *,
    program: Program,
    programs_root: Path,
    candidate_store: SourceCandidateStore,
    intent: SourceIntent,
    candidate: Any,
    as_of: datetime,
) -> bool:
    channel = _channel_for_source_ref_kind(intent.ref_kind)
    ttl_days: int | None = None
    try:
        channel_config = resolve_channel_config(program, channel, programs_root=programs_root)
    except ConfigError:
        channel_config = None
    if channel_config is not None:
        ttl_days = channel_config.ttl_days

    persisted_candidate = candidate_store.get_candidate_by_ref(
        ref_id=candidate.discovered_id,
        ref_kind=intent.ref_kind,
        channel=channel,
    )
    if persisted_candidate is None:
        return False
    resolution = _accept_candidate_and_resolve_intent(
        candidate_store=candidate_store,
        program_id=intent.program_id,
        programs_root=programs_root,
        intent=intent,
        candidate_id=persisted_candidate.candidate_id,
        as_of=as_of,
        ttl_days=ttl_days,
        actor_alias="vertex.gather",
        scope_prefix="auto",
        auto_resolved=True,
        first_discovered_at_override=persisted_candidate.first_discovered_at,
    )
    if resolution.updated_intent is not None and resolution.accepted_candidate is not None:
        append_intent_decision_log(
            intent.program_id,
            programs_root=programs_root,
            payload=intent_decision_payload(
                ts=as_of,
                intent=intent,
                action="candidate_auto_accept_resolved_intent",
                actor_alias="vertex.gather",
                old_status=intent.status.value,
                new_status=resolution.updated_intent.status.value,
                reason=resolution.accepted_candidate.decision_reason,
                candidate_id=resolution.accepted_candidate.candidate_id,
                ref_id=resolution.accepted_candidate.ref_id,
            ),
        )
    return resolution.stale_plan


def _should_attempt_seeded_source_resolution(
    *,
    intent: SourceIntent,
    candidate_store: SourceCandidateStore,
    as_of: datetime,
) -> bool:
    if intent.ref_kind not in {
        SourceRefKind.MEETING_SERIES,
        SourceRefKind.TEAMS_CHAT,
        SourceRefKind.TEAMS_CHANNEL,
        SourceRefKind.EMAIL_THREAD,
    }:
        return False
    derived_state = candidate_store.derive_intent_state(intent.intent_id, as_of=as_of)
    return derived_state not in {
        SourceIntentStatus.RESOLVED.value,
        SourceIntentStatus.ACTIVE.value,
        SourceIntentStatus.STALE.value,
        SourceIntentStatus.SUPPRESSED.value,
        SourceIntentStatus.SUPERSEDED.value,
        SourceIntentStatus.RETIRED.value,
    }


def _seeded_source_topics_for_intent(
    *,
    intent: SourceIntent,
    workstreams: tuple[Workstream, ...],
    registry_artifacts: tuple[M365RegistryArtifact, ...],
) -> tuple[str, ...]:
    topics: list[str] = []
    workstream = next((item for item in workstreams if item.id == intent.workstream_id), None)
    if workstream is not None and workstream.signal_sources is not None:
        topics.extend(workstream.signal_sources.workiq_keywords)
    for artifact in registry_artifacts:
        if not _artifact_matches_source_intent(artifact=artifact, intent=intent):
            continue
        topics.extend(artifact.topics)
        if artifact.display_name:
            topics.append(artifact.display_name)
    ordered_topics: list[str] = []
    seen: set[str] = set()
    for topic in topics:
        normalized = normalize_intent_display_name(topic)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered_topics.append(topic)
    return tuple(ordered_topics)


def _seeded_source_owner_aliases_for_intent(
    *,
    intent: SourceIntent,
    workstreams: tuple[Workstream, ...],
) -> tuple[str, ...]:
    workstream = next((item for item in workstreams if item.id == intent.workstream_id), None)
    if workstream is None:
        return ()
    owner_values = (
        workstream.pm_owner,
        workstream.eng_owner,
        workstream.accountable_owner,
        workstream.alternate_owner,
        workstream.dri_email,
        workstream.accountable_email,
        *workstream.responsible_owners,
        *workstream.consulted_owners,
        *workstream.informed_owners,
        *workstream.aliases,
    )
    ordered_owners: list[str] = []
    seen: set[str] = set()
    for owner in owner_values:
        if owner is None:
            continue
        normalized = normalize_intent_display_name(owner)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered_owners.append(str(owner))
    return tuple(ordered_owners)


def _artifact_matches_source_intent(*, artifact: M365RegistryArtifact, intent: SourceIntent) -> bool:
    ref_kind = _ref_kind_for_registry_artifact(artifact)
    if ref_kind is None or ref_kind != intent.ref_kind:
        return False
    if artifact.inferred_workstream != intent.workstream_id:
        return False
    return normalize_intent_display_name(artifact.display_name or artifact.artifact_id) == intent.normalized_name


def _ref_kind_for_registry_artifact(artifact: M365RegistryArtifact) -> SourceRefKind | None:
    mapping = {
        "meeting_series": SourceRefKind.MEETING_SERIES,
        "teams_chat": SourceRefKind.TEAMS_CHAT,
        "teams_channel": SourceRefKind.TEAMS_CHANNEL,
        "email_thread": SourceRefKind.EMAIL_THREAD,
    }
    return mapping.get(artifact.artifact_type)


def _seeded_source_attempt_outcome(
    *,
    candidates: tuple[Any, ...],
    unavailable_reason: str | None,
    suppressed_candidate_count: int = 0,
) -> DiscoveryAttemptOutcome:
    return _service_seeded_source_attempt_outcome(
        candidates=tuple(candidates),
        unavailable_reason=unavailable_reason,
        suppressed_candidate_count=suppressed_candidate_count,
    )


def _seeded_source_attempt_reason(
    *,
    unavailable_reason: str | None,
    suppressed_candidate_count: int,
) -> str | None:
    return _service_seeded_source_attempt_reason(
        unavailable_reason=unavailable_reason,
        suppressed_candidate_count=suppressed_candidate_count,
    )


def _available_workiq_tools(capabilities: AgencyCapabilities) -> set[str]:
    configured = {tool.strip() for tool in capabilities.server_tools.get("workiq", ()) if tool.strip()}
    if configured:
        return configured
    if capabilities.has_workiq:
        return set(AgencyBridge._STATIC_ALLOWED_TOOLS.get("workiq", ()))
    return set()


def _channel_for_source_ref_kind(ref_kind: SourceRefKind) -> str:
    return _service_channel_for_source_ref_kind(ref_kind)


def _reroute_low_confidence_registry_artifacts(
    *,
    program_id: str,
    as_of: datetime,
    workstreams: tuple[Workstream, ...],
    topic_router: IM365TopicRouter,
    observed_signals: tuple[Signal, ...],
    programs_root: Path,
) -> tuple[Signal, ...]:
    registry = load_m365_registry(program_id, programs_root)
    if not registry.artifacts:
        return observed_signals

    latest_signal_by_thread_id: dict[str, Signal] = {}
    for signal in observed_signals:
        thread_id = _signal_thread_id(signal)
        if thread_id is None:
            continue
        current = latest_signal_by_thread_id.get(thread_id)
        if current is None or signal.timestamp >= current.timestamp:
            latest_signal_by_thread_id[thread_id] = signal

    if not latest_signal_by_thread_id:
        return observed_signals

    feedback_events = read_m365_routing_feedback_events(program_id, programs_root)
    recent_confirmed_signals_by_workstream = build_m365_corpus_texts_by_workstream(
        workstreams=workstreams,
        registry_artifacts=registry.artifacts,
        feedback_events=feedback_events,
        approved_signals=load_approved_m365_corpus_signals(program_id, as_of=as_of, programs_root=programs_root),
        as_of=as_of,
    )
    recent_rejected_signals_by_workstream = build_m365_rejected_texts_by_workstream(
        workstreams=workstreams,
        registry_artifacts=registry.artifacts,
        feedback_events=feedback_events,
        as_of=as_of,
    )
    recent_reassign_corrections_by_workstream = build_m365_reassign_corrections_by_workstream(
        workstreams=workstreams,
        registry_artifacts=registry.artifacts,
        feedback_events=feedback_events,
        as_of=as_of,
    )

    rerouted_artifacts: list[M365RegistryArtifact] = []
    rerouted_workstreams_by_thread_id: dict[str, str] = {}
    for artifact in registry.artifacts:
        if artifact.thread_id is None:
            continue
        if artifact.pm_confirmed or artifact.confidence_source == "pm_rejected" or artifact.confidence >= 0.60:
            continue
        if "recent_rejection" in describe_current_m365_registry_promotion_blockers(
            artifact,
            feedback_events=feedback_events,
            as_of=as_of,
        ):
            continue
        latest_signal = latest_signal_by_thread_id.get(artifact.thread_id)
        if latest_signal is None:
            continue

        participant_aliases = _signal_participant_aliases(latest_signal)
        decision = topic_router.route_artifact(
            display_name=artifact.display_name,
            subject_or_title=artifact.display_name,
            participant_aliases=participant_aliases,
            sample_text=latest_signal.text,
            workstream_profiles=workstreams,
            recent_confirmed_signals=recent_confirmed_signals_by_workstream,
            recent_rejected_signals=recent_rejected_signals_by_workstream,
            recent_reassign_corrections=recent_reassign_corrections_by_workstream,
        )
        rerouted_workstream_id = decision.workstream_id or artifact.inferred_workstream
        rerouted_workstreams_by_thread_id[artifact.thread_id] = rerouted_workstream_id
        rerouted_artifacts.append(
            replace(
                artifact,
                inferred_workstream=rerouted_workstream_id,
                confidence=decision.confidence,
                confidence_source=decision.confidence_source,
                topics=decision.topics or artifact.topics,
                routing_reasoning=decision.reasoning,
            )
        )

    if not rerouted_artifacts:
        return observed_signals

    upsert_m365_registry_artifacts(
        program_id,
        artifacts=tuple(rerouted_artifacts),
        programs_root=programs_root,
        as_of=as_of,
    )

    rerouted_signals: list[Signal] = []
    for signal in observed_signals:
        thread_id = _signal_thread_id(signal)
        if thread_id is None:
            rerouted_signals.append(signal)
            continue
        found_workstream_id = rerouted_workstreams_by_thread_id.get(thread_id)
        if found_workstream_id is None or found_workstream_id == signal.workstream_id:
            rerouted_signals.append(signal)
            continue

        metadata = dict(signal.metadata or {})
        message_id = metadata.get("message_id")
        next_entity_refs = merge_entity_refs(
            provider_refs=tuple(ref for ref in signal.entity_refs if not ref.startswith("WS:")),
            workstream_id=found_workstream_id,
        )
        # BL-F2: rerouting REPLACES workstream_ids too (replace() otherwise carries the old plural set forward).
        if not isinstance(message_id, str) or not message_id.strip():
            rerouted_signals.append(
                replace(
                    signal,
                    workstream_id=found_workstream_id,
                    workstream_ids=(found_workstream_id,),
                    entity_refs=next_entity_refs,
                )
            )
            continue

        metadata["routed_workstream_id"] = found_workstream_id
        rerouted_signals.append(
            replace(
                signal,
                id=str(
                    uuid5(
                        NAMESPACE_URL,
                        _workiq_signal_identity(
                            program_id=signal.program_id,
                            source=signal.source,
                            message_id=message_id,
                            timestamp=signal.timestamp,
                            workstream_id=found_workstream_id,
                        ),
                    )
                ),
                workstream_id=found_workstream_id,
                workstream_ids=(found_workstream_id,),
                entity_refs=next_entity_refs,
                metadata=metadata,
            )
        )

    return _dedupe_workiq_signals(tuple(rerouted_signals))


def _resolve_m365_topic_router(
    *,
    program: Program,
    explicit_router: IM365TopicRouter | None,
) -> IM365TopicRouter:
    if explicit_router is not None:
        return explicit_router
    if program.ai is not None and program.ai.enabled:
        try:
            return M365TopicRouter.from_program(program)
        except M365TopicRouterError as error:
            log.warning("AI M365 topic router unavailable for %s; falling back to deterministic router: %s", program.id, error)
    return KeywordM365TopicRouter()


def _run_m365_discovery_pass(
    *,
    program_id: str,
    as_of: datetime,
    workstreams: tuple[Workstream, ...],
    bridge_client: AgencyBridge,
    topic_router: IM365TopicRouter,
    programs_root: Path,
) -> tuple[tuple[M365RegistryArtifact, ...], tuple[str, ...]]:
    return _m365_run_discovery_pass(
        program_id=program_id,
        as_of=as_of,
        workstreams=workstreams,
        bridge_client=bridge_client,
        topic_router=topic_router,
        programs_root=programs_root,
        result_limit=_M365_DISCOVERY_RESULT_LIMIT,
    )


def _mail_record_payload(record: Any) -> dict[str, Any]:
    return {
        "id": record.source_id,
        "subject": record.subject,
        "from": {"emailAddress": {"address": record.sender}} if record.sender else None,
        "toRecipients": [{"emailAddress": {"address": recipient}} for recipient in record.recipients],
        "receivedDateTime": record.received_at,
        "webUrl": record.web_url,
        "bodyPreview": record.preview,
        "threadId": record.thread_id,
        "conversationId": record.conversation_id,
    }


def _teams_record_payload(record: Any) -> dict[str, Any]:
    return {
        "id": record.source_id,
        "channel": record.channel,
        "from": {"user": {"displayName": record.sender}} if record.sender else None,
        "createdDateTime": record.sent_at,
        "webUrl": record.web_url,
        "bodyPreview": record.preview,
        "threadId": record.thread_id,
        "conversationId": record.conversation_id,
        "title": record.title,
    }


def _build_m365_discovery_queries(
    *,
    workstreams: tuple[Workstream, ...],
    registry_artifacts: tuple[M365RegistryArtifact, ...],
    feedback_events: tuple[Any, ...] = (),
    as_of: datetime | None = None,
) -> tuple[str, ...]:
    return _core_build_m365_discovery_queries(
        workstreams=workstreams,
        registry_artifacts=registry_artifacts,
        feedback_events=feedback_events,
        as_of=as_of,
    )


def _build_m365_discovery_query_for_workstream(
    *,
    workstream: Workstream,
    registry_artifacts: tuple[M365RegistryArtifact, ...],
    feedback_events: tuple[Any, ...] = (),
    as_of: datetime | None = None,
) -> str | None:
    return _core_build_m365_discovery_query_for_workstream(
        workstream=workstream,
        registry_artifacts=registry_artifacts,
        feedback_events=feedback_events,
        as_of=as_of,
    )


def _build_m365_discovery_query(
    *,
    workstreams: tuple[Workstream, ...],
    registry_artifacts: tuple[M365RegistryArtifact, ...],
    feedback_events: tuple[Any, ...] = (),
    as_of: datetime | None = None,
) -> str | None:
    return _core_build_m365_discovery_query(
        workstreams=workstreams,
        registry_artifacts=registry_artifacts,
        feedback_events=feedback_events,
        as_of=as_of,
    )


def _build_workiq_query_plans(
    *,
    program: Program,
    workstreams: tuple[Workstream, ...],
    items: tuple[WorkItem, ...] = (),
    milestones: tuple[Milestone, ...] = (),
    registry_artifacts: tuple[M365RegistryArtifact, ...] = (),
    approved_signals_by_workstream: dict[str, tuple[str, ...]] | None = None,
    rejected_signals_by_workstream: dict[str, tuple[str, ...]] | None = None,
    feedback_events: tuple[Any, ...] = (),
    as_of: datetime | None = None,
) -> tuple[_WorkIQQueryPlan, ...]:
    if program.m365 is None or not program.m365.enabled:
        return ()

    plans: list[_WorkIQQueryPlan] = []
    legacy_queries = dict(program.m365.workiq_queries or {})
    approved_signals_by_workstream = approved_signals_by_workstream or {}
    rejected_signals_by_workstream = rejected_signals_by_workstream or {}

    configured_artifact_ids = _configured_m365_artifact_ids(workstreams)
    for workstream in workstreams:
        signal_sources = workstream.signal_sources
        # base_keywords = authored (or registry-fallback) keywords used for *precise*
        # thread-targeted queries, which already carry the exact display + durable id.
        # The adaptively-expanded `keywords` below (ADO/milestone/approved-signal terms)
        # are for the broad NL/exploration queries only — injecting them into a targeted
        # query just adds noise (e.g. "mail"/"chat" tokens).
        base_keywords = signal_sources.workiq_keywords if signal_sources is not None else ()
        exclude_keywords = signal_sources.workiq_exclude_keywords if signal_sources is not None else ()
        if not base_keywords:
            base_keywords = _registry_keywords_for_workstream(
                workstream.id,
                registry_artifacts,
                feedback_events=feedback_events,
                as_of=as_of,
            )
        keywords, exclude_keywords, exploration_terms = _build_adaptive_workiq_terms(
            workstream=workstream,
            items=items,
            milestones=milestones,
            base_keywords=base_keywords,
            exclude_keywords=exclude_keywords,
            approved_signals=approved_signals_by_workstream.get(workstream.id, ()),
            rejected_signals=rejected_signals_by_workstream.get(workstream.id, ()),
        )
        meeting_series_entries = signal_sources.teams_meeting_series if signal_sources is not None else ()
        for meeting_series in meeting_series_entries:
            normalized_series_id = normalize_thread_id(meeting_series.series_id)
            if normalized_series_id is None:
                continue
            search_query = _build_m365_artifact_search_query(
                display_name=meeting_series.display_name,
                artifact_id=normalized_series_id,
                keywords=base_keywords,
            )
            plans.append(
                _WorkIQQueryPlan(
                    query_name=f"meeting_series:{workstream.id}:{normalized_series_id}",
                    question=search_query,
                    workstream_id=workstream.id,
                    exclude_keywords=exclude_keywords,
                    mcp_tool="calendar_gather",
                    tool_args={"query": search_query, "limit": _M365_DISCOVERY_RESULT_LIMIT},
                    allowed_thread_ids=(normalized_series_id,),
                    include_transcripts=meeting_series.include_transcripts,
                )
            )
        email_threads = signal_sources.email_threads if signal_sources is not None else ()
        for email_thread in email_threads:
            search_query = _build_m365_artifact_search_query(
                display_name=email_thread.display_name,
                artifact_id=email_thread.thread_id,
                keywords=base_keywords,
            )
            plans.append(
                _WorkIQQueryPlan(
                    query_name=f"email_thread:{workstream.id}:{email_thread.thread_id}",
                    question=search_query,
                    workstream_id=workstream.id,
                    exclude_keywords=exclude_keywords,
                    mcp_tool="search_emails",
                    tool_args={"query": search_query, "limit": _M365_DISCOVERY_RESULT_LIMIT},
                    allowed_thread_ids=(email_thread.thread_id,),
                )
            )
        teams_chats = signal_sources.teams_chats if signal_sources is not None else ()
        for teams_chat in teams_chats:
            normalized_thread_id = normalize_thread_id(teams_chat.thread_id)
            if normalized_thread_id is None:
                continue
            search_query = _build_m365_artifact_search_query(
                display_name=teams_chat.display_name,
                artifact_id=normalized_thread_id,
                keywords=base_keywords,
            )
            plans.append(
                _WorkIQQueryPlan(
                    query_name=f"teams_chat:{workstream.id}:{normalized_thread_id}",
                    question=search_query,
                    workstream_id=workstream.id,
                    exclude_keywords=exclude_keywords,
                    mcp_tool="teams_chat",
                    tool_args={"channel": "all", "query": search_query, "limit": _M365_DISCOVERY_RESULT_LIMIT},
                    allowed_thread_ids=(normalized_thread_id,),
                )
            )
        for artifact in _targeted_registry_artifacts_for_workstream(
            workstream_id=workstream.id,
            registry_artifacts=registry_artifacts,
            configured_artifact_ids=configured_artifact_ids,
            feedback_events=feedback_events,
            as_of=as_of,
        ):
            normalized_artifact_id = normalize_thread_id(artifact.series_id or artifact.thread_id)
            if normalized_artifact_id is None:
                continue
            search_query = _build_m365_artifact_search_query(
                display_name=artifact.display_name or "",
                artifact_id=normalized_artifact_id,
                keywords=keywords,
            )
            if artifact.artifact_type == "meeting_series":
                plans.append(
                    _WorkIQQueryPlan(
                        query_name=f"meeting_series:{workstream.id}:{normalized_artifact_id}",
                        question=search_query,
                        workstream_id=workstream.id,
                        exclude_keywords=exclude_keywords,
                        mcp_tool="calendar_gather",
                        tool_args={"query": search_query, "limit": _M365_DISCOVERY_RESULT_LIMIT},
                        allowed_thread_ids=(normalized_artifact_id,),
                        include_transcripts=True,
                    )
                )
            elif artifact.artifact_type == "email_thread":
                plans.append(
                    _WorkIQQueryPlan(
                        query_name=f"email_thread:{workstream.id}:{normalized_artifact_id}",
                        question=search_query,
                        workstream_id=workstream.id,
                        exclude_keywords=exclude_keywords,
                        mcp_tool="search_emails",
                        tool_args={"query": search_query, "limit": _M365_DISCOVERY_RESULT_LIMIT},
                        allowed_thread_ids=(normalized_artifact_id,),
                    )
                )
            elif artifact.artifact_type == "teams_channel":
                plans.append(
                    _WorkIQQueryPlan(
                        query_name=f"teams_chat:{workstream.id}:{normalized_artifact_id}",
                        question=search_query,
                        workstream_id=workstream.id,
                        exclude_keywords=exclude_keywords,
                        mcp_tool="teams_chat",
                        tool_args={"channel": "all", "query": search_query, "limit": _M365_DISCOVERY_RESULT_LIMIT},
                        allowed_thread_ids=(normalized_artifact_id,),
                    )
                )
        # G10/G13 fix: generate an email subject search from email_subject_filters.
        # email_subject_filters is an explicit, operator-configured email signal source
        # (distinct from workiq_keywords, which drives general WorkIQ feedback search).
        # It is suppressed only when precise email_threads are configured for the
        # workstream — threads target the same email source more precisely, so emitting
        # a broad subject search alongside them would double-count. Keywords do NOT
        # suppress it, so a workstream can carry both feedback-search and email coverage.
        email_subject_filters = signal_sources.email_subject_filters if signal_sources else ()
        if not email_threads and email_subject_filters:
            or_joined_filters = " OR ".join(f'"{kw.strip()}"' for kw in email_subject_filters if kw.strip())
            if or_joined_filters:
                plans.append(
                    _WorkIQQueryPlan(
                        query_name=f"subject_filter:{workstream.id}",
                        question=f"Search emails about: {or_joined_filters}",
                        workstream_id=workstream.id,
                        exclude_keywords=exclude_keywords,
                        mcp_tool="search_emails",
                        tool_args={"query": or_joined_filters, "limit": _M365_DISCOVERY_RESULT_LIMIT},
                    )
                )
        if keywords:
            if legacy_queries:
                for base_query_name, base_question in legacy_queries.items():
                    plans.append(
                        _WorkIQQueryPlan(
                            query_name=f"{base_query_name}:{workstream.id}",
                            question=_build_scoped_workiq_question(
                                workstream_name=workstream.name,
                                base_question=base_question,
                                keywords=keywords,
                                exclude_keywords=exclude_keywords,
                            ),
                            workstream_id=workstream.id,
                            exclude_keywords=exclude_keywords,
                            discovery_terms=keywords,
                        )
                    )
            else:
                plans.append(
                    _WorkIQQueryPlan(
                        query_name=f"feedback_search:{workstream.id}",
                        question=_build_scoped_workiq_question(
                            workstream_name=workstream.name,
                            base_question=None,
                            keywords=keywords,
                            exclude_keywords=exclude_keywords,
                        ),
                        workstream_id=workstream.id,
                        exclude_keywords=exclude_keywords,
                        discovery_terms=keywords,
                    )
                    )
            continue

        if exploration_terms:
            if legacy_queries:
                # Never drop operator-authored workiq_queries: scope each one with the
                # adaptive exploration terms instead of replacing it with a generic probe.
                for base_query_name, base_question in legacy_queries.items():
                    plans.append(
                        _WorkIQQueryPlan(
                            query_name=f"{base_query_name}:{workstream.id}",
                            question=_build_scoped_workiq_question(
                                workstream_name=workstream.name,
                                base_question=base_question,
                                keywords=exploration_terms,
                                exclude_keywords=exclude_keywords,
                            ),
                            workstream_id=workstream.id,
                            exclude_keywords=exclude_keywords,
                            discovery_terms=exploration_terms,
                        )
                    )
            else:
                plans.append(
                    _WorkIQQueryPlan(
                        query_name=f"feedback_explore:{workstream.id}",
                        question=_build_scoped_workiq_question(
                            workstream_name=workstream.name,
                            base_question="Find recent decisions, risks, blockers, and stakeholder feedback",
                            keywords=exploration_terms,
                            exclude_keywords=exclude_keywords,
                        ),
                        workstream_id=workstream.id,
                        exclude_keywords=exclude_keywords,
                        discovery_terms=exploration_terms,
                    )
                )
            continue

        fallback_question = legacy_queries.get(workstream.id)
        if isinstance(fallback_question, str) and fallback_question.strip():
            plans.append(
                _WorkIQQueryPlan(
                    query_name=workstream.id,
                    question=fallback_question.strip(),
                    workstream_id=workstream.id,
                    discovery_terms=(workstream.name,),
                )
            )

    if plans:
        return _apply_structured_workiq_discovery(
            plans=tuple(plans),
            program=program,
            workstreams=workstreams,
            as_of=as_of,
        )

    return tuple(
        _WorkIQQueryPlan(query_name=query_name, question=question)
        for query_name, question in legacy_queries.items()
        if isinstance(question, str) and question.strip()
    )


def _build_adaptive_workiq_terms(
    *,
    workstream: Workstream,
    items: tuple[WorkItem, ...],
    milestones: tuple[Milestone, ...],
    base_keywords: tuple[str, ...],
    exclude_keywords: tuple[str, ...],
    approved_signals: tuple[str, ...],
    rejected_signals: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    effective_keywords = _dedupe_workiq_terms(base_keywords)
    learned_keywords = suggest_keyword_expansions(
        existing_keywords=effective_keywords,
        texts=approved_signals,
        max_suggestions=_ADAPTIVE_WORKIQ_LEARNED_KEYWORD_LIMIT,
        min_frequency=1,
    )
    # Drop structural source-type labels (e.g. "chat"/"mail"/"thread") mined from
    # artifact display names — they are noise as search keywords, not content.
    learned_keywords = tuple(
        kw for kw in learned_keywords if kw.strip().lower() not in _ADAPTIVE_KEYWORD_STOPWORDS
    )
    if learned_keywords:
        effective_keywords = _dedupe_workiq_terms((*effective_keywords, *learned_keywords))

    effective_excludes = _dedupe_workiq_terms(exclude_keywords)
    learned_excludes = suggest_keyword_expansions(
        existing_keywords=effective_excludes,
        texts=rejected_signals,
        max_suggestions=_ADAPTIVE_WORKIQ_LEARNED_KEYWORD_LIMIT,
        min_frequency=1,
    )
    if learned_excludes:
        effective_excludes = _dedupe_workiq_terms((*effective_excludes, *learned_excludes))

    exploration_terms = _derive_workiq_exploration_terms(
        workstream=workstream,
        items=items,
        milestones=milestones,
        existing_keywords=effective_keywords,
        exclude_keywords=effective_excludes,
    )
    return effective_keywords, effective_excludes, exploration_terms


def _derive_workiq_exploration_terms(
    *,
    workstream: Workstream,
    items: tuple[WorkItem, ...],
    milestones: tuple[Milestone, ...],
    existing_keywords: tuple[str, ...],
    exclude_keywords: tuple[str, ...],
) -> tuple[str, ...]:
    item_titles = tuple(
        item.title
        for item in items
        if item.title.strip() and _resolve_workstream_id(item.area_path, (workstream,)) == workstream.id
    )
    title_terms = suggest_keyword_expansions(
        existing_keywords=existing_keywords,
        texts=item_titles,
        max_suggestions=_ADAPTIVE_WORKIQ_EXPLORATION_TERM_LIMIT,
        min_frequency=1,
    )
    terms = list(title_terms)
    milestone_names = tuple(
        milestone.name
        for milestone in milestones
        if milestone.name.strip() and workstream.id in milestone.linked_workstream_ids
    )
    milestone_terms = suggest_keyword_expansions(
        existing_keywords=(*existing_keywords, *tuple(terms)),
        texts=milestone_names,
        max_suggestions=_ADAPTIVE_WORKIQ_EXPLORATION_TERM_LIMIT,
        min_frequency=1,
    )
    for term in milestone_terms:
        if len(terms) >= _ADAPTIVE_WORKIQ_EXPLORATION_TERM_LIMIT:
            break
        terms.append(term)
    for alias in _workstream_owner_aliases(workstream):
        if len(terms) >= _ADAPTIVE_WORKIQ_EXPLORATION_TERM_LIMIT:
            break
        terms.append(alias)
    return tuple(
        term
        for term in _dedupe_workiq_terms(tuple(terms))
        if not _workiq_term_is_excluded(term, existing_keywords=existing_keywords, exclude_keywords=exclude_keywords)
    )[:_ADAPTIVE_WORKIQ_EXPLORATION_TERM_LIMIT]


def _workstream_owner_aliases(workstream: Workstream) -> tuple[str, ...]:
    aliases: list[str] = []
    for value in (
        workstream.pm_owner,
        workstream.eng_owner,
        workstream.alternate_owner,
        workstream.accountable_owner,
        workstream.dri_email,
        *workstream.aliases,
    ):
        alias = _normalize_workiq_alias(value)
        if alias:
            aliases.append(alias)
    return _dedupe_workiq_terms(tuple(aliases))


def _normalize_workiq_alias(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip().lower()
    if not text:
        return None
    if "@" in text:
        text = text.split("@", 1)[0].strip()
    else:
        text = text.split()[0].strip()
    return text or None


def _dedupe_workiq_terms(terms: tuple[str, ...]) -> tuple[str, ...]:
    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in terms:
        normalized = " ".join(candidate.split())
        if not normalized:
            continue
        lowered = normalized.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        deduped.append(normalized)
    return tuple(deduped)


def _workiq_term_is_excluded(
    term: str,
    *,
    existing_keywords: tuple[str, ...],
    exclude_keywords: tuple[str, ...],
) -> bool:
    normalized = term.strip().lower()
    if not normalized:
        return True
    existing = {keyword.strip().lower() for keyword in existing_keywords if keyword.strip()}
    excluded = {keyword.strip().lower() for keyword in exclude_keywords if keyword.strip()}
    return normalized in existing or normalized in excluded


def _load_discovery_milestones(program_id: str, *, programs_root: Path) -> tuple[Milestone, ...]:
    try:
        # INV-SG-10: migrated readers must read current state via the Program Fact
        # Store, not the raw milestone-engine loader. Names feed adaptive discovery.
        return load_current_milestones(program_id, programs_root=programs_root)
    except (ConfigError, OSError, TypeError, ValueError):
        return ()


def _build_adaptive_workiq_state(
    *,
    program_id: str,
    programs_root: Path,
    workstreams: tuple[Workstream, ...],
    items: tuple[WorkItem, ...],
    milestones: tuple[Milestone, ...],
    registry_artifacts: tuple[M365RegistryArtifact, ...],
    feedback_events: tuple[Any, ...],
    as_of: datetime | None,
) -> dict[str, Any]:
    approved_signals_by_workstream = build_m365_corpus_texts_by_workstream(
        workstreams=workstreams,
        registry_artifacts=registry_artifacts,
        feedback_events=feedback_events,
        approved_signals=load_approved_m365_corpus_signals(program_id, as_of=as_of or datetime.now(timezone.utc), programs_root=programs_root),
        as_of=as_of,
    )
    rejected_signals_by_workstream = build_m365_rejected_texts_by_workstream(
        workstreams=workstreams,
        registry_artifacts=registry_artifacts,
        feedback_events=feedback_events,
        as_of=as_of,
    )
    source_yield_by_workstream = _load_workstream_source_yield_state(
        program_id=program_id,
        programs_root=programs_root,
        workstreams=workstreams,
    )

    profiles: dict[str, dict[str, Any]] = {}
    for workstream in workstreams:
        signal_sources = workstream.signal_sources
        base_keywords = signal_sources.workiq_keywords if signal_sources is not None else ()
        exclude_keywords = signal_sources.workiq_exclude_keywords if signal_sources is not None else ()
        if not base_keywords:
            base_keywords = _registry_keywords_for_workstream(
                workstream.id,
                registry_artifacts,
                feedback_events=feedback_events,
                as_of=as_of,
            )
        effective_keywords, effective_excludes, exploration_terms = _build_adaptive_workiq_terms(
            workstream=workstream,
            items=items,
            milestones=milestones,
            base_keywords=base_keywords,
            exclude_keywords=exclude_keywords,
            approved_signals=approved_signals_by_workstream.get(workstream.id, ()),
            rejected_signals=rejected_signals_by_workstream.get(workstream.id, ()),
        )
        source_yield = source_yield_by_workstream.get(workstream.id, {"active_source_count": 0, "yield_total_last_3": 0, "top_sources": []})
        profiles[workstream.id] = {
            "effective_keywords": list(effective_keywords),
            "effective_excludes": list(effective_excludes),
            "exploration_terms": list(exploration_terms),
            "approved_signal_count": len(approved_signals_by_workstream.get(workstream.id, ())),
            "rejected_signal_count": len(rejected_signals_by_workstream.get(workstream.id, ())),
            "active_source_count": int(source_yield["active_source_count"]),
            "yield_total_last_3": int(source_yield["yield_total_last_3"]),
            "top_sources": list(source_yield["top_sources"]),
        }
    return {
        "workstream_count": len(profiles),
        "workstreams": profiles,
    }


def _load_workstream_source_yield_state(
    *,
    program_id: str,
    programs_root: Path,
    workstreams: tuple[Workstream, ...],
) -> dict[str, dict[str, Any]]:
    db_path = resolve_channel_registry_path_for_read(program_id, programs_root=programs_root)
    if not db_path.exists():
        return {}
    store = ChannelRegistryStore(db_path, program_id, ensure_schema=False)
    snapshots: dict[str, dict[str, Any]] = {}
    for workstream in workstreams:
        sources: list[dict[str, Any]] = []
        for channel in store.registered_channels():
            for registration in store.active_registrations(channel, workstream_id=workstream.id):
                yield_total = sum(registration.signal_yield_last_3)
                sources.append(
                    {
                        "channel": registration.channel,
                        "ref_kind": registration.ref_kind,
                        "ref_id": registration.ref_id,
                        "ref_title": registration.ref_title,
                        "signal_yield_last_3": list(registration.signal_yield_last_3),
                        "yield_total_last_3": yield_total,
                    }
                )
        sources.sort(
            key=lambda entry: (
                -int(entry["yield_total_last_3"]),
                -int((entry["signal_yield_last_3"] or [0])[0]),
                str(entry["ref_title"] or entry["ref_id"]),
            )
        )
        snapshots[workstream.id] = {
            "active_source_count": len(sources),
            "yield_total_last_3": sum(int(entry["yield_total_last_3"]) for entry in sources),
            "top_sources": sources[:3],
        }
    return snapshots


def _build_m365_artifact_search_query(
    *,
    display_name: str,
    artifact_id: str,
    keywords: tuple[str, ...],
) -> str:
    terms: list[str] = []
    seen: set[str] = set()
    for candidate in (display_name, artifact_id, *keywords):
        normalized = candidate.strip()
        if not normalized:
            continue
        lowered = normalized.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        terms.append(f'"{normalized}"' if " " in normalized else normalized)
    return " OR ".join(terms[:12])


def _registry_keywords_for_workstream(
    workstream_id: str,
    registry_artifacts: tuple[M365RegistryArtifact, ...],
    *,
    feedback_events: tuple[Any, ...] = (),
    as_of: datetime | None = None,
) -> tuple[str, ...]:
    return _core_registry_keywords_for_workstream(
        workstream_id,
        registry_artifacts,
        feedback_events=feedback_events,
        as_of=as_of,
    )


def _targeted_registry_artifacts_for_workstream(
    *,
    workstream_id: str,
    registry_artifacts: tuple[M365RegistryArtifact, ...],
    configured_artifact_ids: set[str],
    feedback_events: tuple[Any, ...] = (),
    as_of: datetime | None = None,
) -> tuple[M365RegistryArtifact, ...]:
    targeted_artifacts: list[M365RegistryArtifact] = []
    seen_ids: set[str] = set()
    for artifact in registry_artifacts:
        if artifact.inferred_workstream != workstream_id:
            continue
        if not artifact.pm_confirmed and not _artifact_meets_auto_promotion_confidence_gate(artifact):
            continue
        if "recent_rejection" in describe_current_m365_registry_promotion_blockers(
            artifact,
            feedback_events=feedback_events,
            as_of=as_of,
        ):
            continue
        normalized_artifact_id = normalize_thread_id(artifact.series_id or artifact.thread_id)
        if normalized_artifact_id is None:
            continue
        if normalized_artifact_id in configured_artifact_ids or normalized_artifact_id in seen_ids:
            continue
        if artifact.artifact_type not in {"meeting_series", "email_thread", "teams_channel"}:
            continue
        seen_ids.add(normalized_artifact_id)
        targeted_artifacts.append(artifact)
    return tuple(targeted_artifacts)


def _build_scoped_workiq_question(
    *,
    workstream_name: str,
    base_question: str | None,
    keywords: tuple[str, ...],
    exclude_keywords: tuple[str, ...],
) -> str:
    kw_string = ", ".join(keyword for keyword in keywords if keyword.strip())
    exclude_suffix = f" Exclude: {', '.join(exclude_keywords)}." if exclude_keywords else ""
    if base_question is not None and base_question.strip():
        return f"{base_question.strip()}. Focus on {workstream_name}. Keywords: {kw_string}.{exclude_suffix}".strip()
    return f"What is the latest status on {workstream_name}? Focus on: {kw_string}.{exclude_suffix}".strip()


def _signals_from_workiq_payload(
    *,
    payload: dict[str, Any] | None,
    query_name: str,
    question: str,
    program_id: str,
    as_of: datetime,
    items_by_id: dict[int, WorkItem],
    workstreams: tuple[Workstream, ...],
    default_workstream_id: str | None = None,
    tracked_workstream_ids_by_m365_id: dict[str, str] | None = None,
    topic_router: IM365TopicRouter | None = None,
    recent_confirmed_signals: dict[str, tuple[str, ...]] | None = None,
    recent_rejected_signals: dict[str, tuple[str, ...]] | None = None,
    recent_reassign_corrections: dict[str, tuple[M365ReassignCorrection, ...]] | None = None,
    exclude_keywords: tuple[str, ...] = (),
    allowed_thread_ids: tuple[str, ...] = (),
) -> tuple[Signal, ...]:
    if payload is None:
        return ()

    source_type = _workiq_source_type(query_name)
    source = f"workiq/{source_type}"
    records = _workiq_payload_records(payload)
    configured_entity_refs_by_m365_id = _configured_work_item_entity_refs_by_m365_id(workstreams)
    if not records:
        response = _optional_string(payload.get("response") or payload.get("summary"))
        if response is None:
            return ()
        records = [{"summary": response, "conversationId": payload.get("conversationId")}]

    signals: list[Signal] = []
    for index, record in enumerate(records):
        thread_id = _workiq_thread_id(record)
        if allowed_thread_ids and thread_id not in allowed_thread_ids:
            continue
        subject = _workiq_subject(record)
        preview = _workiq_preview(record) or _optional_string(record.get("summary"))
        raw_text = "\n".join(part for part in (subject, preview) if part)
        if _workiq_record_matches_exclusions(raw_text, exclude_keywords=exclude_keywords):
            continue
        configured_thread_entity_refs = configured_entity_refs_by_m365_id.get(thread_id or "", ())
        record_entity_refs = _merge_workiq_entity_refs(
            _extract_work_item_refs(raw_text),
            configured_thread_entity_refs,
        )
        workstream_id = _resolve_workiq_signal_workstream_id(
            entity_refs=record_entity_refs,
            items_by_id=items_by_id,
            workstreams=workstreams,
        )
        routed_workstream_id: str | None = None
        if workstream_id is None and thread_id is not None and tracked_workstream_ids_by_m365_id is not None:
            workstream_id = tracked_workstream_ids_by_m365_id.get(thread_id)
        participant_aliases = _workiq_participant_aliases(record)
        if workstream_id is None and topic_router is not None:
            decision = topic_router.route_artifact(
                display_name=_optional_string(record.get("channel") or record.get("teamChannel")),
                subject_or_title=subject,
                participant_aliases=participant_aliases,
                sample_text=preview,
                workstream_profiles=workstreams,
                recent_confirmed_signals=recent_confirmed_signals,
                recent_rejected_signals=recent_rejected_signals,
                recent_reassign_corrections=recent_reassign_corrections,
            )
            workstream_id = decision.workstream_id
            routed_workstream_id = decision.workstream_id
        if workstream_id is None:
            workstream_id = default_workstream_id
        timestamp = _workiq_timestamp(record, as_of=as_of)
        message_id = _workiq_message_id(
            record,
            program_id=program_id,
            query_name=query_name,
            question=question,
            index=index,
            fallback_text=raw_text or _optional_string(record.get("summary")) or "workiq",
            timestamp=timestamp,
        )
        sender = _workiq_sender(record)
        sender_alias = _sender_alias(sender)
        fragments = _workiq_signal_fragments(subject=subject, preview=preview)
        for segment_index, fragment_text in enumerate(fragments):
            fragment_entity_refs = _merge_workiq_entity_refs(
                _extract_work_item_refs(fragment_text),
                configured_thread_entity_refs,
            )
            if not fragment_entity_refs and len(record_entity_refs) == 1:
                fragment_entity_refs = record_entity_refs
            fragment_workstream_id = _resolve_workiq_signal_workstream_id(
                entity_refs=fragment_entity_refs,
                items_by_id=items_by_id,
                workstreams=workstreams,
            ) or workstream_id
            fragment_message_id = _workiq_fragment_message_id(
                message_id=message_id,
                segment_index=segment_index,
                segment_count=len(fragments),
            )
            metadata: dict[str, Any] = {
                "source_type": source_type,
                "message_id": fragment_message_id,
                "parent_message_id": message_id,
                "segment_index": segment_index,
                "segment_count": len(fragments),
                "sender_alias": sender_alias,
                "participant_aliases": ",".join(participant_aliases) if participant_aliases else None,
                "thread_id": thread_id,
                "entity_link_confidence": ("high" if fragment_entity_refs else "low"),
                "query_name": query_name,
                "queried_workstream_id": default_workstream_id,
                "routed_workstream_id": routed_workstream_id,
                "record_subject": subject,
            }
            if source_type == "transcript" and timestamp.date() < as_of.date():
                metadata["backfill"] = True
            text = (
                _build_workiq_signal_text(source_type=source_type, subject=subject, preview=preview)
                if len(fragments) == 1
                else _build_workiq_fragment_signal_text(source_type=source_type, fragment_text=fragment_text)
            )
            signals.append(
                Signal(
                    id=str(
                        uuid5(
                            NAMESPACE_URL,
                            _workiq_signal_identity(
                                program_id=program_id,
                                source=source,
                                message_id=fragment_message_id,
                                timestamp=timestamp,
                                workstream_id=fragment_workstream_id,
                            ),
                        )
                    ),
                    timestamp=timestamp,
                    source=source,
                    program_id=program_id,
                    workstream_id=fragment_workstream_id,
                    entity_refs=merge_entity_refs(
                        provider_refs=fragment_entity_refs,
                        workstream_id=fragment_workstream_id,
                    ),
                    text=text,
                    raw_ref=f"workiq:{source_type}:{fragment_message_id}",
                    confidence=Confidence.MEDIUM,
                    metadata=metadata,
                    thread_id=thread_id,
                )
            )
    return tuple(signals)


def _workiq_signal_identity(
    *,
    program_id: str,
    source: str,
    message_id: str,
    timestamp: datetime,
    workstream_id: str | None,
) -> str:
    if workstream_id is None:
        return f"{program_id}|{source}|{message_id}|{timestamp.isoformat()}"
    return f"{program_id}|{source}|{workstream_id}|{message_id}|{timestamp.isoformat()}"


def _signal_participant_aliases(signal: Signal) -> tuple[str, ...]:
    metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
    raw_aliases = metadata.get("participant_aliases")
    if isinstance(raw_aliases, str) and raw_aliases.strip():
        parsed_aliases = tuple(
            alias.strip()
            for alias in raw_aliases.split(",")
            if alias.strip()
        )
        if parsed_aliases:
            return parsed_aliases

    sender_alias = metadata.get("sender_alias") if isinstance(metadata.get("sender_alias"), str) else None
    if isinstance(sender_alias, str) and sender_alias.strip():
        return (sender_alias.strip(),)
    return ()


def _workiq_record_matches_exclusions(value: str, *, exclude_keywords: tuple[str, ...]) -> bool:
    if not value.strip() or not exclude_keywords:
        return False
    normalized = value.lower()
    return any(keyword.strip().lower() in normalized for keyword in exclude_keywords if keyword.strip())


def _dedupe_workiq_signals(signals: tuple[Signal, ...]) -> tuple[Signal, ...]:
    deduped: list[Signal] = []
    seen: set[tuple[str, str | None, str | None]] = set()
    for signal in signals:
        metadata = signal.metadata or {}
        raw_message_id = metadata.get("message_id") if isinstance(metadata, dict) else None
        message_id_for_key = str(raw_message_id) if isinstance(raw_message_id, str) else None
        key = (
            signal.source,
            message_id_for_key,
            signal.workstream_id,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(signal)
    return tuple(deduped)


def _resolve_workiq_signal_workstream_id(
    *,
    entity_refs: tuple[str, ...],
    items_by_id: dict[int, WorkItem],
    workstreams: tuple[Workstream, ...],
) -> str | None:
    if not entity_refs or not items_by_id:
        return None

    resolved_workstream_ids: set[str] = set()
    for entity_ref in entity_refs:
        if not entity_ref.startswith("WI:"):
            continue
        try:
            work_item_id = int(entity_ref.split(":", 1)[1])
        except ValueError:
            continue
        item = items_by_id.get(work_item_id)
        if item is None:
            continue
        workstream_id = _resolve_workstream_id(item.area_path, workstreams)
        if workstream_id is not None:
            resolved_workstream_ids.add(workstream_id)

    if len(resolved_workstream_ids) == 1:
        return next(iter(resolved_workstream_ids))
    return None


def _merge_workiq_entity_refs(*groups: tuple[str, ...]) -> tuple[str, ...]:
    merged: list[str] = []
    for group in groups:
        for entity_ref in group:
            if entity_ref not in merged:
                merged.append(entity_ref)
    return tuple(merged)


def _is_echo_chamber_revision(revision: Revision, vertex_identities: set[str]) -> bool:
    return _record_is_echo_chamber_revision(revision, vertex_identities)


def _is_echo_chamber_comment(comment: Comment, vertex_identities: set[str]) -> bool:
    return _record_is_echo_chamber_comment(comment, vertex_identities)
