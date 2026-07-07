from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StateReaderRegistration:
    state_name: str
    path_pattern: str
    owner_module: str
    reader_symbols: tuple[str, ...]


STATE_READER_REGISTRY: dict[str, StateReaderRegistration] = {
    "gather_state": StateReaderRegistration(
        state_name="gather_state",
        path_pattern="programs/<program>/runtime/gather_state.json",
        owner_module="src.core.gather_state_store",
        reader_symbols=("load_gather_state", "load_gather_query_states", "get_gather_state_path"),
    ),
    "claim_log": StateReaderRegistration(
        state_name="claim_log",
        path_pattern="programs/<program>/journal/claims.jsonl",
        owner_module="src.core.claim_tracker",
        reader_symbols=("read_claim_log", "load_claim_entries", "load_decision_asks", "load_open_claims"),
    ),
    "ledger_event_log": StateReaderRegistration(
        # Program event ledger: authoritative append-only event stream for
        # replay, verify, indexing, and projection rebuilds.
        state_name="ledger_event_log",
        path_pattern="programs/<program>/ledger/events/*.events.jsonl",
        owner_module="src.core.ledger.event_log",
        reader_symbols=(
            "read_events",
            "iter_event_records",
            "get_event_logs_dir",
            "get_active_event_log_path",
            "write_event",
            "write_events",
        ),
    ),
    "trajectory": StateReaderRegistration(
        state_name="trajectory",
        path_pattern="programs/<program>/trajectories/<work_item>.jsonl",
        owner_module="src.core.trajectory",
        reader_symbols=("read_trajectory", "load_all_trajectories", "get_trajectory_path"),
    ),
    "autonomy_audit": StateReaderRegistration(
        state_name="autonomy_audit",
        path_pattern="programs/<program>/journal/autonomy_audit.jsonl",
        owner_module="src.core.analytics_store",
        reader_symbols=("load_autonomy_audit_records",),
    ),
    "review_policy_audit": StateReaderRegistration(
        state_name="review_policy_audit",
        path_pattern="programs/<program>/journal/review_policy_audit.jsonl",
        owner_module="src.core.signal_review",
        reader_symbols=("load_review_policy_audit_entries", "get_review_policy_audit_path"),
    ),
    "review_log": StateReaderRegistration(
        state_name="review_log",
        path_pattern="programs/<program>/journal/reviews.jsonl",
        owner_module="src.core.journal",
        reader_symbols=("read_review_log",),
    ),
    "signal_threads": StateReaderRegistration(
        state_name="signal_threads",
        path_pattern="programs/<program>/journal/signal_threads.jsonl",
        owner_module="src.core.journal",
        reader_symbols=("read_signal_thread_log",),
    ),
    "actions": StateReaderRegistration(
        state_name="actions",
        path_pattern="programs/<program>/journal/actions.jsonl",
        owner_module="src.core.action_tracker",
        reader_symbols=("load_actions", "read_action_log", "get_actions_path"),
    ),
    "ai_proposals": StateReaderRegistration(
        state_name="ai_proposals",
        path_pattern="programs/<program>/journal/ai_proposals.jsonl",
        owner_module="src.core.ai_proposal_store",
        reader_symbols=("get_ai_proposals_path",),
    ),
    "edit_patterns": StateReaderRegistration(
        state_name="edit_patterns",
        path_pattern="programs/<program>/journal/edit_patterns.jsonl",
        owner_module="src.core.feedback.salience_modeler",
        reader_symbols=("read_edit_pattern_observations", "get_author_salience_path", "get_salience_events_path"),
    ),
    "risk_updates": StateReaderRegistration(
        state_name="risk_updates",
        path_pattern="programs/<program>/journal/risk_updates.jsonl",
        owner_module="src.core.risk_register_engine",
        reader_symbols=("get_risk_updates_path",),
    ),
    "brief_interventions": StateReaderRegistration(
        state_name="brief_interventions",
        path_pattern="programs/<program>/_feedback/brief_interventions.jsonl",
        owner_module="src.core.brief_intervention_store",
        reader_symbols=("load_brief_intervention_resolutions", "get_brief_interventions_path"),
    ),
    "chronicle": StateReaderRegistration(
        state_name="chronicle",
        path_pattern="programs/<program>/chronicle.jsonl",
        owner_module="src.core.chronicle",
        reader_symbols=("load_chronicle_records", "get_chronicle_path"),
    ),
    "claim_extraction_calibration": StateReaderRegistration(
        # Phase 1-B sweep: file lives under _feedback/ (not journal/ as the pre-
        # Phase-0.5 registry said). The owner store always wrote to _feedback/;
        # this entry was stale. reader_symbols corrected to the actual exported
        # function names from claim_extraction_calibration_store.py.
        state_name="claim_extraction_calibration",
        path_pattern="programs/<program>/_feedback/claim_extraction_calibration.jsonl",
        owner_module="src.core.claim_extraction_calibration_store",
        reader_symbols=("load_claim_extraction_calibration_records", "get_claim_extraction_calibration_path"),
    ),
    "context_gaps": StateReaderRegistration(
        state_name="context_gaps",
        path_pattern="programs/<program>/_feedback/context_gaps.jsonl",
        owner_module="src.core.context_gap_store",
        reader_symbols=("load_context_gaps", "get_context_gaps_path"),
    ),
    "external_dependencies": StateReaderRegistration(
        state_name="external_dependencies",
        path_pattern="programs/<program>/external_dependencies.jsonl",
        owner_module="src.core.external_dependency",
        reader_symbols=("load_external_dependencies", "get_external_dependencies_path"),
    ),
    "incident_journal": StateReaderRegistration(
        state_name="incident_journal",
        path_pattern="programs/<program>/journal/incident_journal.jsonl",
        owner_module="src.core.incident_journal_store",
        reader_symbols=("load_incident_journal", "get_incident_journal_path"),
    ),
    "m365_registry_log": StateReaderRegistration(
        state_name="m365_registry_log",
        path_pattern="programs/<program>/journal/m365_registry_log.jsonl",
        owner_module="src.core.m365_registry_store",
        reader_symbols=("load_m365_registry_log", "get_m365_registry_log_path"),
    ),
    "plane1_changelog": StateReaderRegistration(
        state_name="plane1_changelog",
        path_pattern="programs/<program>/journal/plane1_changelog.jsonl",
        owner_module="src.core.plane1_changelog",
        reader_symbols=("load_plane1_changelog", "get_plane1_changelog_path"),
    ),
    "section_proposals": StateReaderRegistration(
        state_name="section_proposals",
        path_pattern="programs/<program>/journal/section_proposals.jsonl",
        owner_module="src.core.section_proposal_store",
        reader_symbols=("load_section_proposals", "get_section_proposals_path"),
    ),
    "workstream_associations": StateReaderRegistration(
        state_name="workstream_associations",
        path_pattern="programs/<program>/journal/workstream_associations.jsonl",
        owner_module="src.core.workstream_association_store",
        reader_symbols=("load_workstream_associations", "get_workstream_associations_path"),
    ),
    "migration_log": StateReaderRegistration(
        # WS-11/WS-16: per-program chart-id + schema-major migration log.
        # Lives at the program root (not under journal/) because it predates
        # the journal layout and is the audit trail for `vertex migrate`
        # operations on a program's tracked configs.
        state_name="migration_log",
        path_pattern="programs/<program>/migration_log.jsonl",
        owner_module="src.core.migration_log",
        reader_symbols=("read_migration_log", "migration_log_path", "append_migration_log"),
    ),
    "run_telemetry": StateReaderRegistration(
        # WS-17: per-run perf telemetry (per-channel wall-time + P50/P95).
        # Phase 1-B: moved to runtime/ (declutter.md §6 1-B).
        state_name="run_telemetry",
        path_pattern="programs/<program>/runtime/run_telemetry.jsonl",
        owner_module="src.core.run_telemetry",
        reader_symbols=("read_run_telemetry", "build_channel_perf_summary", "run_telemetry_path"),
    ),
    "m365_registry": StateReaderRegistration(
        # Phase 1-B: M365 discovery cache + pm_confirmed promotions; T-3b.
        # Moved from program root to runtime/ (declutter.md §6 1-B).
        state_name="m365_registry",
        path_pattern="programs/<program>/runtime/m365_registry.yaml",
        owner_module="src.core.m365_registry_store",
        reader_symbols=("load_m365_registry", "get_m365_registry_path", "resolve_m365_registry_path_for_read"),
    ),
    "channel_registry": StateReaderRegistration(
        # Phase 1-B: per-channel signal state + pm_confirmed registrations; T-3b.
        # Moved from program root to runtime/ (declutter.md §6 1-B).
        state_name="channel_registry",
        path_pattern="programs/<program>/runtime/channel_registry.sqlite3",
        owner_module="src.core.channel_registry_store",
        reader_symbols=("get_channel_registry_path", "resolve_channel_registry_path_for_read"),
    ),
    "vertex_analytics": StateReaderRegistration(
        # Phase 1-B: analytics projection DB; T-3b (rebuild not lossless per A-11).
        # Moved from program root to runtime/ (declutter.md §6 1-B).
        state_name="vertex_analytics",
        path_pattern="programs/<program>/runtime/vertex_analytics.sqlite3",
        owner_module="src.core.analytics_store",
        reader_symbols=("get_program_analytics_store_path",),
    ),
    "readiness_snapshot": StateReaderRegistration(
        # Phase 1-B: last readiness fetch snapshot; T-3a (disposable but read by
        # readiness-gated confirm). Moved from program root to runtime/.
        state_name="readiness_snapshot",
        path_pattern="programs/<program>/runtime/readiness_snapshot.yaml",
        owner_module="src.core.readiness_engine",
        reader_symbols=("get_readiness_snapshot_path", "load_readiness_snapshot"),
    ),
    "alerts": StateReaderRegistration(
        # WS-17: between-runs alert sidecar (NG-3-respecting, no-daemon).
        # Lives under programs/<program>/_alerts/alerts.jsonl so the alert
        # row is read/written by name (D-18 contract) and surfaced at the
        # top of the next gather/confirm/doctor session.
        state_name="alerts",
        path_pattern="programs/<program>/_alerts/alerts.jsonl",
        owner_module="src.core.alerts",
        reader_symbols=("read_alerts", "append_alert", "resolve_alert", "surface_alert_banner"),
    ),
    "flake_buckets": StateReaderRegistration(
        # WS-13/PB-49: per-test flake-bucket tracking. Records how many
        # times a test has flaked + its status (open|quarantined|fixed)
        # + an owner. Powers `vertex observability flakes` (planned) and
        # the CI flake-bucket dashboard.
        state_name="flake_buckets",
        path_pattern="programs/<program>/_state/flake_buckets.jsonl",
        owner_module="src.core.flake_buckets",
        reader_symbols=(
            "record_flake",
            "quarantine_flake",
            "mark_flake_fixed",
            "read_flake_buckets",
            "flake_buckets_path",
        ),
    ),
    "audit_chain_proof": StateReaderRegistration(
        # WS-18: the per-program autonomy-audit hash-chain head + integrity
        # status. The chain itself lives inside the autonomy_audit.jsonl
        # sidecar; this entry records the chain-state helpers (verify,
        # excise) so a D-18 audit can confirm tamper-evidence for the
        # highest-risk sidecar in the platform.
        state_name="audit_chain_proof",
        path_pattern="programs/<program>/journal/autonomy_audit.jsonl",
        owner_module="src.core.audit_query",
        reader_symbols=(
            "verify_autonomy_audit_chain",
            "excise_pii_from_autonomy_audit",
            "compute_record_hash",
            "read_chain_head_hash",
            "build_audit_query",
            "append_chain_record",
        ),
    ),
    "model_registry": StateReaderRegistration(
        # WS-24: per-program model-version pin registry. Records which
        # ``model_id`` + ``deployment_id`` each AI feature is allowed to
        # use, with pinned_at / recert_at / deprecation_review_at dates.
        # Read by `vertex audit query --source model-registry` and by
        # `FallbackAIClient` to detect live-deployment drift.
        state_name="model_registry",
        path_pattern="programs/<program>/_state/model_registry.jsonl",
        owner_module="src.core.model_registry",
        reader_symbols=(
            "read_model_registry",
            "read_model_pin",
            "register_model_pin",
            "record_model_deployment_used",
            "model_registry_path",
        ),
    ),
    "ai_telemetry": StateReaderRegistration(
        # WS-5b: per-program AI-call telemetry sidecar. Every AI call
        # (success or failure) appends one record with ts, feature,
        # deployment_id, status (ok/rate_limit/context_length/auth/
        # timeout/budget_exceeded/other), latency_ms, tokens_in,
        # tokens_out, cost_usd, program_id.  Used by QG-27 (budget gate)
        # and by ``vertex calibration --cost`` to surface per-feature spend.
        state_name="ai_telemetry",
        path_pattern="programs/<program>/_state/ai_telemetry.jsonl",
        owner_module="src.core.ai_telemetry",
        reader_symbols=(
            "read_ai_telemetry",
            "append_ai_telemetry",
            "ai_telemetry_path",
            "build_feature_cost_summary",
        ),
    ),
    "program_reality": StateReaderRegistration(
        # WI-1.1: Single read facade for cross-source program reality (G-1).
        # Wraps fact-store snapshots + legacy projectors behind one interface.
        # All projections (report, risk board, triage, etc.) read ONLY through
        # ProgramReality — never directly from the fact-store or legacy stores.
        state_name="program_reality",
        path_pattern="programs/<program>/",
        owner_module="src.core.program_reality",
        reader_symbols=(
            "ProgramReality",
            "FactAssessment",
            "RealityDelta",
            "AttentionItem",
            "AttentionKind",
            "EvidenceRef",
            "RealityConflict",
            "RealityDomainFreshness",
            "any_provisional",
            "FactStoreEvent",
        ),
    ),
}


def get_state_reader_registration(state_name: str) -> StateReaderRegistration:
    return STATE_READER_REGISTRY[state_name]
