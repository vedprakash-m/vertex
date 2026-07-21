from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, cast

import typer
import yaml
from src.commands.confirm_stages.deserialization import deserialize_items as _deserialize_items
from src.commands.confirm_stages.deserialization import deserialize_kusto_sections as _deserialize_kusto_sections
from src.commands.confirm_stages.deserialization import parse_datetime_required as _parse_datetime_required
from src.commands.confirm_stages.draft_loaders import load_confirm_overrides as _load_confirm_overrides
from src.commands.confirm_stages.draft_loaders import load_confirm_review_status as _load_confirm_review_status
from src.commands.confirm_stages.draft_loaders import load_current_draft_manifest_id as _load_current_draft_manifest_id
from src.commands.confirm_stages.draft_loaders import load_draft_ai_safety_metadata as _load_draft_ai_safety_metadata
from src.commands.confirm_stages.draft_loaders import load_draft_readiness_metadata as _load_draft_readiness_metadata
from src.commands.confirm_stages.draft_loaders import load_draft_state as _load_draft_state
from src.commands.confirm_stages.draft_loaders import load_readiness_gate_settings as _load_readiness_gate_settings
from src.commands.confirm_stages.validation import build_stale_proposed_decision_warnings as _build_stale_proposed_decision_warnings
from src.commands.confirm_stages.validation import validate_decision_strip_ack as _validate_decision_strip_ack_impl
from src.commands.confirm_stages.validation import evaluate_stale_approvals as _evaluate_stale_approvals
from src.commands.confirm_stages.validation import read_confirming_author as _read_confirming_author
from src.commands.confirm_stages.claim_resolution import resolve_confirm_claim_extraction as _resolve_confirm_claim_extraction
from src.commands.confirm_stages.claim_resolution import prepare_confirm_claim_extraction_for_v2 as _prepare_confirm_claim_extraction_for_v2
from src.commands.confirm_stages.claim_resolution import evaluate_claim_extraction_calibration_gate as _evaluate_claim_extraction_calibration_gate
from src.commands.confirm_stages.claim_resolution import record_confirmed_claims_for_v2 as _record_confirmed_claims_for_v2_impl
from src.commands.confirm_stages.baseline_followthrough import apply_baseline_followthrough as _apply_baseline_followthrough
from src.commands.confirm_stages.archive_transaction import execute_archive_transaction as _execute_archive_transaction_impl
from src.commands.confirm_stages.signal_metrics import compute_provenance_confidence as _compute_provenance_confidence
from src.commands.confirm_stages.signal_metrics import compute_source_health_pct as _compute_source_health_pct
from src.commands.confirm_stages.optimization_proposals import write_optimization_proposals as _write_optimization_proposals
from src.commands.confirm_stages.post_confirm_artifacts import confirm_additional_failures as _confirm_additional_failures_impl
from src.commands.confirm_stages.post_confirm_artifacts import record_edit_patterns_for_v2 as _record_edit_patterns_for_v2_impl
from src.commands.confirm_stages.post_confirm_artifacts import record_learning_distillation as _record_learning_distillation_impl
from src.commands.confirm_stages.post_confirm_artifacts import record_ncfl_proposals as _record_ncfl_proposals_impl
from src.commands.confirm_stages.post_confirm_artifacts import record_review_tracking as _record_review_tracking_impl
from src.commands.confirm_stages.post_confirm_artifacts import record_workstream_associations as _record_workstream_associations_impl
from src.commands.confirm_stages.post_confirm_support import build_confirmed_eml_bytes as _build_confirmed_eml_bytes_impl
from src.commands.confirm_stages.post_confirm_support import load_draft_continuation_contract_path as _load_draft_continuation_contract_path_impl
from src.commands.confirm_stages.post_confirm_support import next_issue_narrative_templates as _next_issue_narrative_templates_impl
from src.commands.confirm_stages.post_confirm_support import next_issue_number as _next_issue_number_impl
from src.commands.confirm_stages.post_confirm_support import write_context_snapshot_for_issue as _write_context_snapshot_for_issue_impl
from src.commands.confirm_stages.weekly_summary_card import post_confirm_weekly_summary_card as _post_confirm_weekly_summary_card
from src.commands.confirm_stages.weekly_summary_card import validate_weekly_summary_card_request as _validate_weekly_summary_card_request
from src.commands.doctor_checks.semantic_index_checks import semantic_index_enabled as _semantic_index_enabled
from src.commands.report_email import _build_email_preheader, _build_email_subject
from src.commands.report_narratives import _active_workstream_blurbs, _workstream_narrative_warnings
from src.core.analytics_store import load_contradiction_state
from src.core.archive_store import archive_integrity_waived, ArchiveEntry, ConfirmedIssueArchivePaths, find_archive_index_inconsistencies, find_latest_confirmed_entry, read_archive_index, read_vitality_history
from src.core.archive_store import verify_archive_integrity
from src.core.archive_store import write_confirmed_issue
from src.core.chart_cache_store import load_chart_cache
from src.core.context_gap_store import append_context_gap
from src.core.attribution_engine import build_inline_citations
from src.core.analytics_store import record_gate_failures_from_report
from src.core.ban_list_validator import find_ban_list_violations
from src.core.claim_tracker import ClaimExtractionResult
from src.core.alerts import append_or_suppress_alert, surface_alert_banner as _surface_alert_banner
from src.core.config_loader import EDITIONS_ROOT as _CONFIRM_EDITIONS_ROOT, EditorialRules, PROGRAMS_ROOT as _CONFIRM_PROGRAMS_ROOT, REPORTS_ROOT, load_bundle
from src.core.continuation_contract import build_bridge_section_roster_ids, build_continuation_contract
from src.core.delta_engine import build_deltas
from src.core.edition_resolver import get_program_output_dir, resolve_edition, resolve_edition_paths as _resolve_edition_paths
from src.core.evidence_engine import build_evidence
from src.core.exceptions import ConfirmError, StateError as _AlertStateError
from src.core.forecast_engine import build_forecast_assessment
from src.core.freshness_engine import build_freshness_report
from src.core.hygiene_engine import evaluate_hygiene
from src.core.html_renderer import HTMLRenderer, RenderContext
from src.core.manifest_writer import build_run_manifest
from src.core.narrative_store import build_workstream_narrative_history
from src.core.notification_state_store import load_latest_notification_state
from src.core.models import Confidence, EditionType, ProgramContext, ReportData, ReviewState
from src.core.models import ReviewStatus, RiskLevel, RunManifest, Snapshot, WorkItem
from src.core.narrative_store import find_unresolved_scaffold_placeholders, get_narratives_dir, load_narratives, reset_narratives_for_next_issue, strip_scaffold_comments
from src.core.overrides_store import OverridesDocument, get_overrides_path, reset_overrides_for_next_issue
from src.core.quality_gates import QualityGateReport, combine_gate_reports, evaluate_bridge_gates, evaluate_continuity_gates, evaluate_context_integrity_gates, evaluate_phase_1a_gates
from src.core.quality_gates import evaluate_phase_1b_gates, evaluate_phase_1c_gates, evaluate_contradiction_gate, evaluate_program_fact_drift_from_draft, evaluate_readiness_gates, evaluate_source_health_gates
from src.core.quality_gates import evaluate_workiq_confirm_gates
from src.core.quality_gates.state_authority import StateAuthorityAmbiguousError, assert_state_authority_or_raise
from src.core.review_status_store import get_review_status_path, reset_review_status
from src.core.section_proposal_store import load_proposals
from src.core.section_proposal_store import load_stale_claim_ids
from src.core.signal_review import signal_is_approved_for_evidence
from src.core.signal_classification import signal_class as _get_signal_class
from src.core.risk_register_engine import upsert_risk_from_signal as _upsert_risk_from_signal
from src.core.risk_register_engine import preview_risk_upserts_from_signals as _preview_risk_upserts_from_signals
from src.core.risk_register_engine import compute_risk_delta_preview_hash as _compute_risk_delta_preview_hash
from src.core.risk_register_engine import RiskDeltaPreviewEntry
from src.core.gather_run_manifest import validate_pinned_gather_run as _validate_pinned_gather_run
from src.core.semantic_index import mark_semantic_index_dirty, update_archive_semantic_index_for_issue
from src.core.slice_contract_loader import load_slice_contract_for_edition
from src.core.source_health import source_health_function_name_for_edition
from src.core.source_waiver_store import load_source_waivers
from src.core.snapshot_store import ARCHIVE_ROOT, read_snapshot, write_confirmed
from src.core.store_factory import build_signal_store_for_program_id
from src.core.teams_renderer import TeamsRenderer
from src.core.trusted_baseline_store import load_trusted_baseline
from src.core.trusted_baseline_store import load_trusted_baseline_issue
from src.core.verbosity_enforcer import enforce_verbosity
from src.core.view_models import EditionMeta, WorkstreamData
from src.core.gather_state_store import load_gather_state
from src.core.models_v2 import SectionRevisionStatus, Signal, SignalClass
from src.commands.report import _ado_item_base_url, _build_auto_suggested_top_items, _build_continuity_deltas
from src.commands.report import _build_health_summary, _build_item_urls, _build_model_program_context, _build_exec_summary_text
from src.commands.report import _build_scorecard_data, _build_scorecard_packets, _build_snapshot, _build_top_items, _compute_read_time_minutes
from src.commands.report import _build_newsletter_narrative_covered_item_ids, _build_newsletter_scoped_items
from src.commands.report import _build_v2_vitality_snapshot, _compute_healthy_streak
from src.commands.report import _active_chapter_notes, _build_chapter_templates, _build_continuity_render_data
from src.commands.report import _build_workstream_data, _build_workstream_templates, _count_new_high_dimensions, _decision_strip_ack_required
from src.commands.report import _build_continuity_exec_summary_template, _build_exec_summary_template, _format_edition_title, _format_prior_date_label, _group_scorecard_deltas, _resolve_forwarding_context, _subject_signal
from src.commands.report import _has_usable_continuity_baseline, _load_previous_snapshot, _read_git_sha
from src.commands.report_lookback import build_lookback_ban_list_inputs
from src.commands.report import _is_continuity_layout, _visible_continuity_chapters, _visible_detail_section_ids
from src.commands.report import _write_output_json, _write_output_text
from src.core.vitality_reporting import build_vitality_archive_entry, build_vitality_section, vitality_settings_from_program


@dataclass(frozen=True, slots=True)
class ConfirmResult:
    issue_number: int
    next_issue_number: int
    exit_code: int
    snapshot: Snapshot
    manifest: RunManifest
    archive_paths: ConfirmedIssueArchivePaths | None
    failures: tuple[str, ...]
    warnings: tuple[str, ...]
    review_tracking_path: Path | None = None
    review_tracking_summary: str | None = None
    learning_md_path: Path | None = None
    learning_json_path: Path | None = None
    learning_summary: str | None = None
    weekly_summary_card_path: Path | None = None
    posted_weekly_summary_card: bool = False
    workstream_association_log_path: Path | None = None
    # D-17/ARM-GATHER-11 AG-6.3: populated on a failure-free --dry-run only --
    # what upsert_risk_from_signal would do for each approved RISK-class
    # signal, computed without mutating risk_register.yaml. Empty on a real
    # confirm (which performs the upserts directly) and on any confirm that
    # returned failures before reaching this computation.
    risk_delta_preview: tuple[RiskDeltaPreviewEntry, ...] = ()
    risk_delta_preview_hash: str | None = None


def _validate_decision_strip_ack(overrides_document: OverridesDocument) -> tuple[str, ...]:
    return _validate_decision_strip_ack_impl(overrides_document)


def _confirm_additional_failures(
    *,
    overrides_document: OverridesDocument,
    report: ReportData,
    workstream_data: tuple[WorkstreamData, ...],
    manifest: RunManifest,
    ack_forecast: bool,
    unresolved_scaffold_placeholders: dict[str, tuple[str, ...]],
) -> tuple[str, ...]:
    return _confirm_additional_failures_impl(
        overrides_document=overrides_document,
        report=report,
        workstream_data=workstream_data,
        manifest=manifest,
        ack_forecast=ack_forecast,
        unresolved_scaffold_placeholders=unresolved_scaffold_placeholders,
        build_top_items_fn=_build_top_items,
        count_new_high_dimensions_fn=_count_new_high_dimensions,
        decision_strip_ack_required_fn=_decision_strip_ack_required,
        validate_decision_strip_ack_fn=_validate_decision_strip_ack,
        risk_level_high=RiskLevel.HIGH,
        risk_level_blocked=RiskLevel.BLOCKED,
    )


def confirm_command(
    edition: str = typer.Option(..., "--edition", help="Edition name, e.g. myprogram_weekly."),
    issue: int | None = typer.Option(None, "--issue", help="Issue number to confirm. Defaults to next issue after archive index."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and show what would be confirmed without archive writes."),
    force: bool = typer.Option(False, "--force", help="Override forceable gates such as freshness while keeping hard blocks enforced."),
    ack_forecast: bool = typer.Option(False, "--ack-forecast", help="Acknowledge an enabled forecast before confirming."),
    ack_stale_approval: bool = typer.Option(False, "--ack-stale-approval", help="Acknowledge a stale approval after reviewing the updated ADO data."),
    untrusted: bool = typer.Option(False, "--untrusted", help="Archive the issue without advancing the trusted continuity baseline."),
    reason: str | None = typer.Option(None, "--reason", help="Reason for confirming with --untrusted."),
    legacy_regex_extractor: bool = typer.Option(
        False,
        "--legacy-regex-extractor",
        help="Force the legacy regex claim extractor instead of the AI claim extractor.",
    ),
    post_weekly_summary_card: bool = typer.Option(
        False,
        "--post-weekly-summary-card",
        help="After a successful confirm, write and post the weekly summary Adaptive Card to Teams.",
    ),
    skip_ncfl: bool = typer.Option(
        False,
        "--skip-ncfl",
        help="Skip best-effort NCFL proposal extraction after confirm.",
    ),
) -> None:
    if untrusted and (reason is None or not reason.strip()):
        raise typer.BadParameter("--reason is required when --untrusted is used.")
    if not untrusted and reason is not None:
        raise typer.BadParameter("--reason is only supported with --untrusted.")

    try:
        _ed_paths = _resolve_edition_paths(edition, editions_root=_CONFIRM_EDITIONS_ROOT, programs_root=_CONFIRM_PROGRAMS_ROOT)
        if _ed_paths is not None:
            _confirm_banner = _surface_alert_banner(_ed_paths.program_id, programs_root=_CONFIRM_PROGRAMS_ROOT)
            if _confirm_banner is not None:
                typer.echo(_confirm_banner, err=True)
    except (OSError, _AlertStateError, ValueError):
        pass

    def run_confirm(*, ack_forecast_flag: bool, ack_stale_approval_flag: bool) -> ConfirmResult:
        try:
            return confirm_issue(
                edition_name=edition,
                issue_number=resolved_issue,
                dry_run=dry_run,
                force=force,
                ack_forecast=ack_forecast_flag,
                ack_stale_approval=ack_stale_approval_flag,
                untrusted=untrusted,
                untrusted_reason=reason,
                legacy_regex_extractor=legacy_regex_extractor,
                post_weekly_summary_card=post_weekly_summary_card,
                skip_ncfl=skip_ncfl,
            )
        except ConfirmError as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=2) from exc

    archive_index = read_archive_index(edition, archive_root=ARCHIVE_ROOT)
    # WS-1 §9a P1: archive integrity pre-flight — blocks dangling refs before writing a new confirmed entry.
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
                + "Bypass (not recommended): VERTEX_ARCHIVE_INTEGRITY_WAIVER=1 vertex confirm ...",
                err=True,
            )
            raise typer.Exit(code=3)
    resolved_issue = issue if issue is not None else _next_issue_number_impl(archive_index)
    if any(entry.issue_number == resolved_issue and entry.kind == "confirmed" for entry in archive_index.issues):
        typer.echo(
            f"Issue {resolved_issue:03d} is already confirmed in the archive index for {edition}; no new manifest was written."
        )
        raise typer.Exit(code=1)
    if issue is None:
        latest_confirmed_entry = find_latest_confirmed_entry(archive_index)
        last_confirmed = latest_confirmed_entry.issue_number if latest_confirmed_entry is not None else 0
        if not typer.confirm(
            f"About to confirm Issue {resolved_issue:03d}. Archive shows last confirmed was Issue {last_confirmed:03d}. Continue?",
            default=True,
        ):
            raise typer.Exit(code=1)

    resolved_ack_forecast = ack_forecast
    resolved_ack_stale_approval = ack_stale_approval

    result = run_confirm(
        ack_forecast_flag=resolved_ack_forecast,
        ack_stale_approval_flag=resolved_ack_stale_approval,
    )

    if (
        not dry_run
        and
        not resolved_ack_forecast
        and any(failure.startswith("BLOCKED: Forecast present") for failure in result.failures)
        and typer.confirm(
            f"{result.manifest.metadata.get('forecast_summary')} Confidence: {result.manifest.metadata.get('forecast_confidence', 'unknown')}. Confirm anyway?",
            default=False,
        )
    ):
        resolved_ack_forecast = True
        result = run_confirm(
            ack_forecast_flag=True,
            ack_stale_approval_flag=resolved_ack_stale_approval,
        )

    if (
        not dry_run
        and not resolved_ack_stale_approval
        and any(failure.startswith("BLOCKED: Stale approval + data changed") for failure in result.failures)
        and typer.confirm(
            "ADO data changed after an approval recorded against an older manifest. Confirm anyway and record the override?",
            default=False,
        )
    ):
        resolved_ack_stale_approval = True
        result = run_confirm(
            ack_forecast_flag=resolved_ack_forecast,
            ack_stale_approval_flag=True,
        )

    if result.failures:
        typer.echo(f"Confirm blocked for issue {result.issue_number:03d}.")
        for failure in result.failures:
            typer.echo(f"- {failure}")
        raise typer.Exit(code=result.exit_code)

    if dry_run:
        typer.echo(f"Confirm dry-run passed for issue {result.issue_number:03d}.")
        typer.echo("Would write confirmed snapshot, archive HTML/Markdown/manifest, and reset active author state.")
        if result.risk_delta_preview:
            typer.echo(f"Risk-register delta preview ({len(result.risk_delta_preview)} approved risk signal(s)):")
            for entry in result.risk_delta_preview:
                typer.echo(f"- {entry.action}: {entry.risk_id} ({entry.title})")
            typer.echo(f"Preview hash: {result.risk_delta_preview_hash}")
        if result.warnings:
            typer.echo(f"Warnings: {len(result.warnings)}")
            for warning in result.warnings:
                typer.echo(f"- {warning}")
        raise typer.Exit(code=result.exit_code)

    typer.echo(f"Confirmed issue {result.issue_number:03d} for {edition}.")
    if result.archive_paths is not None:
        typer.echo(f"Snapshot: {result.archive_paths.snapshot_path}")
        if result.archive_paths.eml_path is not None:
            typer.echo(f"EML: {result.archive_paths.eml_path}")
        typer.echo(f"HTML: {result.archive_paths.html_path}")
        typer.echo(f"Markdown: {result.archive_paths.md_path}")
        typer.echo(f"Manifest: {result.archive_paths.manifest_path}")
    if result.weekly_summary_card_path is not None:
        typer.echo(f"Weekly Summary Card: {result.weekly_summary_card_path}")
        if result.posted_weekly_summary_card:
            typer.echo("Weekly summary card posted to Teams.")
    typer.echo(f"Reset active state for issue {result.next_issue_number:03d}.")
    if result.review_tracking_summary is not None:
        typer.echo(result.review_tracking_summary)
        if result.review_tracking_path is not None:
            typer.echo(f"AI Review Tracking: {result.review_tracking_path}")
    if result.learning_summary is not None:
        typer.echo(result.learning_summary)
        if result.learning_md_path is not None:
            typer.echo(f"AI Learning Notes: {result.learning_md_path}")
        if result.learning_json_path is not None:
            typer.echo(f"AI Learning Data: {result.learning_json_path}")
    if result.warnings:
        typer.echo(f"Warnings: {len(result.warnings)}")
        for warning in result.warnings:
            typer.echo(f"- {warning}")
    raise typer.Exit(code=result.exit_code)


def _assert_state_authority_for_confirm(program_id: str, *, programs_root: Path) -> None:
    """ADF-W1.9 / QG-37 (Section 12.1, 8.12.3): the mutation-blocking half,
    activated 2026-07-13 after live operator reconciliation cleared the
    real ambiguity it originally found (see specs/arch-data-fix.md
    ADF-W1.9). Only called for a real (non-dry-run) confirm, since a
    dry-run never mutates. A separate, tiny function so it is unit-testable
    without needing ``confirm_issue``'s full pipeline (this repo's
    acme-fixture test workspace is unrelated-ly broken in this environment
    -- "program.yaml absent after copy" -- so most of `confirm_issue`'s own
    tests are skipped here; this function's own test does not depend on it)."""
    try:
        assert_state_authority_or_raise(program_id, programs_root=programs_root)
    except StateAuthorityAmbiguousError as exc:
        raise ConfirmError(str(exc)) from exc


def _emit_source_health_alerts_best_effort(
    source_health_qg: QualityGateReport, *, program_id: str, edition_name: str, programs_root: Path
) -> None:
    """ADF-W5.8 (Section 8.2.5's "required-source stale or zero-yield"
    category). A separate, tiny function so it is unit-testable without
    ``confirm_issue``'s full pipeline, same rationale as
    ``_assert_state_authority_for_confirm`` above."""
    for result in source_health_qg.results:
        if result.passed:
            continue
        try:
            append_or_suppress_alert(
                program_id=program_id, category="required_source_unhealthy",
                entity_type="edition", entity_id=f"{edition_name}:{result.gate_id}", severity="warn",
                message=result.message, next_command=f"vertex gather --edition {edition_name}",
                programs_root=programs_root,
            )
        except (OSError, _AlertStateError):
            pass


def confirm_issue(
    edition_name: str,
    issue_number: int,
    dry_run: bool = False,
    force: bool = False,
    ack_forecast: bool = False,
    ack_stale_approval: bool = False,
    untrusted: bool = False,
    untrusted_reason: str | None = None,
    legacy_regex_extractor: bool = False,
    post_weekly_summary_card: bool = False,
    skip_ncfl: bool = False,
    reports_root: Path | None = None,
    archive_root: Path | None = None,
    output_root: Path | None = None,
    programs_root: Path | None = None,
) -> ConfirmResult:
    resolved_reports_root = reports_root or REPORTS_ROOT
    resolved_archive_root = archive_root or ARCHIVE_ROOT
    programs_root = programs_root or resolved_reports_root.parent / "programs"
    editions_root = programs_root.parent / "editions"

    bundle = load_bundle(
        edition_name,
        reports_root=resolved_reports_root,
        programs_root=programs_root,
    )
    if not dry_run:
        _assert_state_authority_for_confirm(bundle.program.id, programs_root=programs_root)
    draft_state = _load_draft_state(edition_name, issue_number, programs_root=programs_root)
    if post_weekly_summary_card:
        _validate_weekly_summary_card_request(bundle=bundle, draft_state=draft_state)
    overrides_document = _load_confirm_overrides(edition_name, issue_number, bundle, resolved_reports_root)
    artifacts = _build_confirm_artifacts(
        edition_name=edition_name,
        issue_number=issue_number,
        bundle=bundle,
        overrides_document=overrides_document,
        draft_state=draft_state,
        reports_root=resolved_reports_root,
        archive_root=resolved_archive_root,
        programs_root=programs_root,
    )

    try:
        # Claim extraction enriches the archive, but confirm must still proceed when the
        # optional AI path is unavailable or produces invalid output.
        extraction_result, extraction_mode, claim_extraction_warnings, calibration_record = _prepare_confirm_claim_extraction_for_v2(
            edition_name=edition_name,
            issue_number=issue_number,
            confirmed_at=artifacts[6].ado_data_as_of,
            reports_root=resolved_reports_root,
            items=artifacts[6].items,
            legacy_regex_extractor=legacy_regex_extractor,
        )
    except Exception as exc:
        extraction_result = None
        extraction_mode = "regex"
        calibration_record = None
        claim_extraction_warnings = (f"Claim tracker skipped: {exc}",)

    qg_report = combine_gate_reports(
        artifacts[0],
        _evaluate_claim_extraction_calibration_gate(calibration_record),
    )

    if not dry_run and qg_report.failing_results:
        _gate_resolved_v2 = resolve_edition(
            edition_name,
            programs_root=resolved_reports_root.parent / "programs",
        )
        if _gate_resolved_v2 is not None:
            record_gate_failures_from_report(
                _gate_resolved_v2.program.id,
                gate_report=qg_report,
                edition_id=edition_name,
                programs_root=resolved_reports_root.parent / "programs",
            )

    advisory_results = tuple(
        result
        for result in qg_report.failing_results
        if result.exit_code <= 1
    )
    blocking_results = tuple(
        result
        for result in qg_report.failing_results
        if result.exit_code > 1 and not (force and result.forceable)
    )
    forced_results = tuple(
        result
        for result in qg_report.failing_results
        if result.exit_code > 1 and force and result.forceable
    )
    manifest = artifacts[3]
    current_draft_manifest_id = _load_current_draft_manifest_id(edition_name, issue_number, programs_root=programs_root)
    evidence_window_start = artifacts[6].ado_data_as_of - __import__("datetime", fromlist=["timedelta"]).timedelta(days=bundle.config.ado.date_window_days)
    evidence_by_item = {
        item.id: build_evidence(item, evidence_window_start, artifacts[6].ado_data_as_of)
        for item in artifacts[6].items
    }
    stale_warnings, stale_failures, stale_override_applied = _evaluate_stale_approvals(
        review_status=artifacts[6].review_status,
        report=artifacts[6],
        workstream_data=artifacts[7],
        evidence_by_item=evidence_by_item,
        current_manifest_id=current_draft_manifest_id,
        ack_stale_approval=ack_stale_approval,
    )
    if stale_override_applied:
        manifest = replace(
            manifest,
            metadata={
                **manifest.metadata,
                "overrode_stale_approval": True,
                "override_method": "interactive_confirm",
            },
        )
    extra_failures = _confirm_additional_failures(
        overrides_document=overrides_document,
        report=artifacts[6],
        workstream_data=artifacts[7],
        manifest=manifest,
        ack_forecast=ack_forecast,
        unresolved_scaffold_placeholders=find_unresolved_scaffold_placeholders(
            edition_name,
            issue_number,
            reports_root=resolved_reports_root,
        ),
    )
    decision_warnings = _build_stale_proposed_decision_warnings(
        edition_name=edition_name,
        as_of=artifacts[6].ado_data_as_of.date(),
        reports_root=resolved_reports_root,
    )
    # D-17/ARM-GATHER-11 (AG-6.2): reject a pinned gather run that is invalid,
    # stale, or PARTIAL scope -- before either a dry-run preview or a real
    # archive transaction proceeds. Applies to both paths identically so a
    # dry-run pass is a trustworthy predictor of the real confirm (AG-6.5).
    _lineage_resolved_v2 = resolve_edition(
        edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    _lineage_program_id = _lineage_resolved_v2.program.id if _lineage_resolved_v2 is not None else None
    gather_run_failures = (
        _validate_pinned_gather_run(
            _lineage_program_id,
            gather_run_id=draft_state.get("gather_run_id"),
            gather_run_hash=draft_state.get("gather_run_hash"),
            programs_root=programs_root,
        )
        if _lineage_program_id is not None
        else ()
    )
    failures = tuple(result.message for result in blocking_results) + extra_failures + stale_failures + gather_run_failures
    warnings = (
        artifacts[1]
        + claim_extraction_warnings
        + tuple(result.message for result in advisory_results)
        + tuple(f"Forced past {result.gate_id}: {result.message}" for result in forced_results)
        + stale_warnings
        + decision_warnings
    )
    if failures:
        return ConfirmResult(
            issue_number=issue_number,
            next_issue_number=issue_number + 1,
            exit_code=max((result.exit_code for result in blocking_results), default=3),
            snapshot=artifacts[2],
            manifest=manifest,
            archive_paths=None,
            failures=failures,
            warnings=warnings,
        )

    if dry_run:
        # D-17/ARM-GATHER-11 (AG-6.3): compute a hash-bound risk-register
        # delta preview -- what upsert_risk_from_signal would do for each
        # currently-approved RISK-class signal -- without mutating
        # risk_register.yaml. Reuses the exact matching decision
        # (_decide_risk_upsert) the real archive transaction's upsert loop
        # calls, so it can never drift from what a real confirm would do.
        risk_delta_preview: tuple[RiskDeltaPreviewEntry, ...] = ()
        risk_delta_preview_hash: str | None = None
        if _lineage_program_id is not None:
            _preview_data_as_of = _parse_datetime_required(draft_state["ado_data_as_of"])
            _preview_evidence_window_start = _preview_data_as_of - __import__("datetime", fromlist=["timedelta"]).timedelta(
                days=bundle.config.ado.date_window_days
            )
            _preview_signal_store = build_signal_store_for_program_id(_lineage_program_id, programs_root=programs_root)
            _preview_journal_signals = _preview_signal_store.read(_lineage_program_id, end=_preview_data_as_of)
            _preview_review_states = _preview_signal_store.read_reviews(_lineage_program_id)
            _preview_risk_signals = tuple(
                (signal.id, signal.text or "", tuple(signal.entity_refs or ()), getattr(signal, "workstream_id", None))
                for signal in _preview_journal_signals
                if signal.timestamp >= _preview_evidence_window_start
                and signal_is_approved_for_evidence(signal, _preview_review_states)
                and _get_signal_class(signal) == SignalClass.RISK
            )
            risk_delta_preview = _preview_risk_upserts_from_signals(
                _lineage_program_id,
                _preview_risk_signals,
                programs_root=programs_root,
            )
            risk_delta_preview_hash = _compute_risk_delta_preview_hash(risk_delta_preview)
        return ConfirmResult(
            issue_number=issue_number,
            next_issue_number=issue_number + 1,
            exit_code=0,
            snapshot=artifacts[2],
            manifest=manifest,
            archive_paths=None,
            failures=(),
            warnings=warnings,
            risk_delta_preview=risk_delta_preview,
            risk_delta_preview_hash=risk_delta_preview_hash,
        )

    review_status_path = get_review_status_path(edition_name, reports_root=resolved_reports_root)
    narrative_dir = get_narratives_dir(edition_name, issue_number, reports_root=resolved_reports_root)

    # Reuse the resolution already performed above for gather-run-lineage
    # validation instead of resolving the edition a second time.
    resolved_v2 = _lineage_resolved_v2
    program_id = _lineage_program_id
    resolved_workstreams = resolved_v2.workstreams if resolved_v2 is not None else ()
    resolved_scorecards = resolved_v2.scorecards if resolved_v2 is not None else ()
    items = _deserialize_items(tuple(draft_state.get("items", [])))
    kusto_sections = _deserialize_kusto_sections(tuple(draft_state.get("kusto_sections", [])))
    data_as_of = _parse_datetime_required(draft_state["ado_data_as_of"])
    trusted_baseline_issue_number = load_trusted_baseline_issue(
        edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    previous_snapshot, previous_issue_number = _load_previous_snapshot(
        edition_name=edition_name,
        issue_number=issue_number,
        archive_root=resolved_archive_root,
        trusted_issue_number=trusted_baseline_issue_number,
    )
    evidence_window_start = data_as_of - __import__("datetime", fromlist=["timedelta"]).timedelta(days=bundle.config.ado.date_window_days)
    journal_signals: tuple[Signal, ...] = ()
    approved_signals: tuple[Signal, ...] = ()
    if program_id is not None:
        signal_store = build_signal_store_for_program_id(program_id, programs_root=programs_root)
        journal_signals = signal_store.read(
            program_id,
            end=data_as_of,
        )
        review_states = signal_store.read_reviews(program_id)
        approved_signals = tuple(
            signal
            for signal in journal_signals
            if signal.timestamp >= evidence_window_start
            and signal_is_approved_for_evidence(signal, review_states)
        )
    evidence_by_item = {
        item.id: build_evidence(item, evidence_window_start, data_as_of)
        for item in items
    }
    continuity_snapshot = previous_snapshot if _has_usable_continuity_baseline(previous_snapshot) else None
    continuity_previous_issue_number = previous_issue_number if continuity_snapshot is not None else None
    deltas = _build_continuity_deltas(
        current_items=items,
        previous_snapshot=continuity_snapshot,
        issue_number=issue_number,
        previous_issue_number=continuity_previous_issue_number,
        evidence_by_item=evidence_by_item,
    )
    scorecard_packets = _build_scorecard_packets(bundle, items, continuity_snapshot)
    scorecards, dimension_risks, scorecard_deltas = _build_scorecard_data(
        bundle=bundle,
        items=items,
        evidence_by_item=evidence_by_item,
        scorecard_packets=scorecard_packets,
        overrides_document=overrides_document,
        edition_name=edition_name,
        reports_root=resolved_reports_root,
    )
    resolved_edition_type = EditionType.from_string(str(draft_state.get("edition_type", bundle.config.edition.type)))
    default_exec_summary = _build_exec_summary_text(
        bundle,
        items,
        dimension_risks,
        deltas,
        dependency_cascades=(),
        baseline_available=continuity_snapshot is not None,
    )
    confirmed_at = datetime.now(timezone.utc)
    confirmed_by = _read_confirming_author()
    manifest = replace(
        manifest,
        metadata={
            **manifest.metadata,
            "confirmed_at": confirmed_at.isoformat(),
            "confirmed_by": confirmed_by,
            "untrusted": untrusted,
            "untrusted_reason": (untrusted_reason.strip() if untrusted_reason is not None else None),
        },
        qg_results=qg_report.qg_results,
    )
    vitality_settings = None
    vitality_archive_entry = None
    if artifacts[8] is not None:
        if resolved_v2 is not None:
            vitality_settings = vitality_settings_from_program(resolved_v2.raw_program)
            vitality_archive_entry = build_vitality_archive_entry(
                artifacts[8],
                issue_number=issue_number,
                confirmed_at=confirmed_at,
                include_per_owner=bool(vitality_settings and vitality_settings.vitality_archive_per_person),
                leakage_signal_threshold=(vitality_settings.sparse_workiq_threshold if vitality_settings is not None else 5),
            )

    # P4-3: collect chart cache entries for all chart-image kusto sections
    chart_cache_entries: dict[str, dict[str, Any]] | None = None
    if program_id is not None and kusto_sections:
        chart_cache_entries = {}
        for ks in kusto_sections:
            if getattr(ks, "render_mode", None) == "chart_image" and ks.query_id:
                entry = load_chart_cache(program_id, ks.query_id, programs_root=programs_root)
                if entry is not None:
                    chart_cache_entries[ks.query_id] = {
                        "program_id": entry.program_id,
                        "query_id": entry.query_id,
                        "captured_at": entry.captured_at.isoformat(),
                        "chart_config_hash": entry.chart_config_hash,
                        "row_count": entry.row_count,
                        "schema_version": entry.schema_version,
                    }

    # P4-3: enrich manifest with chart index entries per spec R3-054
    if chart_cache_entries:
        chart_index_entries = [
            {
                "query_id": query_id,
                "chart_config_hash": entry["chart_config_hash"],
                "captured_at": entry["captured_at"],
                "row_count": entry["row_count"],
                "png_size_bytes": next(
                    (ks.chart_png_size_bytes for ks in kusto_sections if ks.query_id == query_id and ks.render_mode == "chart_image"),
                    0,
                ),
            }
            for query_id, entry in chart_cache_entries.items()
        ]
        manifest = replace(
            manifest,
            metadata={
                **manifest.metadata,
                "chart_index": chart_index_entries,
            },
        )

    archive_transaction = _execute_archive_transaction_impl(
        edition_name=edition_name,
        issue_number=issue_number,
        confirmed_at=confirmed_at,
        warnings=warnings,
        archive_root=resolved_archive_root,
        programs_root=programs_root,
        reports_root=resolved_reports_root,
        approved_signals=approved_signals,
        artifacts=artifacts,
        bundle=bundle,
        manifest=manifest,
        overrides_document=overrides_document,
        vitality_archive_entry=vitality_archive_entry,
        chart_cache_entries=chart_cache_entries,
        review_status_path=review_status_path,
        narrative_dir=narrative_dir,
        program_id=program_id,
        resolved_v2=resolved_v2,
        legacy_regex_extractor=legacy_regex_extractor,
        extraction_result=extraction_result,
        extraction_mode=extraction_mode,
        build_confirmed_eml_bytes_fn=_build_confirmed_eml_bytes,
        write_confirmed_fn=write_confirmed,
        write_confirmed_issue_fn=write_confirmed_issue,
        get_overrides_path_fn=get_overrides_path,
        load_draft_continuation_contract_path_fn=_load_draft_continuation_contract_path,
        write_context_snapshot_for_issue_fn=_write_context_snapshot_for_issue,
        record_confirmed_claims_for_v2_fn=_record_confirmed_claims_for_v2,
        semantic_index_enabled_fn=_semantic_index_enabled,
        update_archive_semantic_index_for_issue_fn=update_archive_semantic_index_for_issue,
        mark_semantic_index_dirty_fn=mark_semantic_index_dirty,
        write_optimization_proposals_fn=_write_optimization_proposals,
        load_gather_state_fn=load_gather_state,
        compute_source_health_pct_fn=_compute_source_health_pct,
        compute_provenance_confidence_fn=_compute_provenance_confidence,
        get_signal_class_fn=_get_signal_class,
        upsert_risk_from_signal_fn=_upsert_risk_from_signal,
        confirmed_by=confirmed_by or "",
    )
    archive_paths = archive_transaction.archive_paths
    warnings = archive_transaction.warnings

    warnings = _apply_baseline_followthrough(
        edition_name=edition_name,
        issue_number=issue_number,
        confirmed_at=confirmed_at,
        confirmed_by=confirmed_by,
        warnings=warnings,
        archive_root=archive_root or ARCHIVE_ROOT,
        editions_root=editions_root,
        programs_root=programs_root,
        resolved_v2=resolved_v2,
        items=artifacts[6].items,
        untrusted=untrusted,
        untrusted_reason=untrusted_reason,
    )

    next_issue_number = issue_number + 1
    reset_overrides_for_next_issue(
        edition=edition_name,
        next_issue_number=next_issue_number,
        confirmed_dimensions=artifacts[2].scorecards,
        reports_root=resolved_reports_root,
    )
    reset_narratives_for_next_issue(
        edition=edition_name,
        next_issue_number=next_issue_number,
        templates=_next_issue_narrative_templates_impl(
            artifacts[6],
            bundle,
            is_continuity_layout_fn=_is_continuity_layout,
        ),
        reports_root=resolved_reports_root,
    )
    reset_review_status(
        edition=edition_name,
        issue_number=next_issue_number,
        section_ids=tuple(section.section_id for section in artifacts[6].review_status.sections),
        reports_root=resolved_reports_root,
    )

    review_tracking_path, review_tracking_summary, review_tracking_warning = _record_review_tracking_impl(
        edition_name=edition_name,
        issue_number=issue_number,
        draft_state=draft_state,
        report=artifacts[6],
        programs_root=programs_root,
    )
    if review_tracking_warning is not None:
        warnings = warnings + (review_tracking_warning,)
    edit_pattern_warning = _record_edit_patterns_for_v2_impl(
        edition_name=edition_name,
        issue_number=issue_number,
        draft_state=draft_state,
        report=artifacts[6],
        confirmed_at=confirmed_at,
        reports_root=resolved_reports_root,
    )
    if edit_pattern_warning is not None:
        warnings = warnings + (edit_pattern_warning,)
    learning_md_path, learning_json_path, learning_summary, learning_warning = _record_learning_distillation(
        edition_name=edition_name,
        issue_number=issue_number,
        editorial_rules=bundle.editorial_rules,
        programs_root=programs_root,
    )
    if learning_warning is not None:
        warnings = warnings + (learning_warning,)

    workstream_association_log_path, workstream_association_warning = _record_workstream_associations_impl(
        edition_name=edition_name,
        issue_number=issue_number,
        program_id=(resolved_v2.program.id if resolved_v2 is not None else None),
        programs_root=programs_root,
    )
    if workstream_association_warning is not None:
        warnings = warnings + (workstream_association_warning,)

    if not skip_ncfl and resolved_v2 is not None:
        try:
            ncfl_path, ncfl_count = _record_ncfl_proposals_impl(
                edition_name=edition_name,
                issue_number=issue_number,
                program_id=resolved_v2.program.id,
                reports_root=resolved_reports_root,
                programs_root=programs_root,
            )
            if ncfl_path is not None:
                warnings = warnings + (
                    f"NCFL proposals staged: {ncfl_count} at {ncfl_path}",
                )
        except Exception as error:
            append_context_gap(
                feature="ncfl",
                program=resolved_v2.program.id,
                lane=None,
                field="ncfl_extraction_failed",
                severity="quality_degraded",
                message=(
                    f"NCFL proposals not generated for confirmed issue {issue_number:03d}: {error}. "
                    f"Re-run `vertex context extract --edition {edition_name} --issue {issue_number}`."
                ),
                impact_estimate="medium",
                programs_root=programs_root,
            )
            warnings = warnings + (
                f"NCFL proposal extraction skipped: {error}",
            )
    elif skip_ncfl and resolved_v2 is not None:
        append_context_gap(
            feature="ncfl",
            program=resolved_v2.program.id,
            lane=None,
            field="ncfl_skipped",
            severity="quality_degraded",
            message=(
                f"NCFL proposal extraction was explicitly skipped for confirmed issue {issue_number:03d}. "
                f"Re-run `vertex context extract --edition {edition_name} --issue {issue_number}` when ready."
            ),
            impact_estimate="low",
            programs_root=programs_root,
        )

    weekly_summary_card_path: Path | None = None
    posted_weekly_summary_card = False
    if post_weekly_summary_card:
        weekly_summary_card_path, posted_weekly_summary_card, weekly_summary_warning = _post_confirm_weekly_summary_card(
            bundle=bundle,
            edition_name=edition_name,
            issue_number=issue_number,
            report=artifacts[6],
            archive_paths=archive_paths,
            webhook_url=str(bundle.config.m365.teams_incoming_webhook_url or ""),
        )
        if weekly_summary_warning is not None:
            warnings = warnings + (weekly_summary_warning,)

    return ConfirmResult(
        issue_number=issue_number,
        next_issue_number=next_issue_number,
        exit_code=0,
        snapshot=artifacts[2],
        manifest=manifest,
        archive_paths=archive_paths,
        failures=(),
        warnings=warnings,
        review_tracking_path=review_tracking_path,
        review_tracking_summary=review_tracking_summary,
        learning_md_path=learning_md_path,
        learning_json_path=learning_json_path,
        learning_summary=learning_summary,
        weekly_summary_card_path=weekly_summary_card_path,
        posted_weekly_summary_card=posted_weekly_summary_card,
        workstream_association_log_path=workstream_association_log_path,
    )


def _write_context_snapshot_for_issue(
    *,
    program_id: str,
    edition_id: str,
    issue_number: int,
    confirmed_at: datetime,
    archive_root: Path,
    programs_root: Path,
    prior_issue_entry: ArchiveEntry | None,
) -> None:
    _write_context_snapshot_for_issue_impl(
        program_id=program_id,
        edition_id=edition_id,
        issue_number=issue_number,
        confirmed_at=confirmed_at,
        archive_root=archive_root,
        programs_root=programs_root,
        prior_issue_entry=prior_issue_entry,
    )


def _build_confirmed_eml_bytes(
    bundle,
    *,
    issue_number: int,
    as_of: datetime,
    html_body: str,
    markdown_body: str,
    suggested_subject: str,
    generated_at: datetime,
) -> bytes:
    return _build_confirmed_eml_bytes_impl(
        bundle,
        issue_number=issue_number,
        as_of=as_of,
        html_body=html_body,
        markdown_body=markdown_body,
        suggested_subject=suggested_subject,
        generated_at=generated_at,
        format_edition_title_fn=_format_edition_title,
    )


def _load_draft_continuation_contract_path(
    edition_name: str,
    issue_number: int,
    *,
    programs_root: Path | None = None,
) -> Path | None:
    return _load_draft_continuation_contract_path_impl(
        edition_name,
        issue_number,
        programs_root=programs_root,  # type: ignore[arg-type]
    )


def _record_confirmed_claims_for_v2(
    *,
    edition_name: str,
    issue_number: int,
    confirmed_at: datetime,
    reports_root: Path,
    items: tuple[WorkItem, ...],
    legacy_regex_extractor: bool = False,
    extraction_result: ClaimExtractionResult | None = None,
    extraction_mode: str = "regex",
    resolve_extraction_if_missing: bool = True,
) -> tuple[str, ...]:
    return _record_confirmed_claims_for_v2_impl(
        edition_name=edition_name,
        issue_number=issue_number,
        confirmed_at=confirmed_at,
        reports_root=reports_root,
        items=items,
        legacy_regex_extractor=legacy_regex_extractor,
        extraction_result=extraction_result,
        extraction_mode=extraction_mode,
        resolve_extraction_if_missing=resolve_extraction_if_missing,
    )


def _build_confirm_artifacts(
    edition_name: str,
    issue_number: int,
    bundle,
    overrides_document: OverridesDocument,
    draft_state: dict[str, Any],
    reports_root: Path,
    archive_root: Path,
    programs_root: Path | None = None,
) -> tuple[QualityGateReport, tuple[str, ...], Snapshot, RunManifest, str, str, ReportData, tuple[WorkstreamData, ...], Any]:
    started_at = datetime.now(timezone.utc)
    programs_root = programs_root or reports_root.parent / "programs"
    editions_root = programs_root.parent / "editions"
    resolved_v2 = resolve_edition(
        edition_name,
        programs_root=programs_root,
    )
    program_id = resolved_v2.program.id if resolved_v2 is not None else None
    resolved_workstreams = resolved_v2.workstreams if resolved_v2 is not None else ()
    resolved_scorecards = resolved_v2.scorecards if resolved_v2 is not None else ()
    items = _deserialize_items(tuple(draft_state.get("items", [])))
    kusto_sections = _deserialize_kusto_sections(tuple(draft_state.get("kusto_sections", [])))
    data_as_of = _parse_datetime_required(draft_state["ado_data_as_of"])
    trusted_baseline_issue_number = load_trusted_baseline_issue(
        edition_name,
        programs_root=programs_root,
    )
    previous_snapshot, previous_issue_number = _load_previous_snapshot(
        edition_name=edition_name,
        issue_number=issue_number,
        archive_root=archive_root,
        trusted_issue_number=trusted_baseline_issue_number,
    )
    evidence_window_start = data_as_of - __import__("datetime", fromlist=["timedelta"]).timedelta(days=bundle.config.ado.date_window_days)
    journal_signals: tuple[Signal, ...] = ()
    approved_signals: tuple[Signal, ...] = ()
    if program_id is not None:
        signal_store = build_signal_store_for_program_id(program_id, programs_root=programs_root)
        journal_signals = signal_store.read(
            program_id,
            end=data_as_of,
        )
        review_states = signal_store.read_reviews(program_id)
        approved_signals = tuple(
            signal
            for signal in journal_signals
            if signal.timestamp >= evidence_window_start
            and signal_is_approved_for_evidence(signal, review_states)
        )
    evidence_by_item = {
        item.id: build_evidence(item, evidence_window_start, data_as_of)
        for item in items
    }
    continuity_snapshot = previous_snapshot if _has_usable_continuity_baseline(previous_snapshot) else None
    continuity_previous_issue_number = previous_issue_number if continuity_snapshot is not None else None
    deltas = _build_continuity_deltas(
        current_items=items,
        previous_snapshot=continuity_snapshot,
        issue_number=issue_number,
        previous_issue_number=continuity_previous_issue_number,
        evidence_by_item=evidence_by_item,
    )
    scorecard_packets = _build_scorecard_packets(bundle, items, continuity_snapshot)
    scorecards, dimension_risks, scorecard_deltas = _build_scorecard_data(
        bundle=bundle,
        items=items,
        evidence_by_item=evidence_by_item,
        scorecard_packets=scorecard_packets,
        overrides_document=overrides_document,
        edition_name=edition_name,
        reports_root=reports_root,
    )
    resolved_edition_type = EditionType.from_string(str(draft_state.get("edition_type", bundle.config.edition.type)))
    default_exec_summary = _build_exec_summary_text(
        bundle,
        items,
        dimension_risks,
        deltas,
        dependency_cascades=(),
        baseline_available=continuity_snapshot is not None,
    )
    trusted_baseline_issue_number = load_trusted_baseline_issue(
        edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    archive_index = read_archive_index(edition_name, archive_root=archive_root)
    continuity_chapters = _visible_continuity_chapters(bundle, resolved_edition_type)
    auto_suggestions = _build_auto_suggested_top_items(scorecard_deltas, scorecard_packets)
    narrative_templates = {
        "exec_summary.md": (
            _build_continuity_exec_summary_template(
                issue_number=issue_number,
                program_objective=(bundle.program_context.objective if bundle.program_context is not None else None),
                auto_suggestions=auto_suggestions,
                scorecard_deltas=scorecard_deltas,
                dimension_risks=dimension_risks,
            )
            if _is_continuity_layout(bundle)
            else _build_exec_summary_template(issue_number, layout_mode=bundle.config.layout_mode)
        ),
        **(
            _build_chapter_templates(issue_number, continuity_chapters)
            if _is_continuity_layout(bundle)
            else _build_workstream_templates(
                issue_number=issue_number,
                bundle=bundle,
                items=items,
                scorecards=scorecards,
                scorecard_packets=scorecard_packets,
                overrides_document=overrides_document,
            )
        ),
    }
    loaded_narratives = load_narratives(edition_name, issue_number, reports_root=reports_root)
    exec_summary_text = loaded_narratives.get("exec_summary.md", "").strip() or default_exec_summary
    top_items = _build_top_items(overrides_document, scorecards)
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
            reports_root=reports_root,
            archive_root=archive_root,
            current_section_ids=raw_visible_section_ids,
            loaded_narratives=loaded_narratives,
            removed_section_ids=set(overrides_document.removed_sections),
        )
        section_roster_current_ids = tuple(sorted(("exec_summary", *diagnostic_section_ids)))
        workstream_blurbs = _active_workstream_blurbs(loaded_narratives, visible_section_ids)
    for filename, template in narrative_templates.items():
        if filename == "exec_summary.md":
            continue
        if filename.startswith("chapter_"):
            section_id = filename.removeprefix("chapter_").removesuffix(".md")
        else:
            section_id = filename.removeprefix("ws_").removesuffix(".md")
        workstream_blurbs.setdefault(section_id, strip_scaffold_comments(template))
    previous_notification_state = load_latest_notification_state(
        edition=edition_name,
        programs_root=programs_root,
    )
    workstream_narrative_history = (
        {}
        if _is_continuity_layout(bundle)
        else build_workstream_narrative_history(
            edition=edition_name,
            issue_number=issue_number,
            workstream_names=tuple(workstream.name for workstream in bundle.program_context.workstreams) if bundle.program_context is not None else (),
            current_workstream_blurbs=workstream_blurbs,
            archive_root=archive_root,
        )
    )
    freshness_report = build_freshness_report(
        current_items=items,
        issue_number=issue_number,
        as_of=data_as_of,
        stale_warn_days=bundle.editorial_rules.stale_warn_days,
        stale_block_days=bundle.editorial_rules.stale_block_days,
        previous_snapshot=previous_snapshot,
        previous_notification_state=previous_notification_state,
        program_context=bundle.program_context,
        workstream_narrative_history=workstream_narrative_history,
    )

    review_status = _load_confirm_review_status(
        edition_name,
        issue_number,
        tuple(chapter.id for chapter in continuity_chapters) if _is_continuity_layout(bundle) else tuple(workstream_blurbs),
        reports_root,
    )
    report = ReportData(
        issue_number=issue_number,
        edition=resolved_edition_type,
        generated_at=started_at,
        ado_data_as_of=data_as_of,
        program=_build_model_program_context(bundle),
        items=items,
        deltas=deltas,
        scorecard=dimension_risks,
        scorecard_deltas=scorecard_deltas,
        exec_summary_text=exec_summary_text,
        workstream_blurbs=workstream_blurbs,
        freshness=freshness_report,
        hygiene_warnings=(),
        review_status=review_status,
        manifest_id=__import__("uuid").uuid4().hex,
    )
    item_urls = _build_item_urls(bundle, items)
    workstream_data = _build_workstream_data(
        issue_number=issue_number,
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
    continuity_render = _build_continuity_render_data(
        bundle=bundle,
        issue_number=issue_number,
        edition_type=resolved_edition_type,
        overrides_document=overrides_document,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        workstream_data=workstream_data,
        items=items,
        item_urls=item_urls,
        eta_forecasts={},
    )
    forecast = build_forecast_assessment(
        enabled=bundle.config.forecast_enabled,
        edition_name=edition_name,
        as_of=data_as_of,
        workstreams=workstream_data,
        deltas=deltas,
        archive_root=archive_root,
    )
    auto_suggestions = _build_auto_suggested_top_items(scorecard_deltas, scorecard_packets)
    new_high_count = _count_new_high_dimensions(scorecard_deltas)
    severe_ack_required = _decision_strip_ack_required(top_items, new_high_count, freshness_report)
    read_time_minutes = _compute_read_time_minutes(exec_summary_text, workstream_blurbs, report.edition)
    healthy_streak = _compute_healthy_streak(
        edition_name,
        issue_number,
        dimension_risks,
        archive_root,
    )
    health = _build_health_summary(
        dimension_risks,
        continuity_snapshot,
        overrides_document=overrides_document,
        top_items=top_items,
        forecast=forecast,
        severe_ack_required=severe_ack_required,
        is_dry_run=False,
        read_time_minutes=read_time_minutes,
        edition_type=report.edition,
        new_high_count=new_high_count,
        healthy_streak=healthy_streak,
    )
    forwarding_context = _resolve_forwarding_context(overrides_document, top_items, auto_suggestions)
    exec_summary_citations = build_inline_citations(items, evidence_by_item, ado_base_url=_ado_item_base_url(bundle))
    title = _format_edition_title(bundle, issue_number, data_as_of)
    subject_signal = _subject_signal(dimension_risks, top_items, auto_suggestions, scorecard_deltas)
    email_subject = _build_email_subject(title, health, subject_signal)
    email_preheader = _build_email_preheader(health, health.bluf, top_items or auto_suggestions)
    confirmed_depth = sum(1 for entry in archive_index.issues if entry.kind == "confirmed")
    resolved_v2 = resolve_edition(edition_name, editions_root=editions_root, programs_root=programs_root)
    ado_vitality = None
    vitality_snapshot = None
    if resolved_v2 is not None and resolved_edition_type in {EditionType.DETAILED, EditionType.FOCUSED}:
        vitality_snapshot, vitality_settings = _build_v2_vitality_snapshot(
            resolved_v2=resolved_v2,
            items=items,
            as_of=data_as_of,
            programs_root=programs_root,
        )
        if vitality_settings.newsletter_aggregate:
            ado_vitality = build_vitality_section(
                vitality_snapshot,
                current_issue_number=issue_number,
                history_entries=read_vitality_history(edition_name, archive_root=archive_root),
                items=items,
                workstreams=resolved_v2.workstreams,
                include_individual_praise=vitality_settings.newsletter_individual_praise,
            )
    render_context = RenderContext(
        title=title,
        subtitle=("" if _is_continuity_layout(bundle) else f"Issue {issue_number:03d} confirmed"),
        preheader=email_preheader,
        report=report,
        edition_meta=EditionMeta(
            edition=edition_name,
            issue_number=issue_number,
            generated_at=started_at,
            ado_data_as_of=data_as_of,
            manifest_id=report.manifest_id,
            qg_status="pass",
            email_subject=email_subject,
            email_preheader=email_preheader,
            subject_signal=subject_signal,
            show_orientation=overrides_document.show_orientation or confirmed_depth < 2,
        ),
        layout_mode=bundle.config.layout_mode,
        health=health,
        top_items=top_items,
        auto_suggestions=auto_suggestions,
        forwarding_context=forwarding_context,
        decision_strip_ack_required=severe_ack_required,
        scorecards=scorecards,
        kusto_sections=kusto_sections,
        ado_vitality=ado_vitality,
        workstreams=workstream_data,
        exec_summary_citations=exec_summary_citations,
        sections=(),
        prior_date_label=_format_prior_date_label(continuity_snapshot),
        changes_url=None,
        item_urls=item_urls,
        scorecard_packets=scorecard_packets,
        scorecard_deltas=_group_scorecard_deltas(scorecard_deltas),
        scorecard_urls={name: next(iter(packet_map.values())).ado_query_url for name, packet_map in scorecard_packets.items() if packet_map},
        workstream_urls={},
        is_dry_run=False,
        workspace_root=str(Path(__file__).resolve().parents[2]),
        mobile_safe_scorecards=bundle.config.mobile_safe_scorecards,
        type_scale_v2=bundle.config.type_scale_v2,
        continuity=continuity_render,
        show_footer=not _is_continuity_layout(bundle),
    )
    if resolved_edition_type == EditionType.DECK:
        html_body = ""
        _deck_md = get_program_output_dir(edition_name, programs_root=programs_root) / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.deck.md"
        markdown_body = _deck_md.read_text(encoding="utf-8") if _deck_md.exists() else ""
    else:
        html_body = HTMLRenderer(edition_name, reports_root=reports_root).render(render_context)
        markdown_body = TeamsRenderer(edition_name, reports_root=reports_root).render(render_context)
    snapshot = _build_snapshot(report, scorecard_packets)
    rendered_strings = {f"workstream:{section_id}": blurb for section_id, blurb in workstream_blurbs.items()}
    location_profiles: dict[str, Any] | None = None
    if resolved_edition_type == EditionType.LOOKBACK:
        lookback_strings, lookback_location_profiles = build_lookback_ban_list_inputs(
            html_body=html_body,
            markdown_body=markdown_body,
            exec_summary_text=exec_summary_text,
            incident_learning=render_context.incident_learning,
        )
        rendered_strings.update(lookback_strings)
        location_profiles = lookback_location_profiles
    else:
        rendered_strings.update(
            {
                "html": html_body,
                "markdown": markdown_body,
                "exec_summary": exec_summary_text,
            }
        )
    ban_violations = find_ban_list_violations(
        rendered_strings,
        bundle.editorial_rules,
        location_profiles=location_profiles,
    )
    verbosity_violations = enforce_verbosity(
        workstream_blurbs=workstream_blurbs,
        exec_summary_text=exec_summary_text,
        scorecard_summaries={dimension.name: dimension.summary for dimension in dimension_risks},
        subject_line=_format_edition_title(bundle, issue_number, data_as_of),
        verbosity=bundle.editorial_rules.verbosity,
        edition_type=resolved_edition_type,
    )
    workstream_citations = {workstream.section_id: workstream.citations for workstream in workstream_data}
    hygiene_warnings = evaluate_hygiene(
        workstream_blurbs=workstream_blurbs,
        workstream_citations=workstream_citations,
        exec_summary_text=exec_summary_text,
        exec_summary_citations=exec_summary_citations,
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
        issue_number=issue_number,
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
        freshness_summary={"blocks": freshness_report.blocks, "warns": freshness_report.warns, "infos": freshness_report.infos},
        qg_results={},
        git_sha=_read_git_sha(),
        metadata={
            "suggested_subject": email_subject,
            "suggested_preheader": email_preheader,
            "subject_signal": subject_signal,
            "forecast_summary": (forecast.summary if forecast is not None else None),
            "forecast_confidence": (forecast.confidence.value if forecast is not None else None),
            "forecast_sources": (list(forecast.source_item_ids) if forecast is not None else []),
            "ai_safety": _load_draft_ai_safety_metadata(
                edition_name=edition_name,
                issue_number=issue_number,
                programs_root=programs_root,
            ),
        },
        # D-17: read back the gather-run lineage pinned in the draft at
        # generation time -- never re-resolved live, so confirm structurally
        # cannot silently rebind to a newer committed run than the draft used.
        gather_run_id=draft_state.get("gather_run_id"),
        gather_run_hash=draft_state.get("gather_run_hash"),
    )
    qg_phase_1a = evaluate_phase_1a_gates(
        ban_list_violations=ban_violations,
        verbosity_violations=verbosity_violations,
        manifest=provisional_manifest,
        expected_snapshot_hash=provisional_manifest.snapshot_hash,
        dimension_risks=dimension_risks,
        program_id=program_id,
        edition_name=edition_name,
        issue_number=issue_number,
        archive_root=archive_root,
        programs_root=programs_root,
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
    narrative_covered_item_ids = _build_newsletter_narrative_covered_item_ids(
        bundle=bundle,
        edition_type=resolved_edition_type,
        items=items,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        overrides_document=overrides_document,
        continuity_chapters=continuity_chapters,
        visible_section_ids=visible_section_ids,
        loaded_narratives=loaded_narratives,
    )
    gather_state = load_gather_state(program_id, programs_root=programs_root) if program_id is not None else None
    slice_contracts = (
        load_slice_contract_for_edition(edition_name, reports_root=reports_root)
        if program_id is not None
        else None
    )
    source_waivers = load_source_waivers(program_id, programs_root=programs_root) if program_id is not None else ()
    stale_claim_ids = _load_persisted_stale_claim_ids(
        program_id=program_id,
        issue_number=issue_number,
        programs_root=programs_root,
    )
    qg_phase_1b = evaluate_phase_1b_gates(
        freshness_report=freshness_report,
        items=items,
        publishable_item_ids=tuple(item.id for item in newsletter_items),
        covered_item_ids=narrative_covered_item_ids,
        as_of=data_as_of,
        deltas=deltas,
        edition_name=edition_name,
        issue_number=issue_number,
        workstream_blurbs=workstream_blurbs,
        program_context=bundle.program_context,
        dimension_risks=dimension_risks,
        overrides_document=overrides_document,
        approved_signals=approved_signals,
        narratives=loaded_narratives,
        journal_signals=journal_signals,
        program_id=program_id,
        program_maturity_level=resolved_v2.program.maturity_level if resolved_v2 is not None else 0,
        workstreams=resolved_workstreams,
        scorecards=resolved_scorecards,
        channel_states=gather_state.channels if gather_state is not None else None,
        archive_root=archive_root,
        programs_root=programs_root,
        stale_claim_ids=stale_claim_ids,
    )
    qg_phase_1c = evaluate_phase_1c_gates(
        hygiene_warnings=hygiene_warnings,
        review_status=review_status,
        review_required=bundle.review.required,
        archive_inconsistencies=find_archive_index_inconsistencies(edition_name, archive_root=archive_root),
        html_content=html_body,
    )
    continuity_qg = (
        evaluate_continuity_gates(html_content=html_body, issue_number=issue_number)
        if _is_continuity_layout(bundle)
        else QualityGateReport(results=())
    )
    bridge_qg = evaluate_bridge_gates(
        continuation_contract=build_continuation_contract(
            edition_name=edition_name,
            issue_number=issue_number,
            started_at=started_at,
            reports_root=reports_root,
            archive_root=archive_root,
            editions_root=editions_root,
            programs_root=programs_root,
            overrides_document=overrides_document,
            workstream_data=workstream_data,
            output_dir=get_program_output_dir(edition_name, programs_root=programs_root),
            current_scorecard_dimensions=tuple(
                sorted(
                    (scorecard.name, dimension.name)
                    for scorecard in bundle.config.scorecards
                    for dimension in scorecard.dimensions
                )
            ),
            current_section_ids=section_roster_current_ids,
        ),
        narratives=loaded_narratives,
        review_status=review_status,
        bridge_graduated=(
            trusted_baseline.bridge_graduated
            if (
                trusted_baseline := load_trusted_baseline(
                    edition_name,
                    editions_root=editions_root,
                    programs_root=programs_root,
                )
            ) is not None
            else False
        ),
    )
    readiness_gate_enabled, readiness_snapshot_max_age_days = _load_readiness_gate_settings(
        edition_name=edition_name,
        program_id=program_id,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    readiness_qg = (
        evaluate_readiness_gates(
            program_id=program_id,
            programs_root=programs_root,
            max_age_days=readiness_snapshot_max_age_days,
        )
        if readiness_gate_enabled
        else QualityGateReport(results=())
    )
    # Context Integrity Block — QG-CI-01 (DATE-01 stub WI IDs) and QG-CI-02 (FILTER-01 informal OData)
    # Spec: program-context-maturity.md §12 item 13
    context_integrity_qg = evaluate_context_integrity_gates(
        program_id=program_id or "",
        programs_root=programs_root,
    )
    qg_report = combine_gate_reports(qg_phase_1a, qg_phase_1b, qg_phase_1c, continuity_qg, bridge_qg, readiness_qg, context_integrity_qg)
    source_health_qg = evaluate_source_health_gates(
        program_id=program_id,
        edition_name=edition_name,
        slice_contracts=slice_contracts,
        gather_state=gather_state,
        waivers=source_waivers,
        function_name=source_health_function_name_for_edition(str(draft_state.get("edition_type", bundle.config.edition.type))),
    )
    if program_id is not None:
        _emit_source_health_alerts_best_effort(
            source_health_qg, program_id=program_id, edition_name=edition_name, programs_root=programs_root,
        )
    qg_report = combine_gate_reports(qg_report, source_health_qg)
    qg_program_fact_drift = evaluate_program_fact_drift_from_draft(
        draft_state=draft_state,
        program_id=program_id,
        db_root=reports_root.parent / "vertex-db",
    )
    qg_report = combine_gate_reports(qg_report, qg_program_fact_drift)
    if program_id is not None:
        contradiction_packets = load_contradiction_state(program_id, programs_root=programs_root)
        qg_report = combine_gate_reports(qg_report, evaluate_contradiction_gate(contradiction_packets))
        # P4-4 (spec §14.1): WorkIQ/M365 enrichment gates QG-WIQ-1/2/3/7. Self-contained
        # reader loads signal/evidence/provenance stores internally.
        wiq_qg = evaluate_workiq_confirm_gates(
            program_id=program_id,
            programs_root=programs_root,
            channel_states=gather_state.channels if gather_state is not None else None,
            workstreams=resolved_workstreams,
            as_of=data_as_of,
        )
        qg_report = combine_gate_reports(qg_report, wiq_qg)
    final_manifest = build_run_manifest(
        manifest_id=report.manifest_id,
        issue_number=issue_number,
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
        freshness_summary={"blocks": freshness_report.blocks, "warns": freshness_report.warns, "infos": freshness_report.infos},
        qg_results=qg_report.qg_results,
        git_sha=_read_git_sha(),
        metadata={
            "suggested_subject": email_subject,
            "suggested_preheader": email_preheader,
            "subject_signal": subject_signal,
            "forecast_summary": (forecast.summary if forecast is not None else None),
            "forecast_confidence": (forecast.confidence.value if forecast is not None else None),
            "forecast_sources": (list(forecast.source_item_ids) if forecast is not None else []),
            "ai_safety": _load_draft_ai_safety_metadata(
                edition_name=edition_name,
                issue_number=issue_number,
                programs_root=programs_root,
            ),
            "draft_readiness": _load_draft_readiness_metadata(
                edition_name=edition_name,
                issue_number=issue_number,
            ),
        },
        gather_run_id=draft_state.get("gather_run_id"),
        gather_run_hash=draft_state.get("gather_run_hash"),
    )
    warnings = tuple(hygiene_warnings) + _workstream_narrative_warnings(
        issue_number=issue_number,
        workstream_data=workstream_data,
        stale_narratives=(),
        stage="confirm",
    )
    return qg_report, warnings, snapshot, final_manifest, html_body, markdown_body, report, workstream_data, vitality_snapshot


def _load_persisted_stale_claim_ids(
    *,
    program_id: str | None,
    issue_number: int,
    programs_root: Path,
) -> tuple[str, ...]:
    if program_id is None:
        return ()
    return load_stale_claim_ids(program_id, issue_number, programs_root=programs_root)

def _record_learning_distillation(
    *,
    edition_name: str,
    issue_number: int,
    editorial_rules: EditorialRules,
    programs_root: Path | None = None,
) -> tuple[Path | None, Path | None, str | None, str | None]:
    return _record_learning_distillation_impl(
        edition_name=edition_name,
        issue_number=issue_number,
        editorial_rules=editorial_rules,
        programs_root=programs_root,  # type: ignore[arg-type]
    )


def _record_edit_patterns_for_v2(
    *,
    edition_name: str,
    issue_number: int,
    draft_state: dict[str, Any],
    report: ReportData,
    confirmed_at: datetime,
    reports_root: Path,
) -> str | None:
    return _record_edit_patterns_for_v2_impl(
        edition_name=edition_name,
        issue_number=issue_number,
        draft_state=draft_state,
        report=report,
        confirmed_at=confirmed_at,
        reports_root=reports_root,
    )
