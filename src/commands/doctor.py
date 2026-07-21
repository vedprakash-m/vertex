from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from datetime import datetime, timezone
from importlib.util import find_spec
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable

import typer
import yaml

from src.commands.metric import _build_metric_rollout_status, _build_program_metric_binding_probe, validate_metric_bindings_command
from src.commands import watch as watch_command
from src.commands.doctor_checks.output import build_doctor_payload, doctor_tip, render_doctor_output
from src.commands.doctor_checks.chart_checks import run_charts_doctor as _run_charts_doctor
from src.commands.doctor_checks.nudge_checks import run_nudge_doctor as _run_nudge_doctor
from src.commands.doctor_checks.sharepoint_checks import run_sharepoint_doctor as _run_sharepoint_doctor
from src.commands.doctor_checks.assumption_checks import run_assumption_doctor as _run_assumption_doctor_impl
from src.commands.doctor_checks.auth_checks import run_auth_doctor as _run_auth_doctor_impl
from src.commands.doctor_checks.action_checks import run_action_doctor as _run_action_doctor_impl
from src.commands.doctor_checks.cadence_checks import describe_cadence_status
from src.commands.doctor_checks.cadence_checks import run_cadence_doctor as _run_cadence_doctor_impl
from src.commands.doctor_checks.capability_checks import run_capability_review_check as _run_capability_review_check_impl
from src.commands.doctor_checks.catchup_log_checks import run_catchup_log_doctor as _run_catchup_log_doctor_impl
from src.commands.doctor_checks.channel_composition import run_channel_doctor as _run_channel_doctor_impl
from src.commands.doctor_checks.channel_quality_checks import conversion_fidelity_check as _conversion_fidelity_check
from src.commands.doctor_checks.channel_quality_checks import eta_credibility_check as _eta_credibility_check
from src.commands.doctor_checks.channel_runtime_support_checks import channel_auth_failure_detail as _channel_auth_failure_detail
from src.commands.doctor_checks.channel_runtime_support_checks import current_doctor_kusto_targets as _current_doctor_kusto_targets
from src.commands.doctor_checks.channel_source_health_checks import slice_source_health_check as _slice_source_health_check
from src.commands.doctor_checks.channel_surface_checks import ado_pr_coverage_check as _ado_pr_coverage_check_impl
from src.commands.doctor_checks.channel_surface_checks import channel_detail_check as _channel_detail_check_impl
from src.commands.doctor_checks.channel_surface_checks import extract_kusto_failed_target as _extract_kusto_failed_target_impl
from src.commands.doctor_checks.channel_surface_checks import channel_last_error as _channel_last_error_impl
from src.commands.doctor_checks.channel_surface_checks import uil_registry_check as _uil_registry_check_impl
from src.commands.doctor_checks.channel_surface_checks import uil_registry_checks as _uil_registry_checks_impl
from src.commands.doctor_checks.channel_delta_checks import channel_delta_check as _channel_delta_check_impl
from src.commands.doctor_checks.channel_delta_checks import channel_health_snapshot as _channel_health_snapshot_impl
from src.commands.doctor_checks.circuit_breaker_checks import default_breaker_snapshot
from src.commands.doctor_checks.circuit_breaker_checks import describe_circuit_breaker_snapshot
from src.commands.doctor_checks.circuit_breaker_checks import display_path
from src.commands.doctor_checks.circuit_breaker_checks import run_circuit_breaker_doctor as _run_circuit_breaker_doctor_impl
from src.commands.doctor_checks.consistency_checks import consistency_check as _consistency_check_impl
from src.commands.doctor_checks.context_checks import run_context_doctor as _run_context_doctor_impl
from src.commands.doctor_checks.context_checks import run_ranked_gaps as _run_ranked_gaps_impl
from src.commands.doctor_checks.default_report_composition import run_default_doctor_report as _run_default_doctor_report
from src.commands.doctor_checks.default_report_support_checks import audit_hygiene_check as _audit_hygiene_check
from src.commands.doctor_checks.default_report_support_checks import candidate_queue_backlog_check as _candidate_queue_backlog_check
from src.commands.doctor_checks.default_report_support_checks import claim_freshness_check as _claim_freshness_check
from src.commands.doctor_checks.default_report_support_checks import coverage_range_check as _coverage_range_check
from src.commands.doctor_checks.default_report_support_checks import degraded_confirm_check as _degraded_confirm_check
from src.commands.doctor_checks.default_report_support_checks import external_dependencies_check as _external_dependencies_check
from src.commands.doctor_checks.default_report_support_checks import ledger_health_check as _ledger_health_check
from src.commands.doctor_checks.default_report_support_checks import latest_gather_integration_check as _latest_gather_integration_check
from src.commands.doctor_checks.default_report_support_checks import recurring_gate_failures_check as _recurring_gate_failures_check
from src.commands.doctor_checks.default_report_support_checks import slice_telemetry_runtime_check as _slice_telemetry_runtime_check
from src.commands.doctor_checks.default_report_support_checks import override_streak_check as _override_streak_check
from src.commands.doctor_checks.doctor_support_checks import agency_cli_check as _agency_cli_check_impl
from src.commands.doctor_checks.doctor_support_checks import load_milestone_owner_aliases
from src.commands.doctor_checks.doctor_support_checks import latest_snapshot_check as _latest_snapshot_check_impl
from src.commands.doctor_checks.doctor_support_checks import mail_preview_check as _mail_preview_check_impl
from src.commands.doctor_checks.doctor_support_checks import probe_ado_access as _probe_ado_access_impl
from src.commands.doctor_checks.doctor_support_checks import seed_overrides as _seed_overrides_impl
from src.commands.doctor_checks.doctor_support_checks import template_check as _template_check_impl
from src.commands.doctor_checks.doctor_support_checks import template_contract_edition_check as _template_contract_edition_check_impl
from src.commands.doctor_checks.doctor_support_checks import token_check as _token_check_impl
from src.commands.doctor_checks.dependency_checks import count_legacy_dependencies
from src.commands.doctor_checks.dependency_checks import load_dependency_milestone_ids
from src.commands.doctor_checks.dependency_checks import run_dependency_doctor as _run_dependency_doctor_impl
from src.commands.doctor_checks.dependency_checks import validate_dependency_references
from src.commands.doctor_checks.decision_checks import run_decision_doctor as _run_decision_doctor_impl
from src.commands.doctor_checks.escalation_checks import run_escalation_doctor as _run_escalation_doctor_impl
from src.commands.doctor_checks.governance_checks import run_config_governance_check as _run_config_governance_check_impl
from src.commands.doctor_checks.hygiene_checks import run_hygiene_nudge_check as _run_hygiene_nudge_check_impl
from src.commands.doctor_checks.id_checks import run_id_doctor as _run_id_doctor_impl
from src.commands.doctor_checks.id_checks import load_dependency_workstream_ids as _load_dependency_workstream_ids_impl
from src.commands.doctor_checks.id_checks import load_scorecard_dimension_bindings as _load_scorecard_dimension_bindings
from src.commands.doctor_checks.kusto_checks import build_live_kusto_probe as _build_live_kusto_probe_impl
from src.commands.doctor_checks.kusto_checks import icm_kusto_check as _icm_kusto_check_impl
from src.commands.doctor_checks.kusto_checks import kusto_target_labels as _kusto_target_labels_impl
from src.commands.doctor_checks.kusto_checks import kusto_validation_check as _kusto_validation_check_impl
from src.commands.doctor_checks.kusto_checks import load_doctor_kusto_queries as _load_doctor_kusto_queries_impl
from src.commands.doctor_checks.kusto_checks import run_kusto_doctor as _run_kusto_doctor_impl
from src.commands.doctor_checks.kusto_checks import summarize_kusto_targets as _summarize_kusto_targets_impl
from src.commands.doctor_checks.kusto_checks import validate_kusto_query_definitions as _validate_kusto_query_definitions_impl
from src.commands.doctor_checks.kb_checks import run_kb_doctor as _run_kb_doctor_impl
from src.commands.doctor_checks.milestone_health_checks import build_milestone_health_warning as _build_milestone_health_warning_impl
from src.commands.doctor_checks.milestone_health_checks import load_latest_confirmed_snapshot_items as _load_latest_confirmed_snapshot_items_impl
from src.commands.doctor_checks.milestone_health_checks import snapshot_to_work_items as _snapshot_to_work_items_impl
from src.commands.doctor_checks.milestone_checks import load_current_milestones
from src.commands.doctor_checks.milestone_checks import run_milestone_doctor as _run_milestone_doctor_impl
from src.commands.doctor_checks.metric_binding_checks import run_metric_binding_doctor as _run_metric_binding_doctor_impl
from src.commands.doctor_checks.m365_review_checks import build_m365_registry_review_metadata as _build_m365_registry_review_metadata_impl
from src.commands.doctor_checks.m365_review_checks import artifact_is_missing_m365_id as _artifact_is_missing_m365_id_impl
from src.commands.doctor_checks.m365_review_checks import m365_discovery_check as _m365_discovery_check_impl
from src.commands.doctor_checks.m365_review_checks import m365_registry_promotion_check as _m365_registry_promotion_check_impl
from src.commands.doctor_checks.m365_review_checks import m365_registry_review_check as _m365_registry_review_check_impl
from src.commands.doctor_checks.m365_review_checks import summarize_m365_discovery as _summarize_m365_discovery_impl
from src.commands.doctor_checks.m365_review_checks import summarize_m365_discovery_comparison as _summarize_m365_discovery_comparison_impl
from src.commands.doctor_checks.m365_review_checks import summarize_m365_registry_review as _summarize_m365_registry_review_impl
from src.commands.doctor_checks.models import ADOProbeResult, DoctorCheck, DoctorReport, directory_size, format_bytes
from src.commands.doctor_checks.operator_gate_composition import run_operator_gates_doctor as _run_operator_gates_doctor_impl
from src.commands.doctor_checks.operator_gate_followup_checks import operator_gate_checkpoint_creation_check as _operator_gate_checkpoint_creation_check_impl
from src.commands.doctor_checks.operator_gate_followup_checks import operator_gate_kusto_validation_check as _operator_gate_kusto_validation_check_impl
from src.commands.doctor_checks.operator_gate_followup_checks import operator_gate_rollback_drill_check as _operator_gate_rollback_drill_check_impl
from src.commands.doctor_checks.operator_gate_followup_checks import operator_gate_transcript_health_check as _operator_gate_transcript_health_check_impl
from src.commands.doctor_checks.operator_gate_m365_checks import build_missing_id_action_categories as _build_missing_id_action_categories_impl
from src.commands.doctor_checks.operator_gate_m365_checks import build_missing_id_diagnostics as _build_missing_id_diagnostics_impl
from src.commands.doctor_checks.operator_gate_m365_checks import operator_gate_m365_ids_check as _operator_gate_m365_ids_check_impl
from src.commands.doctor_checks.operator_gate_m365_checks import source_channel_for_ref_kind as _source_channel_for_ref_kind_impl
from src.commands.doctor_checks.operator_gate_m365_checks import source_ref_kind_for_artifact_type as _source_ref_kind_for_artifact_type_impl
from src.commands.doctor_checks.operator_gate_m365_checks import summarize_missing_id_diagnostics as _summarize_missing_id_diagnostics_impl
from src.commands.doctor_checks.platform_readiness_checks import run_platform_readiness_doctor as _run_platform_readiness_doctor_impl
from src.commands.doctor_checks.privacy_checks import run_privacy_doctor as _run_privacy_doctor_impl
from src.commands.doctor_checks.readiness_checks import readiness_gate_settings
from src.commands.doctor_checks.readiness_checks import run_readiness_doctor as _run_readiness_doctor_impl
from src.commands.doctor_checks.risk_checks import run_risk_doctor as _run_risk_doctor_impl
from src.commands.doctor_checks.semantic_index_checks import run_semantic_index_doctor as _run_semantic_index_doctor_impl
from src.commands.doctor_checks.semantic_index_checks import build_semantic_index_checks as _build_semantic_index_checks
from src.commands.doctor_checks.semantic_index_checks import semantic_index_enabled as _semantic_index_enabled
from src.commands.doctor_checks.watch_source_checks import run_watch_source_doctor as _run_watch_source_doctor_impl
from src.commands.doctor_checks.checkpoint_checks import run_checkpoint_doctor as _run_checkpoint_doctor
from src.commands.doctor_checks.persona_checks import run_persona_doctor as _run_persona_doctor
from src.commands.doctor_checks.adapter_cert_checks import run_adapter_cert_doctor as _run_adapter_cert_doctor
from src.commands.doctor_checks.refactor_status import build_refactor_status_report, render_refactor_status_output
from src.commands.doctor_checks.confirm_readiness_checks import run_confirm_readiness_doctor as _run_confirm_readiness_doctor
from src.commands.doctor_checks.fact_store_flip_checks import run_fact_parity_doctor as _run_fact_parity_doctor
from src.commands.doctor_checks.fact_store_flip_checks import run_bridge_disabled_doctor as _run_bridge_disabled_doctor
from src.commands.doctor_checks.fact_store_flip_checks import run_bridge_failure_backlog_doctor as _run_bridge_failure_backlog_doctor
from src.commands.doctor_checks.fact_store_flip_checks import run_fact_deserialization_doctor as _run_fact_deserialization_doctor
from src.commands.doctor_checks.fact_store_flip_checks import run_flip_parity_doctor as _run_flip_parity_doctor
from src.commands.doctor_checks.fact_store_flip_checks import run_flip_status_doctor as _run_flip_status_doctor
from src.commands.doctor_checks.storage_checks import (
    run_storage_doctor as _run_storage_doctor,
    _dc02_runtime_layout_check,
)
from src.commands.doctor_checks.source_waiver_checks import run_source_waiver_doctor as _run_source_waiver_doctor
from src.commands.doctor_checks.schedule_health_checks import run_schedule_health_doctor as _run_schedule_health_doctor
from src.core.ado_client import ADOClient, ADO_RESOURCE
from src.core.action_tracker import assess_action_staleness, get_actions_path
from src.core.assumption_tracker import check_validation_due, get_assumptions_path
from src.core.archive_store import read_archive_index
from src.core.alerts import surface_alert_banner as _surface_alert_banner
from src.core.ban_list_validator import DEFAULT_BANNED_PHRASES
from src.core.capability_status import latest_program_capability_reviewed_on, load_program_capability_status
from src.core.claim_tracker import load_decision_asks
from src.core.config_loader import EDITIONS_ROOT, PROGRAMS_ROOT, REPORTS_ROOT, discover_report_editions, load_bundle
from src.core.decision_register import assess_decision_review_staleness, assess_proposed_decision_staleness, get_decisions_path
from src.core.dependency_graph import build_dependency_dag, get_dependencies_path
from src.core.edition_resolver import ResolvedEdition, resolve_edition, resolve_edition_paths as _resolve_edition_paths
from src.core.exceptions import AuthError, QueryError, QueryTimeoutError, StateError as _StateError
from src.core.gather_state_store import load_gather_state
from src.core.knowledge_store import (
    KnowledgeStore,
    get_shared_knowledge_root,
    load_program_knowledge,
)
from src.core.kusto_client import build_live_kusto_query_probe
from src.core.metric_binding_validator import (
    MetricBindingProbe,
    build_live_metric_binding_probe,
    build_metric_validation_kql,
    compute_metric_binding_validation_hash,
    compute_metric_validation_kql_hash,
    validate_metric_source_binding,
)
from src.core.metric_models import MetricDefinition
from src.core.metric_registry import METRICS_ROOT, load_metric_definition_map
from src.core.keyword_topic_router import suggest_keyword_expansions
from src.core.m365_registry_store import M365RegistryArtifact, describe_current_m365_registry_promotion_blockers, is_current_m365_registry_promotion_candidate, load_m365_registry, read_m365_routing_feedback_events
from src.core.milestone_engine import (
    assess_milestone_health,
    describe_milestone_schedule_variance,
    load_milestone_completion_date_history_map,
    load_milestone_target_date_history_map,
    summarize_milestone_completion_date_history,
    summarize_milestone_target_date_history,
)
from src.core.models import WorkItem
from src.core.models_v2 import KustoQuery, Milestone, MilestoneStatus, Signal
from src.core.m365_signal_corpus import build_m365_corpus_texts_by_workstream, load_approved_m365_corpus_signals
from src.core.program_fact_store import load_current_workstreams, load_program_facts, project_action_items, project_assumptions, project_decision_entries, project_dependencies, project_milestones, project_risk_entries
from src.core.privacy_scan import find_plaintext_sensitive_profile_files, scan_program_journal_for_credentials
from src.core.risk_register_engine import assess_risk_staleness, get_risk_register_path
from src.core.overrides_store import load_overrides, merge_overrides, save_overrides
from src.core.persona_checker import CHECK_TYPE_REGISTRY
from src.core.persona_models import PersonaCheck, PersonaDefinition
from src.core.policy_evaluator import get_escalation_rules_path, load_escalation_rules
from src.core.quality_gates import evaluate_context_integrity_gates
from src.core.quality_matrix_engine import validate_slice_contracts
from src.core.query_builder import build_odata_filter
from src.core.reality_store import RealityStore
from src.core.review_status_store import load_review_status
from src.core.signal_review import signal_is_approved_for_evidence
from src.core.snapshot_store import ARCHIVE_ROOT, get_archive_root, read_snapshot
from src.core.source_candidate_store import SourceCandidateStore
from src.core.discovery_intent import SourceCandidateStatus, SourceIntentStatus, SourceRefKind
from src.core.source_health import (
    source_health_function_name_for_edition,
)
from src.core.source_waiver_store import load_source_waivers
from src.core.store_factory import build_signal_store_for_program_id, build_trajectory_store_for_program_id, read_signal_review_log_for_program_id
from src.core.trajectory import read_trajectory
from src.core.journal import get_week_key
from src.m365.agency_bridge import AgencyBridge, AgencyCapabilities
from src.m365.discovery_diagnostics import classify_missing_id_discovery_status


REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_ROOT = REPO_ROOT / "templates"
_METRIC_BINDING_REVALIDATION_DAYS = 30


def doctor_command(
    edition: str | None = typer.Option(None, "--edition", help="Edition name. Uses the only configured edition when omitted."),
    fix: bool = typer.Option(False, "--fix", help="Auto-create missing overrides.yaml when possible."),
    check_auth: bool = typer.Option(False, "--check-auth", help="Validate ADO auth reachability/token age, Graph send prerequisites, and Agency CLI availability."),
    operator_gates: bool = typer.Option(False, "--operator-gates", help="Summarize the remaining PM/operator gates with live evidence, next commands, and explicit operator-vs-LLM responsibilities."),
    platform_readiness: bool = typer.Option(False, "--platform-readiness", help="Measure fleet-scoped P4/P5/V-11 readiness from provable repo signals and mark unrecorded proof criteria as UNPROVEN."),
    kb: bool = typer.Option(False, "--kb", help="Validate knowledge, program, and edition referential integrity."),
    kb_check_origins: bool = typer.Option(False, "--kb-check-origins", help="With --kb, compare current origin files against stored knowledge vault hashes to detect stale ingested copies."),
    context: bool = typer.Option(False, "--context", help="Validate cross-file program context invariants (§5) and staleness policy (§8)."),
    ids: bool = typer.Option(False, "--ids", help="Validate scorecard, chapter, slice, registry, and workstream ID consistency."),
    cadence: bool = typer.Option(False, "--cadence", help="Validate communication-plan cadence against recent confirmation history."),
    channels: bool = typer.Option(False, "--channels", help="Inspect gather channel completeness, active flags, and transcript coverage telemetry."),
    privacy: bool = typer.Option(False, "--privacy", help="Scan journal files for credential patterns and verify people_profiles.yaml encryption state."),
    kusto: bool = typer.Option(False, "--kusto", help="Validate applicable Kusto query definitions and probe live reachability."),
    milestones: bool = typer.Option(False, "--milestones", help="Validate milestones.yaml schema, workstream links, and owner aliases."),
    dependencies: bool = typer.Option(False, "--dependencies", help="Validate dependencies.yaml schema, references, cycles, and legacy fallback state."),
    actions: bool = typer.Option(False, "--actions", help="Validate actions.jsonl schema, references, and overdue actions."),
    risks: bool = typer.Option(False, "--risks", help="Validate risk_register.yaml schema, references, and stale review dates."),
    escalations: bool = typer.Option(False, "--escalations", help="Validate escalation_rules.yaml schema and escalation_state.json cooldown state."),
    decisions: bool = typer.Option(False, "--decisions", help="Validate decisions.yaml schema, references, and stale proposed decisions."),
    assumptions: bool = typer.Option(False, "--assumptions", help="Validate assumptions.yaml schema, references, and overdue validation dates."),
    readiness: bool = typer.Option(False, "--readiness", help="Validate readiness.yaml presence and readiness_snapshot.yaml freshness/integrity."),
    semantic_index: bool = typer.Option(False, "--semantic-index", help="Validate semantic index freshness, dirty state, and optimization health."),
    personas: bool = typer.Option(False, "--personas", help="Validate personas.yaml schema, check hygiene, minimum density, staleness, and re2 availability."),
    metric_bindings: bool = typer.Option(False, "--metric-bindings", help="Validate L1 metric-binding readiness, revalidate stale bindings, and flag validation drift."),
    consistency: bool = typer.Option(False, "--consistency", help="Validate trusted baseline, confirmed archive, and review-state issue alignment."),
    checkpoints: bool = typer.Option(False, "--checkpoints", help="Validate checkpoint inventory and whether the latest checkpoint covers the mutable program stores needed for rollback."),
    storage: bool = typer.Option(False, "--storage", help="Validate journal retention posture, trajectory footprint, and SQLite storage health."),
    flip_status: bool = typer.Option(False, "--flip-status", help="Report the current Fact Store source-of-record posture for the resolved edition (legacy, dual, or fact-store)."),
    flip_parity: bool = typer.Option(False, "--flip-parity", help="Compare legacy mutable-state projections against Fact Store projections for one confirmed issue."),
    fact_parity: bool = typer.Option(False, "--fact-parity", help="Check whether enough dual-read parity cycles have been logged for the resolved program (reads fact_store.dual_read_cycles from platform_state.yaml, default 5)."),
    fact_bridge: bool = typer.Option(False, "--fact-bridge", help="Check the ledger->fact-store bridge posture: whether it is enabled for a REV-configured program, and whether a persistent bridge-failure backlog exists (fix-data-flow.md Track A / PS-2)."),
    fact_deserialization: bool = typer.Option(False, "--fact-deserialization", help="Confirm existing persisted facts still deserialize against the current schema, not just newly-bridged ones (fix-data-flow.md Track L)."),
    confirm_readiness: bool = typer.Option(False, "--confirm-readiness", help="Enumerate exact live blockers that would prevent a non-forced confirm. Returns 0 only when confirm would succeed."),
    adapter_cert: bool = typer.Option(False, "--adapter-cert", help="Audit UIL adapter certification per WS-3: checks which channels are enabled/certified and probes WorkIQ verb availability."),
    issue: int | None = typer.Option(None, "--issue", help="Issue number required by --flip-parity."),
    charts: bool = typer.Option(False, "--charts", help="Validate chart cache TTL vs edition cadence, attachment targets, exec-summary uniqueness, and renderer IDs."),
    source_waivers: bool = typer.Option(False, "--source-waivers", help="Audit programs/<id>/source_waivers.yaml against vertex/policies/source_waivers.schema.yaml (D-32)."),
    schedule_health: bool = typer.Option(False, "--schedule-health", help="Check whether scheduled prefetch/cockpit-build artifacts are present and fresh (ADF-W5.10)."),
    watch_sources: bool = typer.Option(False, "--watch-sources", help="Validate selected vertex watch signal sources without starting the polling loop."),
    source: list[str] = typer.Option([], "--source", help="Watch signal source to validate with --watch-sources. Repeat or use comma-separated values: ado, workiq, kusto, analytics, sprints, icm."),
    catchup_log: bool = typer.Option(False, "--catchup-log", help="Show recent catchup failures or truncation events from _feedback/usage_log.jsonl."),
    nudge: bool = typer.Option(False, "--nudge", help="Run all NQ-1 through NQ-9 nudge health checks for the resolved program."),
    circuit_breakers: bool = typer.Option(False, "--circuit-breakers", help="Show current persisted circuit breaker state and optionally reset it."),
    reset_circuit_breakers: bool = typer.Option(False, "--reset-circuit-breakers", help="Reset persisted circuit breaker state to CLOSED. Requires --circuit-breakers."),
    ranked: bool = typer.Option(False, "--ranked", help="Show ranked context gaps from _feedback/context_gaps.jsonl (§21.3). Requires --context."),
    fix_hints: bool = typer.Option(False, "--fix-hints", help="Show per-item remediation guidance for each violation. Requires --context."),
    refactor_status: bool = typer.Option(False, "--refactor-status", help="Show Phase 0 debt-remediation progress metrics."),
    sharepoint: bool = typer.Option(False, "--sharepoint", help="Validate SharePoint/LT deck integration health (QG-SP-1 through QG-SP-8)."),
    strict_lt_alignment: bool = typer.Option(False, "--strict-lt-alignment", help="With --sharepoint, treat lt_deck_alignment divergence as a warning (QG-SP-5)."),
    rev_health: bool = typer.Option(False, "--rev-health", help="Summarize Program-Context Intelligence (REV) subsystem health: run-state + verification distributions, evidence-vault retention, and Prompt-Shields mode."),
    rev_program: str | None = typer.Option(None, "--rev-program", help="Program ID for --rev-health (defaults to the resolved edition's program)."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    default_reports_root = REPO_ROOT / "reports"
    default_programs_root = REPO_ROOT / "programs"
    effective_programs_root: Path | None = PROGRAMS_ROOT
    if REPORTS_ROOT != default_reports_root and PROGRAMS_ROOT == default_programs_root:
        effective_programs_root = None

    if rev_health:
        _run_rev_health(
            edition=edition,
            rev_program=rev_program,
            programs_root=PROGRAMS_ROOT,
            editions_root=EDITIONS_ROOT,
            format=format,
        )
        return

    if refactor_status:
        refactor_report = build_refactor_status_report(repo_root=REPO_ROOT)
        typer.echo(
            render_refactor_status_output(
                refactor_report,
                format=format,
            ),
            nl=False,
        )
        raise typer.Exit(code=1 if refactor_report.failures else 0)

    # Handle --ranked inline (requires --context)
    if ranked:
        if not context:
            raise typer.BadParameter("--ranked requires --context.")
        _run_ranked_gaps(
            edition_name=edition,
            programs_root=PROGRAMS_ROOT,
            editions_root=EDITIONS_ROOT,
        )
        return

    if fix_hints and not context:
        raise typer.BadParameter("--fix-hints requires --context.")
    if issue is not None and not flip_parity:
        raise typer.BadParameter("--issue requires --flip-parity.")

    try:
        _ed_paths = _resolve_edition_paths(edition or "", editions_root=EDITIONS_ROOT, programs_root=PROGRAMS_ROOT)
        if _ed_paths is None and edition is None:
            _discovered = discover_report_editions(reports_root=REPORTS_ROOT)
            if len(_discovered) == 1:
                _ed_paths = _resolve_edition_paths(_discovered[0], editions_root=EDITIONS_ROOT, programs_root=PROGRAMS_ROOT)
        if _ed_paths is not None:
            _doctor_banner = _surface_alert_banner(_ed_paths.program_id, programs_root=PROGRAMS_ROOT)
            if _doctor_banner is not None:
                typer.echo(_doctor_banner, err=True)
    except (OSError, _StateError, ValueError):
        pass

    report = run_doctor(
        edition_name=edition,
        fix=fix,
        check_auth=check_auth,
        operator_gates=operator_gates,
        platform_readiness=platform_readiness,
        kb=kb,
        kb_check_origins=kb_check_origins,
        context=context,
        ids=ids,
        cadence=cadence,
        channels=channels,
        privacy=privacy,
        kusto=kusto,
        milestones=milestones,
        dependencies=dependencies,
        actions=actions,
        risks=risks,
        escalations=escalations,
        decisions=decisions,
        assumptions=assumptions,
        readiness=readiness,
        semantic_index=semantic_index,
        personas=personas,
        metric_bindings=metric_bindings,
        consistency=consistency,
        checkpoints=checkpoints,
        storage=storage,
        flip_status=flip_status,
        flip_parity=flip_parity,
        fact_parity=fact_parity,
        fact_bridge=fact_bridge,
        fact_deserialization=fact_deserialization,
        confirm_readiness=confirm_readiness,
        adapter_cert=adapter_cert,
        issue_number=issue,
        charts=charts,
        source_waivers=source_waivers,
        watch_sources=watch_sources,
        watch_source_values=tuple(source),
        catchup_log=catchup_log,
        nudge=nudge,
        circuit_breakers=circuit_breakers,
        reset_circuit_breakers=reset_circuit_breakers,
        fix_hints=fix_hints,
        sharepoint=sharepoint,
        strict_lt_alignment=strict_lt_alignment,
        schedule_health=schedule_health,
        reports_root=REPORTS_ROOT,
        archive_root=ARCHIVE_ROOT,
        editions_root=EDITIONS_ROOT,
        programs_root=effective_programs_root,
    )

    tip = _doctor_tip(
        check_auth=check_auth,
        operator_gates=operator_gates,
        platform_readiness=platform_readiness,
        kb=kb,
        ids=ids,
        cadence=cadence,
        channels=channels,
        privacy=privacy,
        kusto=kusto,
        milestones=milestones,
        dependencies=dependencies,
        actions=actions,
        risks=risks,
        escalations=escalations,
        decisions=decisions,
        assumptions=assumptions,
        readiness=readiness,
        semantic_index=semantic_index,
        metric_bindings=metric_bindings,
        consistency=consistency,
        checkpoints=checkpoints,
        storage=storage,
        flip_status=flip_status,
        flip_parity=flip_parity,
        fact_parity=fact_parity,
        confirm_readiness=confirm_readiness,
        adapter_cert=adapter_cert,
        charts=charts,
        source_waivers=source_waivers,
        watch_sources=watch_sources,
        catchup_log=catchup_log,
        nudge=nudge,
        circuit_breakers=circuit_breakers,
        context=context,
        schedule_health=schedule_health,
    ) if report.failures == 0 else None

    if format == "human":
        typer.echo("VERTEX DOCTOR - System Health Check")
        typer.echo("===================================")
        for check in report.checks:
            icon = {"ok": "OK", "warn": "WARN", "fail": "FAIL", "info": "INFO"}[check.status]
            typer.echo(f"{icon:4} {check.label:<11} {check.detail}")
        suffix = f" ({report.warnings} warning{'s' if report.warnings != 1 else ''})" if report.warnings else ""
        typer.echo(f"\nOverall: {report.overall}{suffix}")
        if tip is not None:
            typer.echo(tip)
    else:
        typer.echo(
            render_doctor_output(
                _build_doctor_payload(
                    report=report,
                    tip=tip,
                ),
                format=format,
            ),
            nl=False,
        )
    raise typer.Exit(code=1 if report.failures else 0)


# Pure presentation helpers live in doctor_checks/output.py (Phase 3 doctor
# decomposition). Re-exported here under their historical private names so the
# command body and existing test imports keep working.
_doctor_tip = doctor_tip
_build_doctor_payload = build_doctor_payload


def run_doctor(
    edition_name: str | None = None,
    fix: bool = False,
    check_auth: bool = False,
    operator_gates: bool = False,
    platform_readiness: bool = False,
    kb: bool = False,
    kb_check_origins: bool = False,
    context: bool = False,
    ids: bool = False,
    cadence: bool = False,
    channels: bool = False,
    privacy: bool = False,
    kusto: bool = False,
    milestones: bool = False,
    dependencies: bool = False,
    actions: bool = False,
    risks: bool = False,
    escalations: bool = False,
    decisions: bool = False,
    assumptions: bool = False,
    readiness: bool = False,
    semantic_index: bool = False,
    personas: bool = False,
    metric_bindings: bool = False,
    consistency: bool = False,
    checkpoints: bool = False,
    storage: bool = False,
    flip_status: bool = False,
    flip_parity: bool = False,
    fact_parity: bool = False,
    fact_bridge: bool = False,
    fact_deserialization: bool = False,
    confirm_readiness: bool = False,
    adapter_cert: bool = False,
    issue_number: int | None = None,
    charts: bool = False,
    source_waivers: bool = False,
    watch_sources: bool = False,
    watch_source_values: tuple[str, ...] = (),
    catchup_log: bool = False,
    nudge: bool = False,
    circuit_breakers: bool = False,
    reset_circuit_breakers: bool = False,
    fix_hints: bool = False,
    sharepoint: bool = False,
    strict_lt_alignment: bool = False,
    schedule_health: bool = False,
    reports_root: Path | None = None,
    archive_root: Path | None = None,
    templates_root: Path | None = None,
    editions_root: Path | None = None,
    programs_root: Path | None = None,
    output_root: Path | None = None,
    ado_probe: Callable[..., ADOProbeResult] | None = None,
    kusto_probe: Callable[[KustoQuery], None] | None = None,
    metric_binding_probe: MetricBindingProbe | None = None,
    metric_definitions: dict[str, MetricDefinition] | None = None,
    reality_db_root: Path | None = None,
    now: datetime | None = None,
) -> DoctorReport:
    resolved_editions_root = editions_root or EDITIONS_ROOT
    resolved_archive_root = archive_root or ARCHIVE_ROOT
    resolved_reports_root = reports_root or REPORTS_ROOT
    resolved_programs_root = programs_root or (resolved_reports_root.parent / "programs")
    resolved_reality_db_root = reality_db_root or (resolved_programs_root.parent / "vertex-db")
    if sum(1 for option in (check_auth, operator_gates, platform_readiness, kb, context, ids, cadence, channels, privacy, kusto, milestones, dependencies, actions, risks, escalations, decisions, assumptions, readiness, semantic_index, personas, metric_bindings, consistency, checkpoints, storage, flip_status, flip_parity, fact_parity, fact_bridge, fact_deserialization, confirm_readiness, adapter_cert, charts, source_waivers, watch_sources, catchup_log, nudge, circuit_breakers, sharepoint, schedule_health) if option) > 1:
        raise typer.BadParameter(
            "Choose only one of --check-auth, --operator-gates, --platform-readiness, --kb, --context, --ids, --cadence, --channels, --privacy, --kusto, --milestones, --dependencies, --actions, --risks, --escalations, --decisions, --assumptions, --readiness, --semantic-index, --personas, --metric-bindings, --consistency, --checkpoints, --storage, --flip-status, --flip-parity, --fact-parity, --fact-bridge, --fact-deserialization, --confirm-readiness, --adapter-cert, --charts, --source-waivers, --watch-sources, --catchup-log, --nudge, --circuit-breakers, --sharepoint, or --schedule-health."
        )
    if watch_source_values and not watch_sources:
        raise typer.BadParameter("--source requires --watch-sources.")
    if kb_check_origins and not kb:
        raise typer.BadParameter("--kb-check-origins requires --kb.")
    if reset_circuit_breakers and not circuit_breakers:
        raise typer.BadParameter("--reset-circuit-breakers requires --circuit-breakers.")
    if flip_parity and issue_number is None:
        raise typer.BadParameter("--flip-parity requires --issue.")
    if check_auth:
        resolved_edition = _resolve_edition_name(edition_name, resolved_reports_root)
        return _run_auth_doctor(
            edition_name=resolved_edition,
            reports_root=resolved_reports_root,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
            ado_probe=ado_probe,
            kusto_probe=kusto_probe,
        )
    if operator_gates:
        resolved_edition = _resolve_edition_name(edition_name, resolved_reports_root)
        return _run_operator_gates_doctor(
            edition_name=resolved_edition,
            reports_root=resolved_reports_root,
            archive_root=resolved_archive_root,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
            ado_probe=ado_probe,
            kusto_probe=kusto_probe,
            metric_binding_probe=metric_binding_probe,
            metric_definitions=metric_definitions,
            reality_db_root=resolved_reality_db_root,
            now=now,
        )
    if platform_readiness:
        return _run_platform_readiness_doctor(
            programs_root=resolved_programs_root,
            reports_root=resolved_reports_root,
            editions_root=resolved_editions_root,
        )
    if kb:
        return _run_kb_doctor(
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
            check_origins=kb_check_origins,
        )
    if context:
        resolved_edition = _resolve_edition_name(edition_name, resolved_reports_root)
        return _run_context_doctor(
            edition_name=resolved_edition,
            programs_root=resolved_programs_root,
            editions_root=resolved_editions_root,
            fix_hints=fix_hints,
        )
    # Source-waiver governance is fleet-scoped: it audits every program's
    # policy file and does not consume an edition.  Resolve it before the
    # edition-scoped doctor branches so a multi-edition workspace does not
    # force callers to supply an irrelevant `--edition` value.
    if source_waivers:
        return _run_source_waiver_doctor(
            programs_root=resolved_programs_root,
        )
    resolved_edition = _resolve_edition_name(edition_name, resolved_reports_root)
    if ids:
        return _run_id_doctor(
            edition_name=resolved_edition,
            reports_root=resolved_reports_root,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
        )
    if cadence:
        return _run_cadence_doctor(
            edition_name=resolved_edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
            archive_root=resolved_archive_root,
        )
    if channels:
        return _run_channel_doctor(
            edition_name=resolved_edition,
            reports_root=resolved_reports_root,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
        )
    if privacy:
        return _run_privacy_doctor(
            edition_name=resolved_edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
        )
    if kusto:
        return _run_kusto_doctor(
            edition_name=resolved_edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
            kusto_probe=kusto_probe,
        )
    if milestones:
        return _run_milestone_doctor(
            edition_name=resolved_edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
            archive_root=resolved_archive_root,
        )
    if dependencies:
        return _run_dependency_doctor(
            edition_name=resolved_edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
        )
    if actions:
        return _run_action_doctor(
            edition_name=resolved_edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
        )
    if risks:
        return _run_risk_doctor(
            edition_name=resolved_edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
        )
    if escalations:
        return _run_escalation_doctor(
            edition_name=resolved_edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
        )
    if decisions:
        return _run_decision_doctor(
            edition_name=resolved_edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
        )
    if assumptions:
        return _run_assumption_doctor(
            edition_name=resolved_edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
        )
    if readiness:
        return _run_readiness_doctor(
            edition_name=resolved_edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
        )
    if semantic_index:
        return _run_semantic_index_doctor(
            edition_name=resolved_edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
            archive_root=resolved_archive_root,
        )
    if personas:
        return _run_persona_doctor(
            edition_name=resolved_edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
            reports_root=resolved_reports_root,
        )
    if metric_bindings:
        return _run_metric_binding_doctor(
            edition_name=resolved_edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
            reality_db_root=resolved_reality_db_root,
            metric_binding_probe=metric_binding_probe,
            metric_definitions=metric_definitions,
            now=now,
        )
    if consistency:
        return DoctorReport(
            edition=resolved_edition,
            checks=(
                _consistency_check(
                    resolved_edition,
                    archive_root=resolved_archive_root,
                    reports_root=resolved_reports_root,
                    editions_root=resolved_editions_root,
                    programs_root=resolved_programs_root,
                ),
            ),
        )
    if checkpoints:
        return _run_checkpoint_doctor(
            edition_name=resolved_edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
            archive_root=resolved_archive_root,
        )
    if storage:
        return _run_storage_doctor(
            edition_name=resolved_edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
            archive_root=resolved_archive_root,
            reality_db_root=resolved_reality_db_root,
        )
    if flip_status:
        resolved = resolve_edition(
            resolved_edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
        )
        if resolved is None:
            raise typer.BadParameter(f"Unknown edition '{resolved_edition}'.")
        return _run_flip_status_doctor(
            edition_name=resolved_edition,
            program_id=resolved.program.id,
            programs_root=resolved_programs_root,
            reality_db_root=resolved_reality_db_root,
        )
    if flip_parity:
        resolved = resolve_edition(
            resolved_edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
        )
        if resolved is None:
            raise typer.BadParameter(f"Unknown edition '{resolved_edition}'.")
        if issue_number is None:
            raise typer.BadParameter("--flip-parity requires --issue.")
        return _run_flip_parity_doctor(
            edition_name=resolved_edition,
            program_id=resolved.program.id,
            issue_number=issue_number,
            programs_root=resolved_programs_root,
            reality_db_root=resolved_reality_db_root,
            archive_root=resolved_archive_root,
            resolved_workstreams=resolved.workstreams,
        )
    if fact_parity:
        resolved = resolve_edition(
            resolved_edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
        )
        if resolved is None:
            raise typer.BadParameter(f"Unknown edition '{resolved_edition}'.")
        return _run_fact_parity_doctor(
            edition_name=resolved_edition,
            program_id=resolved.program.id,
            programs_root=resolved_programs_root,
        )
    if fact_bridge:
        resolved = resolve_edition(
            resolved_edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
        )
        if resolved is None:
            raise typer.BadParameter(f"Unknown edition '{resolved_edition}'.")
        disabled_report = _run_bridge_disabled_doctor(
            edition_name=resolved_edition,
            program_id=resolved.program.id,
            programs_root=resolved_programs_root,
        )
        backlog_report = _run_bridge_failure_backlog_doctor(
            edition_name=resolved_edition,
            program_id=resolved.program.id,
            programs_root=resolved_programs_root,
        )
        return DoctorReport(
            edition=resolved_edition,
            checks=disabled_report.checks + backlog_report.checks,
        )
    if fact_deserialization:
        resolved = resolve_edition(
            resolved_edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
        )
        if resolved is None:
            raise typer.BadParameter(f"Unknown edition '{resolved_edition}'.")
        return _run_fact_deserialization_doctor(
            edition_name=resolved_edition,
            program_id=resolved.program.id,
            programs_root=resolved_programs_root,
        )
    if confirm_readiness:
        resolved = resolve_edition(
            resolved_edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
        )
        if resolved is None:
            raise typer.BadParameter(f"Unknown edition '{resolved_edition}'.")
        cadence_value = getattr(resolved, "cadence", None) or "weekly"
        return _run_confirm_readiness_doctor(
            edition_name=resolved_edition,
            program_id=resolved.program.id,
            programs_root=resolved_programs_root,
            editions_root=resolved_editions_root,
            archive_root=resolved_archive_root,
            cadence=cadence_value if isinstance(cadence_value, str) else "weekly",
        )
    if adapter_cert:
        resolved = resolve_edition(
            resolved_edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
        )
        if resolved is None:
            raise typer.BadParameter(f"Unknown edition '{resolved_edition}'.")
        return _run_adapter_cert_doctor(
            edition_name=resolved_edition,
            program_id=resolved.program.id,
            programs_root=resolved_programs_root,
        )
    if watch_sources:
        selected_sources = watch_command.parse_watch_sources(list(watch_source_values) or [watch_command.WatchSource.ADO.value])
        return _run_watch_source_doctor(
            edition_name=resolved_edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
            selected_sources=selected_sources,
        )
    if catchup_log:
        return _run_catchup_log_doctor(
            edition_name=resolved_edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
        )
    if nudge:
        resolved_templates_root = templates_root or TEMPLATES_ROOT
        _resolved = resolve_edition(
            resolved_edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
        )
        if _resolved is None:
            raise typer.BadParameter(f"Unknown edition '{resolved_edition}'.")
        _program_id = _resolved.paths.program_id
        checks = _run_nudge_doctor(
            _program_id,
            programs_root=resolved_programs_root,
            templates_root=resolved_templates_root,
        )
        return DoctorReport(edition=resolved_edition, checks=checks)
    if circuit_breakers:
        return _run_circuit_breaker_doctor(
            edition_name=resolved_edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
            reset=reset_circuit_breakers,
        )
    if charts:
        return _run_charts_doctor(
            edition_name=resolved_edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
        )
    if sharepoint:
        return _run_sharepoint_doctor(
            edition_name=resolved_edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
            strict_lt_alignment=strict_lt_alignment,
        )
    if schedule_health:
        resolved_edition = _resolve_edition_name(edition_name, resolved_reports_root)
        _resolved = resolve_edition(
            resolved_edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
        )
        if _resolved is None:
            raise typer.BadParameter(f"Unknown edition '{resolved_edition}'.")
        return _run_schedule_health_doctor(
            program_id=_resolved.paths.program_id,
            programs_root=resolved_programs_root,
            now=now,
            prefetch_enabled=bool(
                _resolved.program.m365 is not None
                and _resolved.program.m365.enabled
            ),
        )

    resolved_templates_root = templates_root or TEMPLATES_ROOT

    return _run_default_doctor_report(
        edition_name=resolved_edition,
        reports_root=resolved_reports_root,
        editions_root=resolved_editions_root,
        programs_root=resolved_programs_root,
        archive_root=resolved_archive_root,
        templates_root=resolved_templates_root,
        fix=fix,
        ado_probe=ado_probe,
        load_bundle_fn=load_bundle,
        validate_slice_contracts_fn=validate_slice_contracts,
        run_id_doctor=_run_id_doctor,
        probe_ado_access_fn=_probe_ado_access,
        token_check_fn=_token_check_impl,
        mail_preview_check_fn=lambda: _mail_preview_check_impl(
            environ=os.environ,
            find_spec_fn=find_spec,
        ),
        resolve_edition_fn=resolve_edition,
        template_contract_edition_check_fn=lambda bundle, *, edition_name, program_id, programs_root: _template_contract_edition_check_impl(
            bundle,
            edition_name=edition_name,
            program_id=program_id,
            programs_root=programs_root,
        ),
        config_governance_check_fn=lambda *, edition_name, resolved, editions_root, programs_root: _run_config_governance_check_impl(
            edition_name=edition_name,
            resolved=resolved,
            editions_root=editions_root,
            programs_root=programs_root,
        ),
        latest_gather_integration_check_fn=_latest_gather_integration_check,
        slice_telemetry_runtime_check_fn=_slice_telemetry_runtime_check,
        capability_review_check_fn=_capability_review_check,
        hygiene_nudge_check_fn=_hygiene_nudge_check,
        audit_hygiene_check_fn=_audit_hygiene_check,
        read_archive_index_fn=read_archive_index,
        get_archive_root_fn=get_archive_root,
        latest_snapshot_check_fn=_latest_snapshot_check_impl,
        semantic_index_enabled_fn=_semantic_index_enabled,
        build_semantic_index_checks_fn=_build_semantic_index_checks,
        load_overrides_fn=load_overrides,
        seed_overrides_fn=_seed_overrides_impl,
        template_check_fn=_template_check_impl,
        recurring_gate_failures_check_fn=_recurring_gate_failures_check,
        override_streak_check_fn=_override_streak_check,
        candidate_queue_backlog_check_fn=_candidate_queue_backlog_check,
        claim_freshness_check_fn=_claim_freshness_check,
        coverage_range_check_fn=_coverage_range_check,
        degraded_confirm_check_fn=_degraded_confirm_check,
        ledger_health_check_fn=_ledger_health_check,
        external_dependencies_check_fn=_external_dependencies_check,
        directory_size_fn=directory_size,
        format_bytes_fn=format_bytes,
        default_banned_phrases=DEFAULT_BANNED_PHRASES,
        runtime_layout_check_fn=lambda program_id, programs_root: _dc02_runtime_layout_check(
            program_id, programs_root=programs_root
        ),
    )


def _run_semantic_index_doctor(*, edition_name: str, editions_root: Path, programs_root: Path, archive_root: Path) -> DoctorReport:
    return _run_semantic_index_doctor_impl(
        edition_name=edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
        archive_root=archive_root,
        semantic_index_enabled_fn=_semantic_index_enabled,
        build_semantic_index_checks_fn=_build_semantic_index_checks,
    )


def _run_readiness_doctor(*, edition_name: str, editions_root: Path, programs_root: Path) -> DoctorReport:
    return _run_readiness_doctor_impl(
        edition_name=edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
        readiness_gate_settings_fn=readiness_gate_settings,
    )



def _run_channel_doctor(*, edition_name: str, reports_root: Path, editions_root: Path, programs_root: Path) -> DoctorReport:
    return _run_channel_doctor_impl(
        edition_name=edition_name,
        reports_root=reports_root,
        editions_root=editions_root,
        programs_root=programs_root,
        resolve_edition_fn=resolve_edition,
        load_gather_state_fn=load_gather_state,
        load_bundle_fn=load_bundle,
        current_doctor_kusto_targets_fn=_current_doctor_kusto_targets,
        channel_last_error_fn=_channel_last_error,
        channel_auth_failure_detail_fn=_channel_auth_failure_detail,
        build_m365_registry_review_metadata_fn=_build_m365_registry_review_metadata,
        summarize_m365_discovery_fn=_summarize_m365_discovery,
        summarize_m365_registry_review_fn=_summarize_m365_registry_review,
        slice_source_health_check_fn=_slice_source_health_check,
        load_source_waivers_fn=load_source_waivers,
        source_health_function_name_for_edition_fn=source_health_function_name_for_edition,
        conversion_fidelity_check_fn=_conversion_fidelity_check,
        eta_credibility_check_fn=_eta_credibility_check,
        m365_discovery_check_fn=_m365_discovery_check,
        m365_registry_review_check_fn=_m365_registry_review_check,
        m365_registry_promotion_check_fn=_m365_registry_promotion_check,
        uil_registry_checks_fn=_uil_registry_checks,
        ado_pr_coverage_check_fn=_ado_pr_coverage_check,
        channel_delta_check_fn=_channel_delta_check,
        channel_detail_check_fn=_channel_detail_check,
    )


def _uil_registry_checks(channel_entries: dict[str, dict[str, Any]]) -> list[DoctorCheck]:
    return _uil_registry_checks_impl(channel_entries)


def _uil_registry_check(channel_name: str, channel_entry: Any) -> DoctorCheck | None:
    return _uil_registry_check_impl(channel_name, channel_entry)


def _ado_pr_coverage_check(workstreams: tuple[Any, ...]) -> DoctorCheck:
    return _ado_pr_coverage_check_impl(workstreams)


def _channel_detail_check(
    channel_name: str,
    entry: dict[str, Any],
    *,
    current_kusto_targets: tuple[str, ...] = (),
) -> DoctorCheck:
    return _channel_detail_check_impl(
        channel_name,
        entry,
        current_kusto_targets=current_kusto_targets,
        channel_auth_failure_detail_fn=_channel_auth_failure_detail,
    )


def _channel_last_error(
    channel_name: str,
    entry: dict[str, Any],
    *,
    current_kusto_targets: tuple[str, ...] = (),
) -> str | None:
    return _channel_last_error_impl(
        channel_name,
        entry,
        current_kusto_targets=current_kusto_targets,
    )


def _extract_kusto_failed_target(last_error: str) -> str | None:
    return _extract_kusto_failed_target_impl(last_error)


def _m365_discovery_check(entry: dict[str, Any], *, previous_entry: dict[str, Any] | None = None) -> DoctorCheck:
    return _m365_discovery_check_impl(entry, previous_entry=previous_entry)


def _summarize_m365_discovery(entry: dict[str, Any]) -> str:
    return _summarize_m365_discovery_impl(entry)


def _build_m365_registry_review_metadata(program_id: str, *, programs_root: Path) -> dict[str, Any]:
    return _build_m365_registry_review_metadata_impl(
        program_id,
        programs_root=programs_root,
        load_m365_registry_fn=load_m365_registry,
        read_m365_routing_feedback_events_fn=read_m365_routing_feedback_events,
        load_current_workstreams_fn=load_current_workstreams,
        load_approved_m365_corpus_signals_fn=load_approved_m365_corpus_signals,
        build_m365_corpus_texts_by_workstream_fn=build_m365_corpus_texts_by_workstream,
        suggest_keyword_expansions_fn=suggest_keyword_expansions,
        describe_current_m365_registry_promotion_blockers_fn=describe_current_m365_registry_promotion_blockers,
        is_current_m365_registry_promotion_candidate_fn=is_current_m365_registry_promotion_candidate,
    )


def _artifact_is_missing_m365_id(artifact: M365RegistryArtifact) -> bool:
    return _artifact_is_missing_m365_id_impl(artifact)


def _summarize_m365_registry_review(metadata: dict[str, Any]) -> str:
    return _summarize_m365_registry_review_impl(metadata)


def _m365_registry_review_check(metadata: dict[str, Any]) -> DoctorCheck:
    return _m365_registry_review_check_impl(metadata)


def _m365_registry_promotion_check(metadata: dict[str, Any]) -> DoctorCheck:
    return _m365_registry_promotion_check_impl(metadata)


def _channel_delta_check(
    *,
    previous_gathered_at: datetime,
    current_channels: dict[str, dict[str, Any]],
    previous_channels: dict[str, dict[str, Any]],
    current_failed_queries: list[str],
    previous_query_states: dict[str, dict[str, Any]],
    current_stale_queries: list[str],
    current_frozen_queries: list[str],
    current_m365_discovery: dict[str, Any],
    previous_m365_discovery: dict[str, Any],
) -> DoctorCheck:
    return _channel_delta_check_impl(
        previous_gathered_at=previous_gathered_at,
        current_channels=current_channels,
        previous_channels=previous_channels,
        current_failed_queries=current_failed_queries,
        previous_query_states=previous_query_states,
        current_stale_queries=current_stale_queries,
        current_frozen_queries=current_frozen_queries,
        current_m365_discovery=current_m365_discovery,
        previous_m365_discovery=previous_m365_discovery,
    )


def _channel_health_snapshot(channel_entries: dict[str, dict[str, Any]]) -> tuple[list[str], list[str], int]:
    return _channel_health_snapshot_impl(channel_entries)


def _summarize_m365_discovery_comparison(entry: dict[str, Any], previous_entry: dict[str, Any] | None) -> str:
    return _summarize_m365_discovery_comparison_impl(entry, previous_entry)


def _capability_review_check(
    program_id: str | None,
    programs_root: Path,
    *,
    warn_on_incomplete: bool = False,
) -> DoctorCheck | None:
    return _run_capability_review_check_impl(
        program_id,
        programs_root,
        warn_on_incomplete=warn_on_incomplete,
    )


def _hygiene_nudge_check(*, resolved, programs_root: Path) -> DoctorCheck | None:
    return _run_hygiene_nudge_check_impl(resolved=resolved, programs_root=programs_root)


def _run_auth_doctor(
    *,
    edition_name: str,
    reports_root: Path,
    editions_root: Path,
    programs_root: Path,
    ado_probe: Callable[..., ADOProbeResult] | None,
    kusto_probe: Callable[[KustoQuery], None] | None,
) -> DoctorReport:
    return _run_auth_doctor_impl(
        edition_name=edition_name,
        reports_root=reports_root,
        editions_root=editions_root,
        programs_root=programs_root,
        ado_probe=ado_probe,
        kusto_probe=kusto_probe,
        load_bundle_fn=load_bundle,
        probe_ado_access_fn=_probe_ado_access,
        token_check_fn=_token_check_impl,
        mail_preview_check_fn=lambda: _mail_preview_check_impl(
            environ=os.environ,
            find_spec_fn=find_spec,
        ),
        agency_bridge_factory=AgencyBridge,
        agency_cli_check_fn=_agency_cli_check_impl,
        resolve_edition_fn=resolve_edition,
        load_doctor_kusto_queries_fn=_load_doctor_kusto_queries_impl,
        kusto_validation_check_fn=_kusto_validation_check_impl,
        icm_kusto_check_fn=_icm_kusto_check_impl,
        capability_review_check_fn=_capability_review_check,
        kusto_target_labels_fn=_kusto_target_labels_impl,
        validate_kusto_query_definitions_fn=_validate_kusto_query_definitions_impl,
        summarize_kusto_targets_fn=_summarize_kusto_targets_impl,
        build_live_kusto_query_probe_fn=build_live_kusto_query_probe,
    )


def _resolve_edition_name(edition_name: str | None, reports_root: Path) -> str:
    if edition_name is not None:
        return edition_name
    editions = discover_report_editions(reports_root=reports_root)
    if len(editions) == 1:
        return editions[0]
    raise typer.BadParameter("Provide --edition when multiple editions exist.")


def _run_cadence_doctor(*, edition_name: str, editions_root: Path, programs_root: Path, archive_root: Path) -> DoctorReport:
    return _run_cadence_doctor_impl(
        edition_name=edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
        archive_root=archive_root,
        describe_cadence_status_fn=describe_cadence_status,
    )


def _run_privacy_doctor(*, edition_name: str, editions_root: Path, programs_root: Path) -> DoctorReport:
    return _run_privacy_doctor_impl(
        edition_name=edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
    )


def _run_kusto_doctor(
    *,
    edition_name: str,
    editions_root: Path,
    programs_root: Path,
    kusto_probe: Callable[[KustoQuery], None] | None,
) -> DoctorReport:
    return _run_kusto_doctor_impl(
        edition_name=edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
        kusto_probe=kusto_probe,
        live_kusto_probe_fn=_live_kusto_probe,
    )


def _run_metric_binding_doctor(
    *,
    edition_name: str,
    editions_root: Path,
    programs_root: Path,
    reality_db_root: Path | None,
    metric_binding_probe: MetricBindingProbe | None,
    metric_definitions: dict[str, MetricDefinition] | None,
    now: datetime | None,
) -> DoctorReport:
    return _run_metric_binding_doctor_impl(
        edition_name=edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
        reality_db_root=reality_db_root,
        metric_binding_probe=metric_binding_probe,
        metric_definitions=metric_definitions,
        now=now,
        revalidation_days=_METRIC_BINDING_REVALIDATION_DAYS,
    )


def _run_watch_source_doctor(
    *,
    edition_name: str,
    editions_root: Path,
    programs_root: Path,
    selected_sources: tuple[watch_command.WatchSource, ...],
) -> DoctorReport:
    return _run_watch_source_doctor_impl(
        edition_name=edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
        selected_sources=selected_sources,
    )


def _run_catchup_log_doctor(*, edition_name: str, editions_root: Path, programs_root: Path) -> DoctorReport:
    return _run_catchup_log_doctor_impl(
        edition_name=edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
    )


def _run_kb_doctor(*, editions_root: Path, programs_root: Path, check_origins: bool = False) -> DoctorReport:
    return _run_kb_doctor_impl(
        editions_root=editions_root,
        programs_root=programs_root,
        check_origins=check_origins,
        ado_client_factory=ADOClient,
    )


# =============================================================================
# Context Doctor — §7, spec program-context-maturity.md
# Validates cross-file invariants (§5), staleness (§8), and schema versions
# =============================================================================

def _run_ranked_gaps(
    *, edition_name: str | None, programs_root: Path, editions_root: Path
) -> None:
    return _run_ranked_gaps_impl(
        edition_name=edition_name,
        programs_root=programs_root,
        editions_root=editions_root,
    )


def _run_rev_health(
    *,
    edition: str | None,
    rev_program: str | None,
    programs_root: Path,
    editions_root: Path,
    format: str,
) -> None:
    """``doctor --rev-health`` — print the REV subsystem health summary.

    Resolves the target program from ``--rev-program`` or the resolved edition,
    then aggregates run-state + verification + evidence-vault health (read-only).
    """
    import json as _json

    from src.core.rev.health import build_rev_health_report, render_rev_health_human

    program_id = rev_program
    if program_id is None:
        ed_paths = _resolve_edition_paths(edition or "", editions_root=editions_root, programs_root=programs_root)
        if ed_paths is None:
            typer.echo("rev-health: pass --rev-program <id> or --edition <name> to resolve a program.")
            raise typer.Exit(code=2)
        program_id = ed_paths.program_id
    report = build_rev_health_report(program_id, programs_root=programs_root)
    if format == "json":
        typer.echo(_json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        typer.echo(render_rev_health_human(report), nl=False)
    # Non-zero when shield degrade is active (Azure Prompt Shields not wired) so
    # the operator sees the visible-degrade state in CI/exit code.
    raise typer.Exit(code=1 if report.shield_degrade else 0)


def _run_context_doctor(
    *, edition_name: str | None, programs_root: Path, editions_root: Path,
    fix_hints: bool = False,
) -> DoctorReport:
    return _run_context_doctor_impl(
        edition_name=edition_name,
        programs_root=programs_root,
        editions_root=editions_root,
        fix_hints=fix_hints,
    )
def _run_id_doctor(*, edition_name: str, reports_root: Path, editions_root: Path, programs_root: Path) -> DoctorReport:
    return _run_id_doctor_impl(
        edition_name=edition_name,
        reports_root=reports_root,
        editions_root=editions_root,
        programs_root=programs_root,
        load_dependency_workstream_ids_fn=_load_dependency_workstream_ids,
        load_scorecard_dimension_bindings_fn=_load_scorecard_dimension_bindings,
    )


def _live_kusto_probe() -> Callable[[KustoQuery], None]:
    return _build_live_kusto_probe_impl()


def _run_milestone_doctor(*, edition_name: str, editions_root: Path, programs_root: Path, archive_root: Path) -> DoctorReport:
    return _run_milestone_doctor_impl(
        edition_name=edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
        archive_root=archive_root,
        load_current_milestones_fn=lambda program_id: load_current_milestones(program_id, programs_root=programs_root),
        load_milestone_owner_aliases_fn=lambda program_id: load_milestone_owner_aliases(program_id, programs_root=programs_root),
        build_milestone_health_warning_fn=lambda milestone_edition_name, program_id, milestones: _build_milestone_health_warning(
            edition_name=milestone_edition_name,
            program_id=program_id,
            milestones=milestones,
            programs_root=programs_root,
            archive_root=archive_root,
        ),
    )


def _run_circuit_breaker_doctor(
    *,
    edition_name: str,
    editions_root: Path,
    programs_root: Path,
    reset: bool,
) -> DoctorReport:
    return _run_circuit_breaker_doctor_impl(
        edition_name=edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
        reset=reset,
        display_path_fn=lambda path: display_path(path, programs_root=programs_root, repo_root=REPO_ROOT),
        describe_circuit_breaker_snapshot_fn=lambda snapshot, state_path, state_exists: describe_circuit_breaker_snapshot(
            snapshot,
            path_label=display_path(state_path, programs_root=programs_root, repo_root=REPO_ROOT),
            state_exists=state_exists,
        ),
        default_breaker_snapshot_fn=default_breaker_snapshot,
    )


def _build_milestone_health_warning(
    *,
    edition_name: str,
    program_id: str,
    milestones,
    programs_root: Path,
    archive_root: Path,
) -> str | None:
    return _build_milestone_health_warning_impl(
        edition_name=edition_name,
        program_id=program_id,
        milestones=milestones,
        programs_root=programs_root,
        archive_root=archive_root,
    )


def _load_latest_confirmed_snapshot_items(
    edition_name: str,
    *,
    archive_root: Path,
) -> tuple[tuple[WorkItem, ...], datetime] | None:
    return _load_latest_confirmed_snapshot_items_impl(
        edition_name,
        archive_root=archive_root,
    )


def _snapshot_to_work_items(snapshot) -> tuple[WorkItem, ...]:
    return _snapshot_to_work_items_impl(snapshot)


def _run_dependency_doctor(*, edition_name: str, editions_root: Path, programs_root: Path) -> DoctorReport:
    return _run_dependency_doctor_impl(
        edition_name=edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
        count_legacy_dependencies_fn=lambda program_id: count_legacy_dependencies(program_id, programs_root=programs_root),
        validate_dependency_references_fn=lambda dependencies: validate_dependency_references(
            dependencies,
            programs_root=programs_root,
            load_dependency_workstream_ids_fn=lambda program_id: _load_dependency_workstream_ids(
                program_id,
                programs_root=programs_root,
            ),
            load_dependency_milestone_ids_fn=lambda program_id: load_dependency_milestone_ids(
                program_id,
                programs_root=programs_root,
            ),
        ),
        get_dependencies_path_fn=lambda program_id: get_dependencies_path(program_id, programs_root=programs_root),
    )


def _run_risk_doctor(*, edition_name: str, editions_root: Path, programs_root: Path) -> DoctorReport:
    return _run_risk_doctor_impl(
        edition_name=edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
        load_milestone_owner_aliases_fn=lambda program_id: load_milestone_owner_aliases(program_id, programs_root=programs_root),
        load_current_milestones_fn=lambda program_id: load_current_milestones(program_id, programs_root=programs_root),
    )


def _run_escalation_doctor(*, edition_name: str, editions_root: Path, programs_root: Path) -> DoctorReport:
    return _run_escalation_doctor_impl(
        edition_name=edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
    )


def _run_action_doctor(*, edition_name: str, editions_root: Path, programs_root: Path) -> DoctorReport:
    return _run_action_doctor_impl(
        edition_name=edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
        load_milestone_owner_aliases_fn=lambda program_id: load_milestone_owner_aliases(program_id, programs_root=programs_root),
    )


def _run_decision_doctor(*, edition_name: str, editions_root: Path, programs_root: Path) -> DoctorReport:
    return _run_decision_doctor_impl(
        edition_name=edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
        load_milestone_owner_aliases_fn=lambda program_id: load_milestone_owner_aliases(program_id, programs_root=programs_root),
    )


def _run_assumption_doctor(*, edition_name: str, editions_root: Path, programs_root: Path) -> DoctorReport:
    return _run_assumption_doctor_impl(
        edition_name=edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
        load_milestone_owner_aliases_fn=lambda program_id: load_milestone_owner_aliases(program_id, programs_root=programs_root),
        load_current_milestones_fn=lambda program_id: load_current_milestones(program_id, programs_root=programs_root),
    )


def _load_dependency_workstream_ids(program_id: str, *, programs_root: Path) -> tuple[str, ...]:
    return _load_dependency_workstream_ids_impl(
        program_id,
        programs_root=programs_root,
        load_current_workstreams_fn=lambda current_program_id: load_current_workstreams(
            current_program_id,
            programs_root=programs_root,
        ),
    )
def _consistency_check(
    edition: str,
    *,
    archive_root: Path,
    reports_root: Path,
    editions_root: Path,
    programs_root: Path,
) -> DoctorCheck:
    return _consistency_check_impl(
        edition,
        archive_root=archive_root,
        reports_root=reports_root,
        editions_root=editions_root,
        programs_root=programs_root,
    )


def _run_operator_gates_doctor(
    *,
    edition_name: str,
    reports_root: Path,
    archive_root: Path,
    editions_root: Path,
    programs_root: Path,
    ado_probe: Callable[..., ADOProbeResult] | None,
    kusto_probe: Callable[[KustoQuery], None] | None,
    metric_binding_probe: MetricBindingProbe | None,
    metric_definitions: dict[str, MetricDefinition] | None,
    reality_db_root: Path | None,
    now: datetime | None,
) -> DoctorReport:
    return _run_operator_gates_doctor_impl(
        edition_name=edition_name,
        reports_root=reports_root,
        archive_root=archive_root,
        editions_root=editions_root,
        programs_root=programs_root,
        ado_probe=ado_probe,
        kusto_probe=kusto_probe,
        metric_binding_probe=metric_binding_probe,
        metric_definitions=metric_definitions,
        reality_db_root=reality_db_root,
        now=now,
        resolve_edition_fn=resolve_edition,
        run_auth_doctor=_run_auth_doctor,
        run_channel_doctor=_run_channel_doctor,
        run_metric_binding_doctor=_run_metric_binding_doctor,
        run_checkpoint_doctor=_run_checkpoint_doctor,
        build_m365_registry_review_metadata=_build_m365_registry_review_metadata,
        load_gather_state_fn=load_gather_state,
        agency_bridge_factory=AgencyBridge,
        operator_gate_m365_ids_check=_operator_gate_m365_ids_check,
        operator_gate_transcript_health_check=_operator_gate_transcript_health_check,
        operator_gate_kusto_validation_check=_operator_gate_kusto_validation_check,
        operator_gate_checkpoint_creation_check=_operator_gate_checkpoint_creation_check,
        operator_gate_rollback_drill_check=_operator_gate_rollback_drill_check,
    )


def _summarize_missing_id_diagnostics(diagnostics: list[dict[str, Any]]) -> str:
    return _summarize_missing_id_diagnostics_impl(diagnostics)


def _build_missing_id_diagnostics(
    *,
    missing_id_artifacts: list[dict[str, Any]],
    m365_discovery: dict[str, Any] | None,
    agency_caps: AgencyCapabilities,
) -> list[dict[str, Any]]:
    return _build_missing_id_diagnostics_impl(
        missing_id_artifacts=missing_id_artifacts,
        m365_discovery=m365_discovery,
        agency_caps=agency_caps,
    )


def _source_ref_kind_for_artifact_type(artifact_type: str) -> SourceRefKind | None:
    return _source_ref_kind_for_artifact_type_impl(artifact_type)


def _source_channel_for_ref_kind(ref_kind: SourceRefKind) -> str:
    return _source_channel_for_ref_kind_impl(ref_kind)


def _build_missing_id_action_categories(
    *,
    program_id: str,
    programs_root: Path,
    missing_id_artifacts: list[dict[str, Any]],
    artifact_diagnostics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return _build_missing_id_action_categories_impl(
        program_id=program_id,
        programs_root=programs_root,
        missing_id_artifacts=missing_id_artifacts,
        artifact_diagnostics=artifact_diagnostics,
        load_m365_registry_fn=load_m365_registry,
    )


def _operator_gate_m365_ids_check(
    *,
    program_id: str,
    programs_root: Path,
    edition_name: str,
    registry_review: dict[str, Any] | None,
    m365_discovery: dict[str, Any] | None,
    agency_caps: AgencyCapabilities,
) -> DoctorCheck:
    return _operator_gate_m365_ids_check_impl(
        program_id=program_id,
        programs_root=programs_root,
        edition_name=edition_name,
        registry_review=registry_review,
        m365_discovery=m365_discovery,
        agency_caps=agency_caps,
        load_m365_registry_fn=load_m365_registry,
    )


def _operator_gate_transcript_health_check(
    *,
    edition_name: str,
    transcript_check: DoctorCheck | None,
    source_health_check: DoctorCheck | None,
) -> DoctorCheck:
    return _operator_gate_transcript_health_check_impl(
        edition_name=edition_name,
        transcript_check=transcript_check,
        source_health_check=source_health_check,
    )


def _operator_gate_kusto_validation_check(
    *,
    edition_name: str,
    kusto_enabled: bool = True,
    kusto_access_check: DoctorCheck | None,
    kusto_validation_check: DoctorCheck | None,
    metric_bindings_check: DoctorCheck | None,
    metric_rollout_check: DoctorCheck | None,
) -> DoctorCheck:
    return _operator_gate_kusto_validation_check_impl(
        edition_name=edition_name,
        kusto_enabled=kusto_enabled,
        kusto_access_check=kusto_access_check,
        kusto_validation_check=kusto_validation_check,
        metric_bindings_check=metric_bindings_check,
        metric_rollout_check=metric_rollout_check,
    )


def _operator_gate_checkpoint_creation_check(*, edition_name: str, checkpoint_inventory_check: DoctorCheck | None) -> DoctorCheck:
    return _operator_gate_checkpoint_creation_check_impl(
        edition_name=edition_name,
        checkpoint_inventory_check=checkpoint_inventory_check,
    )


def _operator_gate_rollback_drill_check(
    *,
    edition_name: str,
    checkpoint_inventory_check: DoctorCheck | None,
    checkpoint_coverage_check: DoctorCheck | None,
    editions_root: Path,
    programs_root: Path,
) -> DoctorCheck:
    return _operator_gate_rollback_drill_check_impl(
        edition_name=edition_name,
        checkpoint_inventory_check=checkpoint_inventory_check,
        checkpoint_coverage_check=checkpoint_coverage_check,
        editions_root=editions_root,
        programs_root=programs_root,
    )


def _run_platform_readiness_doctor(
    *,
    programs_root: Path,
    reports_root: Path,
    editions_root: Path,
) -> DoctorReport:
    return _run_platform_readiness_doctor_impl(
        programs_root=programs_root,
        reports_root=reports_root,
        editions_root=editions_root,
        run_channel_doctor_fn=_run_channel_doctor,
    )


def _probe_ado_access(bundle) -> ADOProbeResult:
    return _probe_ado_access_impl(bundle)
