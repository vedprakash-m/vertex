# Adapted from Artha scripts/lib/quality_gate.py
from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from datetime import date, datetime, timezone
import json
from pathlib import Path
from typing import Any

from src.core.ado_reconcile import build_ado_reconcile_report
from src.core.archive_store import find_latest_confirmed_entry, get_dimension_history, read_archive_index
from src.core.claim_tracker import load_open_claims
from src.core.config_loader import NarrativeProgramContext
from src.core.contradiction_engine import build_contradiction_packets
from src.core.coverage_gap import build_coverage_gaps
from src.core.journal import PROGRAMS_ROOT
from src.core.ledger.event_log import compute_envelope_hash, read_events, verify_event_log, write_event
from src.core.ledger.program_views import project_program_events
from src.core.models import DeltaKind, DeltaSet, DimensionRisk, FreshnessReport, ReviewState, ReviewStatus, RiskLevel, RunManifest, WorkItem
from src.core.models_v2 import DependencyStatus, MilestoneStatus, Scorecard, Signal, Workstream
from src.core.narrative_store import load_archived_narratives
from src.core.overrides_store import OverridesDocument
from src.core.projections.snapshot_manager import get_snapshot_dir
from src.core.snapshot_store import ARCHIVE_ROOT
_ESCALATION_SOURCE = "vertex/escalation"


# Gate result value objects live in the models submodule (D-09 split). Re-exported
# here so existing `from src.core.quality_gates import GateEvaluation` imports work.
from src.core.quality_gates.models import (
    GateEvaluation,
    QualityGateReport,
    combine_gate_reports,
)


# Program Fact Store drift gate (QG-SG-20) lives in the fact_drift submodule
# (D-09 split). Re-exported here so existing imports keep working.
from src.core.quality_gates.fact_drift import (  # noqa: E402
    evaluate_program_fact_drift_from_draft,
    evaluate_program_fact_drift_gate,
)


# Single-concern gate clusters live in their own submodules (D-09 split).
# Re-exported here so existing imports keep working.
from src.core.quality_gates.bridge import evaluate_bridge_gates  # noqa: E402
from src.core.quality_gates.chart import evaluate_chart_gates  # noqa: E402
from src.core.quality_gates.chronic import evaluate_chronic_high_dimension_gate as _evaluate_chronic_high_dimension_gate_impl  # noqa: E402
from src.core.quality_gates.chronic import has_risk_or_escalation_coverage as _has_risk_or_escalation_coverage_impl  # noqa: E402
from src.core.quality_gates.continuity import evaluate_continuity_gates  # noqa: E402
from src.core.quality_gates.context_integrity import evaluate_context_integrity_gates  # noqa: E402
from src.core.quality_gates.contradiction import evaluate_contradiction_gate  # noqa: E402
from src.core.quality_gates.current_state import evaluate_open_action_completeness_gate as _evaluate_open_action_completeness_gate_impl  # noqa: E402
from src.core.quality_gates.current_state import load_current_actions as _load_current_actions_impl  # noqa: E402
from src.core.quality_gates.current_state import load_current_dependencies as _load_current_dependencies_impl  # noqa: E402
from src.core.quality_gates.current_state import load_current_milestones as _load_current_milestones_impl  # noqa: E402
from src.core.quality_gates.current_state import load_current_risks as _load_current_risks_impl  # noqa: E402
from src.core.quality_gates.editorial import evaluate_claim_freshness_gate as _evaluate_claim_freshness_gate  # noqa: E402
from src.core.quality_gates.editorial import evaluate_gap_detection_sla_gate as _evaluate_gap_detection_sla_gate  # noqa: E402
from src.core.quality_gates.editorial import evaluate_candidate_triage_latency_gate as _evaluate_candidate_triage_latency_gate  # noqa: E402
from src.core.quality_gates.editorial import evaluate_email_signal_coverage_gate as _evaluate_email_signal_coverage_gate  # noqa: E402
from src.core.quality_gates.editorial import evaluate_exec_summary_staleness_gate as _evaluate_exec_summary_staleness_gate  # noqa: E402
from src.core.quality_gates.editorial import evaluate_metric_injection_and_ado_hygiene_gate as _evaluate_metric_injection_and_ado_hygiene_gate  # noqa: E402
from src.core.quality_gates.editorial import evaluate_projection_freshness_gate as _evaluate_projection_freshness_gate  # noqa: E402
from src.core.quality_gates.editorial import evaluate_unresolved_conflict_budget_gate as _evaluate_unresolved_conflict_budget_gate  # noqa: E402
from src.core.quality_gates.editorial import evaluate_kpi_degradation_gate as _evaluate_kpi_degradation_gate  # noqa: E402
from src.core.quality_gates.external_dependency import evaluate_external_dependency_gate as _evaluate_external_dependency_gate_impl  # noqa: E402


# Re-exported for direct use (e.g. by doctor / report paths that need
# QG-26 or QG-28 in isolation).
evaluate_external_dependency_gate = _evaluate_external_dependency_gate_impl  # noqa: E402
evaluate_kpi_degradation_gate = _evaluate_kpi_degradation_gate  # noqa: E402
from src.core.quality_gates.freshness import coerce_date as _coerce_date_impl  # noqa: E402
from src.core.quality_gates.freshness import coerce_datetime as _coerce_datetime_impl  # noqa: E402
from src.core.quality_gates.freshness import evaluate_freshness_gate as _evaluate_freshness_gate_impl  # noqa: E402
from src.core.quality_gates.freshness import filter_freshness_report as _filter_freshness_report_impl  # noqa: E402
from src.core.quality_gates.freshness import filter_item_ids_to_scope as _filter_item_ids_to_scope_impl  # noqa: E402
from src.core.quality_gates.freshness import filter_items_to_scope as _filter_items_to_scope_impl  # noqa: E402
from src.core.quality_gates.narrative import evaluate_claim_contradiction_gate as _evaluate_claim_contradiction_gate  # noqa: E402
from src.core.quality_gates.narrative import evaluate_contradiction_narrative_gate as _evaluate_contradiction_narrative_gate  # noqa: E402
from src.core.quality_gates.narrative import evaluate_high_risk_next_action_gate as _evaluate_high_risk_next_action_gate  # noqa: E402
from src.core.quality_gates.narrative import evaluate_material_change_narrative_gate as _evaluate_material_change_narrative_gate  # noqa: E402
from src.core.quality_gates.narrative import _dimension_workstream_ids  # noqa: E402
from src.core.quality_gates.operational import evaluate_cross_program_dependency_cascade_gate as _evaluate_cross_program_dependency_cascade_gate_impl  # noqa: E402
from src.core.quality_gates.operational import evaluate_high_risk_coverage_gate as _evaluate_high_risk_coverage_gate_impl  # noqa: E402
from src.core.quality_gates.operational import evaluate_milestone_risk_linkage_gate as _evaluate_milestone_risk_linkage_gate_impl  # noqa: E402
from src.core.quality_gates.operational import evaluate_overdue_target_gate as _evaluate_overdue_target_gate_impl  # noqa: E402
from src.core.quality_gates.operational import format_cross_program_cascade_gate_line as _format_cross_program_cascade_gate_line_impl  # noqa: E402
from src.core.quality_gates.operational import has_milestone_risk_linkage as _has_milestone_risk_linkage_impl  # noqa: E402
from src.core.quality_gates.operational import has_overdue_target as _has_overdue_target_impl  # noqa: E402
from src.core.quality_gates.operational import is_terminal as _is_terminal_impl  # noqa: E402
from src.core.quality_gates.persona import evaluate_persona_signal_gates  # noqa: E402
from src.core.quality_gates.operational import preview_work_item_ids as _preview_work_item_ids_impl  # noqa: E402
from src.core.quality_gates.readiness import evaluate_readiness_gates  # noqa: E402
from src.core.quality_gates.rendering import evaluate_outlook_compatibility_gate as _evaluate_outlook_compatibility_gate  # noqa: E402
from src.core.quality_gates.source_health import evaluate_source_health_gates  # noqa: E402
from src.core.quality_gates.ai_budget import evaluate_ai_budget_gate as _evaluate_ai_budget_gate_impl  # noqa: E402

# Re-exported for direct use (e.g. doctor --ai-budget).
evaluate_ai_budget_gate = _evaluate_ai_budget_gate_impl  # noqa: E402

# Newsletter-WorkIQ enrichment gates (P4-4, spec §14.1). Self-contained confirm
# reader plus the individual pure gate functions used by the report/doctor surfaces.
from src.core.quality_gates.workiq import (  # noqa: E402
    evaluate_workiq_blurb_provenance_gate,
    evaluate_workiq_budget_gate,
    evaluate_workiq_confirm_gates,
    evaluate_workiq_evidence_presence_gate,
    evaluate_workiq_latest_divergence_gate,
    evaluate_workiq_pending_signal_gate,
    evaluate_workiq_signal_recency_gate,
    evaluate_workiq_source_freshness_gate,
    evaluate_workiq_transcript_extraction_block_gate,
    evaluate_workiq_transcript_identifier_gate,
)


def evaluate_phase_1a_gates(
    *,
    ban_list_violations: tuple[Any, ...] | list[Any],
    verbosity_violations: tuple[Any, ...] | list[Any] | dict[str, tuple[Any, ...] | list[Any]],
    manifest: RunManifest | None,
    expected_snapshot_hash: str,
    dimension_risks: tuple[DimensionRisk, ...] | list[DimensionRisk],
    program_id: str | None = None,
    edition_name: str | None = None,
    issue_number: int | None = None,
    archive_root: Path = ARCHIVE_ROOT,
    programs_root: Path = PROGRAMS_ROOT,
) -> QualityGateReport:
    results = (
        _evaluate_ban_list_gate(ban_list_violations),
        _evaluate_verbosity_gate(verbosity_violations),
        _evaluate_manifest_gate(manifest, expected_snapshot_hash),
        _evaluate_hash_chain_integrity_gate(
            program_id=program_id,
            programs_root=programs_root,
        ),
        _evaluate_hardlock_immutability_gate(
            program_id=program_id,
            edition_name=edition_name if edition_name is not None else (manifest.edition if manifest is not None else None),
            issue_number=issue_number if issue_number is not None else (manifest.issue_number if manifest is not None else None),
            archive_root=archive_root,
            programs_root=programs_root,
        ),
        _evaluate_risk_input_gate(dimension_risks),
    )
    return QualityGateReport(results=results)


def evaluate_phase_1b_gates(
    *,
    freshness_report: FreshnessReport,
    items: tuple[WorkItem, ...] | list[WorkItem] = (),
    publishable_item_ids: Collection[int] | None = None,
    covered_item_ids: Collection[int] = (),
    as_of: date | datetime | None = None,
    deltas: DeltaSet | None = None,
    edition_name: str | None = None,
    issue_number: int | None = None,
    workstream_blurbs: Mapping[str, str] | None = None,
    program_context: NarrativeProgramContext | None = None,
    dimension_risks: tuple[DimensionRisk, ...] | list[DimensionRisk] = (),
    overrides_document: OverridesDocument | None = None,
    approved_signals: tuple[Signal, ...] | list[Signal] = (),
    narratives: Mapping[str, str] | Iterable[str] = (),
    journal_signals: tuple[Signal, ...] | list[Signal] = (),
    program_id: str | None = None,
    program_maturity_level: int = 0,
    workstreams: tuple[Workstream, ...] | list[Workstream] = (),
    scorecards: tuple[Scorecard, ...] | list[Scorecard] = (),
    channel_states: dict[str, dict[str, Any]] | None = None,
    archive_root: Path = ARCHIVE_ROOT,
    programs_root: Path = PROGRAMS_ROOT,
    budget_usd_per_run: float = 0.0,
    stale_claim_ids: Collection[str] = (),
) -> QualityGateReport:
    resolved_items = tuple(items)
    resolved_publishable_item_ids = (
        {int(item_id) for item_id in publishable_item_ids}
        if publishable_item_ids is not None
        else None
    )
    scoped_items = _filter_items_to_scope(resolved_items, resolved_publishable_item_ids)
    scoped_freshness_report = _filter_freshness_report(freshness_report, resolved_publishable_item_ids)
    scoped_covered_item_ids = _filter_item_ids_to_scope(covered_item_ids, resolved_publishable_item_ids)
    resolved_dimension_risks = tuple(dimension_risks)
    resolved_approved_signals = tuple(approved_signals)
    resolved_journal_signals = tuple(journal_signals)
    resolved_workstreams = tuple(workstreams)
    resolved_scorecards = tuple(scorecards)
    resolved_as_of = as_of or datetime.now(timezone.utc)
    today = _coerce_date(resolved_as_of)
    as_of_timestamp = _coerce_datetime(resolved_as_of)

    results = (
        _evaluate_freshness_gate(scoped_freshness_report),
        _evaluate_claim_freshness_gate(tuple(stale_claim_ids)),
        _evaluate_gap_detection_sla_gate(
            program_id=program_id,
            programs_root=programs_root,
            now=as_of_timestamp,
        ),
        _evaluate_unresolved_conflict_budget_gate(
            program_id=program_id,
            programs_root=programs_root,
        ),
        _evaluate_candidate_triage_latency_gate(
            program_id=program_id,
            programs_root=programs_root,
            now=as_of_timestamp,
        ),
        _evaluate_projection_freshness_gate(
            program_id=program_id,
            programs_root=programs_root,
        ),
        _evaluate_overdue_target_gate(scoped_items, today),
        _evaluate_material_change_narrative_gate(
            items=scoped_items,
            deltas=deltas,
            edition_name=edition_name,
            issue_number=issue_number,
            workstream_blurbs=workstream_blurbs,
            program_context=program_context,
            archive_root=archive_root,
        ),
        _evaluate_claim_contradiction_gate(
            items=resolved_items,
            program_id=program_id,
            program_maturity_level=program_maturity_level,
            workstreams=resolved_workstreams,
            programs_root=programs_root,
        ),
        _evaluate_contradiction_narrative_gate(
            items=resolved_items,
            approved_signals=resolved_approved_signals,
            workstream_blurbs=workstream_blurbs,
            narratives=narratives,
            as_of=as_of_timestamp,
            program_id=program_id,
            workstreams=resolved_workstreams,
            programs_root=programs_root,
        ),
        _evaluate_chronic_high_dimension_gate(
            dimension_risks=resolved_dimension_risks,
            edition_name=edition_name,
            journal_signals=resolved_journal_signals,
            program_id=program_id,
            workstreams=resolved_workstreams,
            scorecards=resolved_scorecards,
            archive_root=archive_root,
            programs_root=programs_root,
        ),
        _evaluate_high_risk_coverage_gate(
            items=scoped_items,
            approved_signals=resolved_approved_signals,
            narratives=narratives,
            as_of=as_of_timestamp,
            covered_item_ids=scoped_covered_item_ids,
        ),
        _evaluate_high_risk_next_action_gate(
            dimension_risks=resolved_dimension_risks,
            overrides_document=overrides_document,
            workstream_blurbs=workstream_blurbs,
            scorecards=resolved_scorecards,
            workstreams=resolved_workstreams,
        ),
        _evaluate_open_action_completeness_gate(
            program_id=program_id,
            programs_root=programs_root,
        ),
        _evaluate_milestone_risk_linkage_gate(
            items=resolved_items,
            as_of=as_of_timestamp,
            program_id=program_id,
            programs_root=programs_root,
        ),
        _evaluate_cross_program_dependency_cascade_gate(
            items=resolved_items,
            approved_signals=resolved_approved_signals,
            as_of=as_of_timestamp,
            program_id=program_id,
            programs_root=programs_root,
        ),
        _evaluate_exec_summary_staleness_gate(
            edition_name=edition_name,
            issue_number=issue_number,
        ),
        _evaluate_email_signal_coverage_gate(
            channel_states=channel_states,
            program_id=program_id,
            programs_root=programs_root,
        ),
        _evaluate_metric_injection_and_ado_hygiene_gate(
            program_id=program_id,
            narratives=narratives,
            items=resolved_items,
        ),
        _evaluate_external_dependency_gate(
            program_id=program_id,
            programs_root=programs_root,
        ),
        _evaluate_ai_budget_gate(
            program_id=program_id,
            budget_usd_per_run=budget_usd_per_run,
            programs_root=programs_root,
        ),
        _evaluate_kpi_degradation_gate(
            program_id=program_id,
            programs_root=programs_root,
        ),
    )
    return QualityGateReport(results=results)


def _filter_items_to_scope(
    items: tuple[WorkItem, ...],
    publishable_item_ids: set[int] | None,
) -> tuple[WorkItem, ...]:
    return _filter_items_to_scope_impl(items, publishable_item_ids)


def _filter_freshness_report(
    freshness_report: FreshnessReport,
    publishable_item_ids: set[int] | None,
) -> FreshnessReport:
    return _filter_freshness_report_impl(freshness_report, publishable_item_ids)


def _filter_item_ids_to_scope(
    item_ids: Collection[int],
    publishable_item_ids: set[int] | None,
) -> tuple[int, ...]:
    return _filter_item_ids_to_scope_impl(item_ids, publishable_item_ids)


def evaluate_phase_1c_gates(
    *,
    hygiene_warnings: tuple[str, ...] | list[str],
    review_status: ReviewStatus,
    review_required: bool,
    archive_inconsistencies: tuple[str, ...] | list[str],
    html_content: str | None = None,
) -> QualityGateReport:
    results = [
        _evaluate_hygiene_gate(hygiene_warnings),
        _evaluate_review_gate(review_status, review_required),
        _evaluate_archive_index_gate(archive_inconsistencies),
    ]
    if html_content is not None:
        results.append(_evaluate_outlook_compatibility_gate(html_content))
    return QualityGateReport(results=tuple(results))


def _evaluate_ban_list_gate(violations: tuple[Any, ...] | list[Any]) -> GateEvaluation:
    violation_count = len(tuple(violations))
    if violation_count == 0:
        return GateEvaluation("QG-4", True, "Ban-list validation passed.", 3)
    return GateEvaluation("QG-4", False, f"Ban-list validation failed with {violation_count} violation(s).", 3)


def _evaluate_verbosity_gate(
    violations: tuple[Any, ...] | list[Any] | dict[str, tuple[Any, ...] | list[Any]],
) -> GateEvaluation:
    if isinstance(violations, dict):
        violation_count = sum(len(tuple(section_violations)) for section_violations in violations.values())
    else:
        violation_count = len(tuple(violations))
    if violation_count == 0:
        return GateEvaluation("QG-5", True, "Verbosity validation passed.", 3)
    return GateEvaluation("QG-5", False, f"Verbosity validation failed with {violation_count} violation(s).", 3)


def _evaluate_manifest_gate(manifest: RunManifest | None, expected_snapshot_hash: str) -> GateEvaluation:
    if manifest is None:
        return GateEvaluation("QG-6", False, "Run manifest is missing.", 3)
    if manifest.snapshot_hash == expected_snapshot_hash:
        return GateEvaluation("QG-6", True, "Manifest hash matches snapshot.", 3)
    return GateEvaluation("QG-6", False, "Manifest hash does not match the latest snapshot.", 3)


def _evaluate_hash_chain_integrity_gate(*, program_id: str | None, programs_root: Path) -> GateEvaluation:
    if program_id is None:
        return GateEvaluation("QG-DM-1", True, "Hash-chain integrity gate passed (skipped: program not provided).", 3)

    verification = verify_event_log(program_id, programs_root=programs_root)
    if verification.ok:
        return GateEvaluation(
            "QG-DM-1",
            True,
            f"Hash-chain integrity gate passed ({verification.checked_event_count} event(s) verified).",
            3,
        )

    return GateEvaluation(
        "QG-DM-1",
        False,
        f"Hash-chain integrity failed: {'; '.join(verification.issues)}",
        3,
    )


def _evaluate_hardlock_immutability_gate(
    *,
    program_id: str | None,
    edition_name: str | None,
    issue_number: int | None,
    archive_root: Path,
    programs_root: Path,
) -> GateEvaluation:
    if program_id is None or edition_name is None or issue_number is None:
        return GateEvaluation(
            "QG-DM-4",
            True,
            "Hardlock immutability gate passed (skipped: program, edition, or issue number not provided).",
            3,
        )

    previous_confirmed = find_latest_confirmed_entry(
        read_archive_index(edition_name, archive_root=archive_root),
        before_issue_number=issue_number,
    )
    if previous_confirmed is None:
        return GateEvaluation("QG-DM-4", True, "Hardlock immutability gate passed (no previous confirmed issue).", 3)

    snapshot_dir = get_snapshot_dir(program_id, programs_root=programs_root)
    manifest_candidates = (
        sorted(snapshot_dir.glob(f"issue_{previous_confirmed.issue_number:03d}-*.manifest.json"))
        if snapshot_dir.exists()
        else []
    )
    if not manifest_candidates:
        return GateEvaluation(
            "QG-DM-4",
            True,
            (
                f"Hardlock immutability gate passed (skipped: previous confirmed issue {previous_confirmed.issue_number:03d} predates ledger hardlock artifacts)."
            ),
            3,
        )

    try:
        manifest_payload = json.loads(manifest_candidates[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return GateEvaluation(
            "QG-DM-4",
            False,
            f"Hardlock immutability failed: previous confirmed issue {previous_confirmed.issue_number:03d} snapshot manifest is unreadable: {exc}",
            3,
        )

    snapshot_hash = manifest_payload.get("snapshot_hash")
    event_id_watermark = manifest_payload.get("event_id_watermark")
    hash_chain_head = manifest_payload.get("hash_chain_head")
    if not isinstance(snapshot_hash, str) or not snapshot_hash or not isinstance(event_id_watermark, str) or not event_id_watermark:
        return GateEvaluation(
            "QG-DM-4",
            False,
            f"Hardlock immutability failed: previous confirmed issue {previous_confirmed.issue_number:03d} snapshot manifest is missing snapshot hash or watermark.",
            3,
        )

    events = read_events(program_id, programs_root=programs_root)
    watermark_event = next((event for event in events if event.event_id == event_id_watermark), None)
    if watermark_event is None:
        return GateEvaluation(
            "QG-DM-4",
            False,
            f"Hardlock immutability failed: previous confirmed issue {previous_confirmed.issue_number:03d} watermark event {event_id_watermark} is missing from the ledger.",
            3,
        )

    if isinstance(hash_chain_head, str) and hash_chain_head and compute_envelope_hash(watermark_event) != hash_chain_head:
        return GateEvaluation(
            "QG-DM-4",
            False,
            (
                f"Hardlock immutability failed: previous confirmed issue {previous_confirmed.issue_number:03d} chain-head hash no longer matches "
                f"watermark event {event_id_watermark}."
            ),
            3,
        )

    matching_hardlock = next(
        (
            event
            for event in events
            if event.event_type == "operator.baseline_hardlock.v1"
            and event.payload.get("issue_number") == previous_confirmed.issue_number
            and event.payload.get("snapshot_hash") == snapshot_hash
            and event.payload.get("event_id_watermark") == event_id_watermark
        ),
        None,
    )
    if matching_hardlock is None:
        return GateEvaluation(
            "QG-DM-4",
            False,
            (
                f"Hardlock immutability failed: previous confirmed issue {previous_confirmed.issue_number:03d} is missing a matching "
                "baseline hardlock event for its ledger snapshot manifest."
            ),
            3,
        )

    return GateEvaluation(
        "QG-DM-4",
        True,
        f"Hardlock immutability gate passed for previous confirmed issue {previous_confirmed.issue_number:03d}.",
        3,
    )


def _evaluate_risk_input_gate(dimension_risks: tuple[DimensionRisk, ...] | list[DimensionRisk]) -> GateEvaluation:
    missing_dimensions = [dimension.name for dimension in dimension_risks if dimension.risk == RiskLevel.UNKNOWN]
    if not missing_dimensions:
        return GateEvaluation("QG-8", True, "All scorecard dimensions have author-confirmed risk levels.", 3)
    joined = ", ".join(sorted(missing_dimensions))
    return GateEvaluation("QG-8", False, f"Missing risk levels for: {joined}", 3)


def _evaluate_freshness_gate(freshness_report: FreshnessReport) -> GateEvaluation:
    return _evaluate_freshness_gate_impl(freshness_report)


def _evaluate_overdue_target_gate(items: tuple[WorkItem, ...], today: date) -> GateEvaluation:
    return _evaluate_overdue_target_gate_impl(items, today)


def _evaluate_chronic_high_dimension_gate(
    *,
    dimension_risks: tuple[DimensionRisk, ...],
    edition_name: str | None,
    journal_signals: tuple[Signal, ...],
    program_id: str | None,
    workstreams: tuple[Workstream, ...],
    scorecards: tuple[Scorecard, ...],
    archive_root: Path,
    programs_root: Path,
) -> GateEvaluation:
    if edition_name is None or program_id is None or not dimension_risks:
        return GateEvaluation("QG-12", True, "Chronic high-risk escalation gate passed.", 2, forceable=True)

    open_risks = tuple(
        risk
        for risk in _load_current_risks(program_id, programs_root=programs_root)
        if risk.status.value != "closed"
    )
    return _evaluate_chronic_high_dimension_gate_impl(
        dimension_risks=dimension_risks,
        edition_name=edition_name,
        journal_signals=journal_signals,
        program_id=program_id,
        workstreams=workstreams,
        scorecards=scorecards,
        archive_root=archive_root,
        open_risks=open_risks,
        dimension_workstream_ids=_dimension_workstream_ids(scorecards),
        escalation_source=_ESCALATION_SOURCE,
    )


def _evaluate_open_action_completeness_gate(*, program_id: str | None, programs_root: Path) -> GateEvaluation:
    return _evaluate_open_action_completeness_gate_impl(
        program_id=program_id,
        programs_root=programs_root,
    )


def _load_current_actions(program_id: str, *, programs_root: Path):
    return _load_current_actions_impl(program_id, programs_root=programs_root)


def _load_current_milestones(program_id: str, *, programs_root: Path):
    return _load_current_milestones_impl(program_id, programs_root=programs_root)


def _load_current_risks(program_id: str, *, programs_root: Path):
    return _load_current_risks_impl(program_id, programs_root=programs_root)


def _load_current_dependencies(program_id: str, *, programs_root: Path):
    return _load_current_dependencies_impl(program_id, programs_root=programs_root)


def _evaluate_milestone_risk_linkage_gate(
    *,
    items: tuple[WorkItem, ...],
    as_of: datetime,
    program_id: str | None,
    programs_root: Path,
) -> GateEvaluation:
    return _evaluate_milestone_risk_linkage_gate_impl(
        items=items,
        as_of=as_of,
        program_id=program_id,
        programs_root=programs_root,
    )


def _evaluate_cross_program_dependency_cascade_gate(
    *,
    items: tuple[WorkItem, ...],
    approved_signals: tuple[Signal, ...],
    as_of: datetime,
    program_id: str | None,
    programs_root: Path,
) -> GateEvaluation:
    return _evaluate_cross_program_dependency_cascade_gate_impl(
        items=items,
        approved_signals=approved_signals,
        as_of=as_of,
        program_id=program_id,
        programs_root=programs_root,
    )


def _format_cross_program_cascade_gate_line(cascade: Any) -> str:
    return _format_cross_program_cascade_gate_line_impl(cascade)


def _evaluate_high_risk_coverage_gate(
    *,
    items: tuple[WorkItem, ...],
    approved_signals: tuple[Signal, ...],
    narratives: Mapping[str, str] | Iterable[str],
    as_of: datetime,
    covered_item_ids: Collection[int] = (),
) -> GateEvaluation:
    return _evaluate_high_risk_coverage_gate_impl(
        items=items,
        approved_signals=approved_signals,
        narratives=narratives,
        as_of=as_of,
        covered_item_ids=covered_item_ids,
    )


def _evaluate_hygiene_gate(hygiene_warnings: tuple[str, ...] | list[str]) -> GateEvaluation:
    warning_count = len(tuple(hygiene_warnings))
    if warning_count == 0:
        return GateEvaluation("QG-2", True, "Hygiene gate passed.", 2, forceable=True)
    return GateEvaluation("QG-2", False, f"Hygiene gate failed with {warning_count} warning(s).", 2, forceable=True)


def _evaluate_review_gate(review_status: ReviewStatus, review_required: bool) -> GateEvaluation:
    if not review_required:
        return GateEvaluation("QG-3", True, "Review approval is not required for this edition.", 2, forceable=True)
    if review_status.all_approved:
        return GateEvaluation("QG-3", True, "All review sections are approved.", 2, forceable=True)

    pending_sections = ", ".join(
        f"{section.section_id}"
        for section in review_status.sections
        if section.state not in {ReviewState.APPROVED, ReviewState.SKIPPED_NO_DELTA}
    )
    skipped_count = sum(
        1 for section in review_status.sections
        if section.state == ReviewState.SKIPPED_NO_DELTA
    )
    skip_note = f" ({skipped_count} skipped_no_delta excluded)" if skipped_count else ""
    return GateEvaluation("QG-3", False, f"Review gate failed for: {pending_sections}{skip_note}", 2, forceable=True)


def _evaluate_archive_index_gate(archive_inconsistencies: tuple[str, ...] | list[str]) -> GateEvaluation:
    issues = tuple(archive_inconsistencies)
    if not issues:
        return GateEvaluation("QG-7", True, "Archive index is consistent with archived files.", 2, forceable=True)
    preview = "; ".join(issues[:3])
    if len(issues) > 3:
        preview = f"{preview}; and {len(issues) - 3} more"
    return GateEvaluation(
        "QG-7",
        False,
        f"Archive index is inconsistent with archived files: {preview}",
        2,
        forceable=True,
    )


def _evaluate_external_dependency_gate(*, program_id: str | None, programs_root: Path) -> GateEvaluation:
    """WS-2 QG-26: external dependency state gate.

    Passes vacuously (n/a) when a program has no `external_dependencies.jsonl`
    OR when no critical deps are non-terminal. Otherwise surfaces the count
    of blocking critical deps as a forceable failure.
    """
    report = _evaluate_external_dependency_gate_impl(
        program_id=program_id,
        programs_root=programs_root,
    )
    if not report.results:
        return GateEvaluation("QG-26", True, "External dependency gate passed (no program).", 3, forceable=True)
    return report.results[0]


def _evaluate_ai_budget_gate(
    *,
    program_id: str | None,
    budget_usd_per_run: float,
    programs_root: Path,
) -> GateEvaluation:
    """WS-5b: AI per-run budget gate (QG-WS5B, forceable).

    Passes vacuously when no budget is configured or no telemetry in window.
    """
    report = _evaluate_ai_budget_gate_impl(
        program_id=program_id,
        budget_usd_per_run=budget_usd_per_run,
        programs_root=programs_root,
    )
    if not report.results:
        return GateEvaluation("QG-WS5B", True, "AI budget gate passed (n/a).", 0, forceable=True)
    return report.results[0]


def _has_overdue_target(item: WorkItem, today: date) -> bool:
    return _has_overdue_target_impl(item, today)


def _is_terminal(item: WorkItem) -> bool:
    return _is_terminal_impl(item)


def _preview_work_item_ids(item_ids: list[int]) -> str:
    return _preview_work_item_ids_impl(item_ids)


def _has_milestone_risk_linkage(*, milestone, open_risks: tuple[Any, ...]) -> bool:
    return _has_milestone_risk_linkage_impl(milestone=milestone, open_risks=open_risks)


def _coerce_date(value: date | datetime) -> date:
    return _coerce_date_impl(value)


def _coerce_datetime(value: date | datetime) -> datetime:
    return _coerce_datetime_impl(value)

