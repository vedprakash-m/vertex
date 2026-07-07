from __future__ import annotations

import json
import os
import re
import yaml
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from src.ai.blurb_generator import BlurbGenerationError
from src.ai.ai_mode import AIMode, get_ai_mode
from src.ai.client import AIClientError
from src.ai.deployment_fallback import LEGACY_DEPLOYMENT_ALIAS_NOTICE, resolve_ai_deployments_for_feature
from src.ai.edit_learner import load_recent_edit_patterns, summarize_recent_calibration, summarize_recent_confidence_bands, summarize_recent_models, summarize_recent_prompt_version_confidence_bands, summarize_recent_prompt_version_models, summarize_recent_prompt_versions
from src.ai.exec_summary_drafter import ExecSummaryDraftError
from src.ai.llm_trace import AITraceContext
from src.ai.provider import LLMProvider
from src.commands.report_continuity import _continuity_chapter_title as _continuity_chapter_title_impl
from src.commands.report_continuity import _is_continuity_layout, _iter_continuity_ai_sections as _iter_continuity_ai_sections_impl
from src.commands.report_detail import _iter_detail_sections as _iter_detail_sections_impl
from src.core.feedback.calibration_router import load_forecast_calibration_modifier
from src.core.altitude_guard import apply_altitude_guard
from src.core.cascade_detector import DependencyCascade, detect_dependency_cascades
from src.core.chapter_contract_loader import ChapterDefinition
from src.core.config_loader import NarrativeProgramContext, ReportBundle, load_bundle_with_mode
from src.core.coverage_gap import build_coverage_gaps
from src.core.edition_resolver import filter_workstreams, resolve_edition
from src.core.evidence_models import SourceRef, WorkstreamEvidence, extract_icm_ids
from src.core.evidence_conflict_detector import detect_evidence_conflicts
from src.core.evidence_store import load_approved_evidence_by_lane
from src.core.forecast_engine import ETAForecast, forecast_etas
from src.core.knowledge_store import load_program_knowledge
from src.core.models import DeltaSet, DimensionRisk, EditionType, EvidencePacket, ItemDelta, RiskLevel, ScorecardEvidencePacket, Snapshot, WorkItem
from src.core.models_v2 import PersonDirectory, Signal as JournalSignal, Workstream, WorkstreamEvidenceBundle
from src.core.overrides_store import OverridesDocument
from src.core.program_reality import ProgramReality
from src.core.signal_ranking import sort_signals_for_ai_context, populate_top_3_now_candidates
from src.core.signal_review import signal_is_approved_for_evidence, signal_needs_review
from src.core.store_factory import build_program_trajectory_store, build_signal_store_for_program_id
from src.core.summary_store import load_summary
from src.core.trajectory_analyzer import DriftPattern, analyze_trajectories
from src.core.triage import ReadinessAssessment, build_readiness_assessment, is_missing_narrative_content
from src.core.workstream_path_resolver import resolve_workstream_id_loose_longest as _resolve_workstream_id
from src.core.view_models import ScorecardData
from src.core.ado_enrichment import deserialize_trajectory_points, merge_trajectory_points


_REPORT_AI_BLURB_PRIMARY_FALLBACK_ENVS = (
    "VERTEX_AI_DEPLOYMENT",
    "AZURE_OPENAI_DEPLOYMENT",
)
_REPORT_AI_BLURB_BACKUP_FALLBACK_ENVS = (
    "VERTEX_AI_BACKUP_DEPLOYMENT",
)
_REPORT_AI_EXEC_PRIMARY_FALLBACK_ENVS = (
    "VERTEX_EXEC_DEPLOYMENT",
    "VERTEX_AI_DEPLOYMENT",
    "AZURE_OPENAI_DEPLOYMENT",
)
_REPORT_AI_EXEC_BACKUP_FALLBACK_ENVS = (
    "VERTEX_EXEC_BACKUP_DEPLOYMENT",
    "VERTEX_AI_BACKUP_DEPLOYMENT",
)


def _report_ai_budget_per_client(bundle: ReportBundle) -> float:
    deployment_count = len(
        {
            deployment
            for deployment in (
                *_resolve_report_ai_deployments(
                    feature_name="blurb_generator",
                    primary=bundle.config.ai.blurb_deployment,
                    backup=bundle.config.ai.blurb_backup_deployment,
                    primary_fallback_envs=_REPORT_AI_BLURB_PRIMARY_FALLBACK_ENVS,
                    backup_fallback_envs=_REPORT_AI_BLURB_BACKUP_FALLBACK_ENVS,
                ),
                *_resolve_report_ai_deployments(
                    feature_name="exec_summary_drafter",
                    primary=bundle.config.ai.exec_summary_deployment,
                    backup=bundle.config.ai.exec_summary_backup_deployment,
                    primary_fallback_envs=_REPORT_AI_EXEC_PRIMARY_FALLBACK_ENVS,
                    backup_fallback_envs=_REPORT_AI_EXEC_BACKUP_FALLBACK_ENVS,
                ),
            )
            if deployment is not None
        }
    )
    return bundle.config.ai.budget_usd_per_run / max(1, deployment_count)


@dataclass(frozen=True, slots=True)
class _AIGeneratedSection:
    section_id: str
    title: str
    items: tuple[WorkItem, ...]


@dataclass(frozen=True, slots=True)
class _DraftAIContext:
    program_id: str
    programs_root: Path
    workstreams: tuple[Workstream, ...]
    rolling_summaries: dict[str, str]
    approved_signals: tuple[JournalSignal, ...]
    drift_patterns: tuple[DriftPattern, ...]
    dependency_cascades: tuple[DependencyCascade, ...]
    people_directory: tuple[PersonDirectory, ...] = ()
    source_confidence_order: tuple[str, ...] = ()
    as_of: datetime | None = None


@dataclass(frozen=True, slots=True)
class _AISynthesisResult:
    exec_summary_text: str
    workstream_blurbs: dict[str, str]
    workstream_source_footnotes: dict[str, str] = field(default_factory=dict)
    prompt_versions: dict[str, str] = field(default_factory=dict)
    ai_confidences: dict[str, str] = field(default_factory=dict)
    trace_run_id: str | None = None
    warnings: tuple[str, ...] = ()
    ai_calls: int = 0
    ai_cost_usd: float = 0.0
    ai_cost_by_model: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _ReportSignalContext:
    program_id: str
    altitude: str
    workstreams: tuple[Workstream, ...]
    approved_signals: tuple[JournalSignal, ...]
    drift_patterns: tuple[DriftPattern, ...]
    dependency_cascades: tuple[DependencyCascade, ...]
    people_directory: tuple[PersonDirectory, ...] = ()
    source_confidence_order: tuple[str, ...] = ()
    as_of: datetime | None = None
    top_3_now_candidates: tuple[JournalSignal, ...] = ()  # FR-SG-33: signal-priority candidates


@dataclass(frozen=True, slots=True)
class _GuardedReviewEvidence:
    approved_signals: tuple[JournalSignal, ...]
    drift_patterns: tuple[DriftPattern, ...]


def _iter_ai_generated_sections(
    *,
    bundle: ReportBundle,
    edition_type: EditionType,
    items: tuple[WorkItem, ...],
    scorecards: tuple[ScorecardData, ...],
    scorecard_packets: dict[str, dict[str, ScorecardEvidencePacket]],
    overrides_document: OverridesDocument,
    continuity_chapters: tuple[ChapterDefinition, ...],
    iter_detail_sections: Callable[..., tuple[tuple[str, str, Any, ScorecardEvidencePacket, tuple[WorkItem, ...]], ...]] | None = None,
) -> tuple[_AIGeneratedSection, ...]:
    if continuity_chapters or _is_continuity_layout(bundle):
        return _iter_continuity_ai_generated_sections(
            bundle=bundle,
            edition_type=edition_type,
            items=items,
            scorecards=scorecards,
            scorecard_packets=scorecard_packets,
            overrides_document=overrides_document,
            continuity_chapters=continuity_chapters,
        )

    detail_sections = iter_detail_sections or _iter_detail_sections_impl
    return tuple(
        _AIGeneratedSection(
            section_id=section_id,
            title=model.display_name or model.name,
            items=matching_items,
        )
        for _scorecard_name, section_id, model, _packet, matching_items in detail_sections(  # type: ignore[call-arg]
            bundle=bundle,
            items=items,
            scorecards=scorecards,
            scorecard_packets=scorecard_packets,
            overrides_document=overrides_document,
        )
        if matching_items
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
    iter_ai_generated_sections: Callable[..., tuple[_AIGeneratedSection, ...]] | None = None,
) -> tuple[WorkItem, ...]:
    generated_sections = iter_ai_generated_sections or _iter_ai_generated_sections
    visible_sections = tuple(
        section
        for section in generated_sections(
            bundle=bundle,
            edition_type=edition_type,
            items=items,
            scorecards=scorecards,
            scorecard_packets=scorecard_packets,
            overrides_document=overrides_document,
            continuity_chapters=continuity_chapters,
        )
        if section.section_id in visible_section_ids
    )
    if not visible_sections:
        return items

    scoped_item_ids: set[int] = set()
    scoped_items: list[WorkItem] = []
    for section in visible_sections:
        for item in section.items:
            if item.id in scoped_item_ids:
                continue
            scoped_item_ids.add(item.id)
            scoped_items.append(item)
    return tuple(scoped_items) if scoped_items else items


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
    iter_ai_generated_sections: Callable[..., tuple[_AIGeneratedSection, ...]] | None = None,
) -> tuple[int, ...]:
    generated_sections = iter_ai_generated_sections or _iter_ai_generated_sections
    covered_item_ids: set[int] = set()
    chapter_surface_active = bool(continuity_chapters) or _is_continuity_layout(bundle)
    for section in generated_sections(
        bundle=bundle,
        edition_type=edition_type,
        items=items,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        overrides_document=overrides_document,
        continuity_chapters=continuity_chapters,
    ):
        if section.section_id not in visible_section_ids:
            continue
        narrative_key = (
            f"chapter_{section.section_id}.md"
            if chapter_surface_active
            else f"ws_{section.section_id}.md"
        )
        if is_missing_narrative_content(loaded_narratives.get(narrative_key, "")):
            continue
        covered_item_ids.update(item.id for item in section.items)
    return tuple(sorted(covered_item_ids))


def _iter_continuity_ai_generated_sections(
    *,
    bundle: ReportBundle,
    edition_type: EditionType,
    items: tuple[WorkItem, ...],
    scorecards: tuple[ScorecardData, ...],
    scorecard_packets: dict[str, dict[str, ScorecardEvidencePacket]],
    overrides_document: OverridesDocument,
    continuity_chapters: tuple[ChapterDefinition, ...],
) -> tuple[_AIGeneratedSection, ...]:
    if continuity_chapters and not _is_continuity_layout(bundle):
        assert bundle.chapter_contract is not None
        item_lookup = {item.id: item for item in items}
        model_lookup = {
            (scorecard.scorecard_name, dimension.name): dimension
            for scorecard in scorecards
            for dimension in scorecard.dimensions
        }
        packet_lookup = {
            (scorecard_name, dimension_name): packet
            for scorecard_name, packets in scorecard_packets.items()
            for dimension_name, packet in packets.items()
        }
        generated_sections: list[_AIGeneratedSection] = []
        for chapter in continuity_chapters:
            chapter_items: list[WorkItem] = []
            seen_item_ids: set[int] = set()
            for dimension_id in chapter.dimensions:
                binding = bundle.chapter_contract.resolve_dimension(dimension_id)
                if binding is None:
                    continue
                model = model_lookup.get(binding)
                packet = packet_lookup.get(binding)
                if model is None or packet is None or packet.total_items == 0:
                    continue
                if model.risk == RiskLevel.DONE:
                    continue
                if model.risk == RiskLevel.LOW and not chapter.include_low_risk_dimensions:
                    continue
                for item_id in packet.item_ids:
                    if item_id in seen_item_ids or item_id not in item_lookup:
                        continue
                    seen_item_ids.add(item_id)
                    chapter_items.append(item_lookup[item_id])
            if chapter_items:
                generated_sections.append(
                    _AIGeneratedSection(
                        section_id=chapter.id,
                        title=_continuity_chapter_title_impl(chapter, overrides_document),
                        items=tuple(chapter_items),
                    )
                )
        return tuple(generated_sections)

    sections = _iter_continuity_ai_sections_impl(
        bundle=bundle,
        edition_type=edition_type,
        items=items,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        chapter_title_builder=lambda chapter: _continuity_chapter_title_impl(chapter, overrides_document),
    )
    return tuple(
        _AIGeneratedSection(
            section_id=section.section_id,
            title=section.title,
            items=section.items,
        )
        for section in sections
    )


def _relevant_item_deltas(deltas: DeltaSet, item_ids: set[int]) -> tuple[ItemDelta, ...]:
    return tuple(
        delta
        for delta_group in (
            deltas.new_items,
            deltas.closed_items,
            deltas.risk_changes,
            deltas.eta_changes,
            getattr(deltas, "owner_changes", ()),
        )
        for delta in delta_group
        if delta.work_item_id in item_ids
    )


def _write_top3_candidates(
    edition_name: str,
    candidates: tuple[JournalSignal, ...],
    *,
    reports_root: Path,
) -> None:
    """FR-SG-33: Write top_3_now signal candidates to reports/<edition>/signal_top3_candidates.yaml."""
    edition_dir = reports_root / edition_name
    edition_dir.mkdir(parents=True, exist_ok=True)
    out_path = edition_dir / "signal_top3_candidates.yaml"
    payload = [
        {
            "id": s.id,
            "source": s.source,
            "text": s.text[:200],
            "timestamp": s.timestamp.isoformat(),
        }
        for s in candidates
    ]
    out_path.write_text(yaml.dump({"candidates": payload}, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _load_report_signal_context(
    *,
    edition_name: str,
    bundle: ReportBundle,
    items: tuple[WorkItem, ...],
    as_of: datetime,
    previous_snapshot: Snapshot | None,
    reports_root: Path,
    item_trajectory_points: Callable[..., tuple] | None = None,
) -> _ReportSignalContext | None:
    if item_trajectory_points is None:
        raise ValueError("Item trajectory loader must be provided.")

    programs_root = reports_root.parent / "programs"
    load_result = load_bundle_with_mode(
        edition_name,
        reports_root=reports_root,
        programs_root=programs_root,
    )
    if load_result.mode != "v2":
        return None

    resolved = resolve_edition(
        edition_name,
        programs_root=programs_root,
    )
    if resolved is None:
        return None

    active_workstreams = filter_workstreams(resolved.workstreams, resolved.edition.workstream_filter)
    window_start = (
        previous_snapshot.ado_data_as_of
        if previous_snapshot is not None
        else as_of - timedelta(days=bundle.config.ado.date_window_days)
    )
    signal_store = build_signal_store_for_program_id(resolved.program.id, programs_root=programs_root)
    journal_signals = signal_store.read(
        resolved.program.id,
        start=window_start,
        end=as_of,
    )
    review_states = signal_store.read_reviews(resolved.program.id)
    approved_signals = tuple(
        signal
        for signal in journal_signals
        if signal_is_approved_for_evidence(signal, review_states)
    )
    knowledge = load_program_knowledge(resolved.program.id, programs_root=programs_root)
    trajectories = {
        item.id: item_trajectory_points(item, program=resolved.program, programs_root=programs_root)
        for item in items
    }
    populated_trajectories = {
        work_item_id: points
        for work_item_id, points in trajectories.items()
        if points
    }
    drift_patterns = analyze_trajectories(populated_trajectories, as_of=as_of.date()) if populated_trajectories else ()
    dependency_cascades = detect_dependency_cascades(
        dependencies=tuple(
            a.record for a in ProgramReality.load(
                resolved.program.id,
                programs_root=programs_root,
            ).dependencies()
        ),
        signals=approved_signals,
        drift_patterns=drift_patterns,
        items=items,
        scorecards=resolved.scorecards,
        workstreams=active_workstreams,
    )
    # FR-SG-33: compute signal-priority top_3_now candidates
    top_3_now_candidates = populate_top_3_now_candidates(approved_signals, {}, as_of=as_of)
    # Write candidates to disk so reviewers can inspect them
    _write_top3_candidates(edition_name, top_3_now_candidates, reports_root=reports_root)
    return _ReportSignalContext(
        program_id=resolved.program.id,
        altitude=resolved.edition.altitude,
        workstreams=active_workstreams,
        approved_signals=approved_signals,
        drift_patterns=drift_patterns,
        dependency_cascades=dependency_cascades,
        people_directory=knowledge.people_directory,
        source_confidence_order=getattr(resolved.program, "source_confidence_order", ()),
        as_of=as_of,
        top_3_now_candidates=top_3_now_candidates,
    )


def _load_eta_forecasts(
    *,
    edition_name: str,
    items: tuple[WorkItem, ...],
    as_of: datetime,
    reports_root: Path,
    item_trajectory_points: Callable[..., tuple] | None = None,
) -> dict[int, ETAForecast]:
    if item_trajectory_points is None:
        raise ValueError("Item trajectory loader must be provided.")

    programs_root = reports_root.parent / "programs"
    load_result = load_bundle_with_mode(
        edition_name,
        reports_root=reports_root,
        programs_root=programs_root,
    )
    if load_result.mode != "v2":
        return {}

    resolved = resolve_edition(
        edition_name,
        programs_root=programs_root,
    )
    if resolved is None:
        return {}

    trajectories = {
        item.id: item_trajectory_points(item, program=resolved.program, programs_root=programs_root)
        for item in items
    }
    populated_trajectories = {
        work_item_id: points
        for work_item_id, points in trajectories.items()
        if points
    }
    if not populated_trajectories:
        return {}

    drift_patterns = analyze_trajectories(populated_trajectories, as_of=as_of.date())
    calibration_modifier = load_forecast_calibration_modifier(
        resolved.program.id,
        programs_root=programs_root,
    )
    return forecast_etas(
        populated_trajectories,
        drift_patterns,
        calibration_adjustments=_build_forecast_calibration_adjustments(
            items=items,
            workstreams=resolved.workstreams,
            calibration_modifier=calibration_modifier,
        ),
        as_of=as_of.date(),
    )


def _build_forecast_calibration_adjustments(
    *,
    items: tuple[WorkItem, ...],
    workstreams: tuple[Workstream, ...],
    calibration_modifier,
) -> dict[int, float]:
    if calibration_modifier is None:
        return {}

    adjustments: dict[int, float] = {}
    for item in items:
        adjustment = 0.0
        workstream_id = _resolve_workstream_id(item.area_path, workstreams)
        if workstream_id is not None:
            adjustment += calibration_modifier.workstream_modifiers.get(workstream_id, 0.0)
        owner_alias = _normalize_owner_alias(item.assigned_to_email or item.assigned_to)
        if owner_alias is not None:
            adjustment += calibration_modifier.dri_modifiers.get(owner_alias, 0.0)
        if adjustment != 0.0:
            adjustments[item.id] = round(adjustment, 2)
    return adjustments


def _normalize_owner_alias(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    if "@" in normalized:
        normalized = normalized.split("@", 1)[0]
    return normalized or None


def _item_trajectory_points(item: WorkItem, *, program: Any, programs_root: Path) -> tuple:
    trajectory_store = build_program_trajectory_store(program, programs_root=programs_root)
    stored = trajectory_store.read(program.id, item.id)
    analytics = deserialize_trajectory_points(item.custom_fields.get("analytics_history"))
    return merge_trajectory_points(stored, analytics)


def _load_guarded_review_evidence(
    *,
    edition_name: str,
    bundle: ReportBundle,
    items: tuple[WorkItem, ...],
    as_of: datetime,
    previous_snapshot: Snapshot | None,
    reports_root: Path,
    load_report_signal_context: Callable[..., _ReportSignalContext | None] | None = None,
) -> _GuardedReviewEvidence:
    if load_report_signal_context is None:
        raise ValueError("Report signal context loader must be provided.")

    signal_context = load_report_signal_context(
        edition_name=edition_name,
        bundle=bundle,
        items=items,
        as_of=as_of,
        previous_snapshot=previous_snapshot,
        reports_root=reports_root,
    )
    if signal_context is None:
        return _GuardedReviewEvidence(approved_signals=(), drift_patterns=())

    altitude = signal_context.altitude
    escalation_item_id = items[0].id if altitude.strip().lower() == "escalation" and len(items) == 1 else None
    guarded = apply_altitude_guard(
        altitude=altitude,
        signals=signal_context.approved_signals,
        drift_patterns=signal_context.drift_patterns,
        escalation_item_id=escalation_item_id,
    )
    return _GuardedReviewEvidence(
        approved_signals=guarded.signals,
        drift_patterns=guarded.drift_patterns,
    )


def _build_draft_readiness(
    *,
    edition_name: str,
    qg_report: Any,
    items: tuple[WorkItem, ...],
    covered_item_ids: tuple[int, ...] = (),
    dimension_risks: tuple[DimensionRisk, ...],
    visible_section_ids: set[str] | tuple[str, ...],
    loaded_narratives: dict[str, str],
    as_of: datetime,
    reports_root: Path,
    is_continuity: bool,
) -> ReadinessAssessment:
    programs_root = reports_root.parent / "programs"
    load_result = load_bundle_with_mode(
        edition_name,
        reports_root=reports_root,
        programs_root=programs_root,
    )
    approved_signals: tuple[JournalSignal, ...] = ()
    unreviewed_signals: tuple[JournalSignal, ...] = ()
    if load_result.mode == "v2":
        resolved = resolve_edition(
            edition_name,
            programs_root=programs_root,
        )
        if resolved is not None:
            signal_window_start = as_of - timedelta(days=load_result.bundle.config.ado.date_window_days)
            signal_store = build_signal_store_for_program_id(resolved.program.id, programs_root=programs_root)
            journal_signals = signal_store.read(
                resolved.program.id,
                start=signal_window_start,
                end=as_of,
            )
            review_states = signal_store.read_reviews(resolved.program.id)
            approved_signals = tuple(
                signal
                for signal in journal_signals
                if signal_is_approved_for_evidence(signal, review_states)
            )
            unreviewed_signals = tuple(
                signal
                for signal in journal_signals
                if signal_needs_review(signal, review_states)
            )

    coverage_gaps = build_coverage_gaps(
        items,
        approved_signals=approved_signals,
        narratives=loaded_narratives,
        as_of=as_of,
        covered_item_ids=covered_item_ids,
    )
    resolved_visible_section_ids = tuple(visible_section_ids)
    if not resolved_visible_section_ids and is_continuity:
        resolved_visible_section_ids = tuple(
            sorted(
                name.removeprefix("chapter_").removesuffix(".md")
                for name in loaded_narratives
                if name.startswith("chapter_") and name.endswith(".md")
            )
        )
    missing_narrative_count = sum(
        1
        for section_id in resolved_visible_section_ids
        if is_missing_narrative_content(
            loaded_narratives.get(
                (f"chapter_{section_id}.md" if is_continuity else f"ws_{section_id}.md"),
                "",
            )
        )
    )
    missing_override_count = sum(1 for dimension in dimension_risks if dimension.risk == RiskLevel.UNKNOWN)
    return build_readiness_assessment(
        quality_gate_report=qg_report,
        unreviewed_signal_count=len(unreviewed_signals),
        missing_narrative_count=missing_narrative_count,
        total_narrative_count=len(resolved_visible_section_ids),
        missing_override_count=missing_override_count,
        total_override_count=len(dimension_risks),
        coverage_gap_count=len(coverage_gaps),
    )


def _load_draft_ai_context(
    *,
    edition_name: str,
    bundle: ReportBundle,
    items: tuple[WorkItem, ...],
    as_of: datetime,
    previous_snapshot: Snapshot | None,
    reports_root: Path,
    signal_context: Any | None = None,
    load_report_signal_context: Callable[..., Any] | None = None,
) -> _DraftAIContext | None:
    if not bundle.config.ai.enabled:
        return None

    if signal_context is None:
        if load_report_signal_context is None:
            raise ValueError("Report signal context loader must be provided.")
        signal_context = load_report_signal_context(
            edition_name=edition_name,
            bundle=bundle,
            items=items,
            as_of=as_of,
            previous_snapshot=previous_snapshot,
            reports_root=reports_root,
        )
    if signal_context is None:
        return None

    programs_root = reports_root.parent / "programs"
    rolling_summaries = {
        workstream.id: summary.text
        for workstream in signal_context.workstreams
        if (summary := load_summary(signal_context.program_id, workstream.id, programs_root=programs_root)) is not None and summary.text.strip()
    }
    return _DraftAIContext(
        program_id=signal_context.program_id,
        programs_root=programs_root,
        workstreams=signal_context.workstreams,
        rolling_summaries=rolling_summaries,
        approved_signals=signal_context.approved_signals,
        drift_patterns=signal_context.drift_patterns,
        dependency_cascades=signal_context.dependency_cascades,
        people_directory=signal_context.people_directory,
        source_confidence_order=signal_context.source_confidence_order,
        as_of=signal_context.as_of,
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
    create_ai_client: Callable[..., LLMProvider] | None = None,
    draft_exec_summary_runner: Callable[..., Any] | None = None,
    generate_workstream_blurb_runner: Callable[..., Any] | None = None,
    iter_ai_generated_sections: Callable[..., tuple[_AIGeneratedSection, ...]] | None = None,
    relevant_item_deltas: Callable[[DeltaSet, set[int]], tuple[ItemDelta, ...]] | None = None,
) -> _AISynthesisResult:
    render_exec_summary_text = current_exec_summary_text
    render_workstream_blurbs = dict(current_workstream_blurbs)
    if ai_context is None or edition_type in {EditionType.DECK, EditionType.LOOKBACK}:
        return _AISynthesisResult(
            exec_summary_text=render_exec_summary_text,
            workstream_blurbs=render_workstream_blurbs,
        )
    if get_ai_mode() == AIMode.DISABLED:
        return _AISynthesisResult(
            exec_summary_text=render_exec_summary_text,
            workstream_blurbs=render_workstream_blurbs,
            warnings=("AI synthesis disabled by --no-ai / AIMode.DISABLED.",),
        )
    if create_ai_client is None or draft_exec_summary_runner is None or generate_workstream_blurb_runner is None:
        raise ValueError("AI synthesis seams must be provided.")

    generated_sections = iter_ai_generated_sections or _iter_ai_generated_sections
    relevant_deltas = relevant_item_deltas or _relevant_item_deltas

    warnings: list[str] = []
    client_cache: dict[_ReportAIClientCacheKey, LLMProvider | None] = {}
    prompt_versions: dict[str, str] = {}
    ai_confidences: dict[str, str] = {}
    trace_context = AITraceContext(
        edition=edition_name,
        run_id=_build_report_ai_trace_run_id(
            edition_name=edition_name,
            issue_number=issue_number,
            started_at=started_at,
        ),
        caller="src.commands.report._synthesize_v2_ai_content",
        metadata={"run_budget_usd": bundle.config.ai.budget_usd_per_run},
    )
    budget_per_client = _report_ai_budget_per_client(bundle)

    if not loaded_exec_summary_text.strip():
        exec_draft = _run_report_ai_with_fallback(
            deployments=_resolve_report_ai_deployments(
                feature_name="exec_summary_drafter",
                primary=bundle.config.ai.exec_summary_deployment,
                backup=bundle.config.ai.exec_summary_backup_deployment,
                primary_fallback_envs=_REPORT_AI_EXEC_PRIMARY_FALLBACK_ENVS,
                backup_fallback_envs=_REPORT_AI_EXEC_BACKUP_FALLBACK_ENVS,
            ),
            temperature=(bundle.config.ai.temperature or 0.2),
            budget_usd=budget_per_client,
            client_cache=client_cache,
            trace_context=_with_trace_metadata(
                trace_context,
                issue_number=issue_number,
                task_type="exec_summary",
                section_id="exec_summary",
            ),
            warnings=warnings,
            label="exec summary",
            create_ai_client=create_ai_client,
            runner=lambda client: draft_exec_summary_runner(
                client=client,
                items=items,
                deltas=deltas,
                editorial_rules=bundle.editorial_rules,
                edition_type=edition_type,
                program_context=ai_program_context,
                supplemental_context=_exec_ai_context_lines(
                    ai_program_context,
                    ai_context,
                ),
            ),
        )
        if exec_draft is not None and exec_draft.text.strip():
            render_exec_summary_text = exec_draft.text.strip()
            prompt_versions["exec_summary"] = exec_draft.prompt_version
            ai_confidences["exec_summary"] = exec_draft.ai_confidence.value
            warnings.append("AI filled the empty exec summary from rolling summaries and approved signals.")

    seeded_sections = 0
    workstream_source_footnotes: dict[str, str] = {}
    # P4-1: load approval-gated M365 evidence once (§17.8 Option A). approved_signal_ids
    # is the set of journal signals already approved for evidence — used as the gate
    # for evidence_store records that carry backing_signal_ids.
    approved_signal_ids = {s.id for s in ai_context.approved_signals}
    m365_evidence_by_lane = load_approved_evidence_by_lane(
        ai_context.program_id,
        programs_root=ai_context.programs_root,
        approved_signal_ids=approved_signal_ids,
    )
    for section in generated_sections(
        bundle=bundle,
        edition_type=edition_type,
        items=items,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        overrides_document=overrides_document,
        continuity_chapters=continuity_chapters,
    ):
        if section.section_id not in visible_section_ids:
            continue
        if render_workstream_blurbs.get(section.section_id, "").strip():
            continue
        section_bundle = _build_section_evidence_bundle(
            lane_id=section.section_id,
            ai_context=ai_context,
            m365_evidence_by_lane=m365_evidence_by_lane,
            item_ids={item.id for item in section.items},
        )
        blurb = _run_report_ai_with_fallback(
            deployments=_resolve_report_ai_deployments(
                feature_name="blurb_generator",
                primary=bundle.config.ai.blurb_deployment,
                backup=bundle.config.ai.blurb_backup_deployment,
                primary_fallback_envs=_REPORT_AI_BLURB_PRIMARY_FALLBACK_ENVS,
                backup_fallback_envs=_REPORT_AI_BLURB_BACKUP_FALLBACK_ENVS,
            ),
            temperature=(bundle.config.ai.temperature or 0.2),
            budget_usd=budget_per_client,
            client_cache=client_cache,
            trace_context=_with_trace_metadata(
                trace_context,
                issue_number=issue_number,
                task_type="workstream_blurb",
                section_id=section.section_id,
            ),
            warnings=warnings,
            label=f"section {section.section_id}",
            create_ai_client=create_ai_client,
            runner=lambda client: generate_workstream_blurb_runner(
                client=client,
                workstream_name=section.title,
                items=section.items,
                evidence_by_item=evidence_by_item,
                deltas=relevant_deltas(deltas, {item.id for item in section.items}),
                editorial_rules=bundle.editorial_rules,
                edition_type=edition_type,
                program_context=ai_program_context,
                supplemental_context=_section_ai_context_lines(
                    section.items,
                    ai_program_context,
                    ai_context,
                ),
                workstream_evidence_bundle=section_bundle,
            ),
        )
        if blurb is None or not blurb.text.strip():
            continue
        render_workstream_blurbs[section.section_id] = blurb.text.strip()
        source_footnote = _build_workstream_source_footnote(section_bundle, blurb.cited_source_refs)
        if source_footnote:
            workstream_source_footnotes[section.section_id] = source_footnote
        prompt_versions[section.section_id] = blurb.prompt_version
        ai_confidences[section.section_id] = blurb.ai_confidence.value
        seeded_sections += 1

    if seeded_sections:
        warnings.append(
            f"AI filled {seeded_sections} empty narrative section(s) from rolling summaries and approved signals."
        )

    ai_calls, ai_cost_usd, ai_cost_by_model = _report_ai_usage(client_cache)
    return _AISynthesisResult(
        exec_summary_text=render_exec_summary_text,
        workstream_blurbs=render_workstream_blurbs,
        workstream_source_footnotes=workstream_source_footnotes,
        prompt_versions=prompt_versions,
        ai_confidences=ai_confidences,
        trace_run_id=trace_context.run_id,
        warnings=tuple(warnings),
        ai_calls=ai_calls,
        ai_cost_usd=ai_cost_usd,
        ai_cost_by_model=ai_cost_by_model,
    )


def _section_ai_context_lines(
    section_items: tuple[WorkItem, ...],
    program_context: NarrativeProgramContext | None,
    ai_context: _DraftAIContext,
) -> tuple[str, ...]:
    item_ids = {item.id for item in section_items}
    workstream_ids = _matching_program_workstream_ids(section_items, ai_context.workstreams)
    lines: list[str] = []
    lines.extend(
        f"Rolling summary [{workstream_id}]: {ai_context.rolling_summaries[workstream_id]}"
        for workstream_id in workstream_ids
        if workstream_id in ai_context.rolling_summaries
    )
    lines.extend(_leadership_reader_context_lines(program_context))
    lines.extend(
        _edit_pattern_context_lines(
            ai_context.program_id,
            workstream_ids=workstream_ids,
            programs_root=ai_context.programs_root,
        )
    )
    lines.extend(
        _feedback_context_lines(
            ai_context.approved_signals,
            item_ids=item_ids,
            workstream_ids=workstream_ids,
            limit=2,
            people_directory=ai_context.people_directory,
            source_confidence_order=ai_context.source_confidence_order,
            as_of=ai_context.as_of,
        )
    )
    lines.extend(
        _signal_context_lines(
            ai_context.approved_signals,
            item_ids=item_ids,
            workstream_ids=workstream_ids,
            limit=5,
            people_directory=ai_context.people_directory,
            source_confidence_order=ai_context.source_confidence_order,
            as_of=ai_context.as_of,
        )
    )
    lines.extend(_drift_context_lines(ai_context.drift_patterns, item_ids=item_ids, limit=4))
    lines.extend(_cascade_context_lines(ai_context.dependency_cascades, workstream_ids=workstream_ids, limit=3))
    return tuple(lines)


def _build_section_evidence_bundle(
    *,
    lane_id: str,
    ai_context: _DraftAIContext,
    m365_evidence_by_lane: dict,
    item_ids: set[int] | None = None,
) -> WorkstreamEvidenceBundle:
    """P4-1: assemble the unified evidence bundle for one workstream section.

    Partitions the already-approved journal signals for this lane by source into the
    ADO / Kusto / IcM / ADO-comment buckets, and attaches the approval-gated M365
    evidence (loaded once before the section loop and passed in via
    ``m365_evidence_by_lane``). ``conflicts`` is populated by P4-10
    (``detect_evidence_conflicts``) — cross-source disagreements surfaced as
    ``RealityConflict`` items rather than silently merged.
    """
    ado_signals: list[JournalSignal] = []
    kusto_metrics: list[JournalSignal] = []
    icm_blockers: list[JournalSignal] = []
    ado_comments: list[JournalSignal] = []
    reference_signals: list[JournalSignal] = []
    for signal in ai_context.approved_signals:
        if signal.workstream_id != lane_id:
            continue
        source = (signal.source or "").lower()
        if source == "ado/comment":
            ado_comments.append(signal)
        elif source == "engms" or source.startswith("sharepoint"):
            reference_signals.append(signal)
        elif source.startswith("kusto"):
            kusto_metrics.append(signal)
        elif source.startswith("icm"):
            icm_blockers.append(signal)
        elif source.startswith("ado"):
            ado_signals.append(signal)
    augmented_evidence = _augment_evidence_with_quantitative_signals(
        m365_evidence_by_lane.get(lane_id),
        icm_blockers=icm_blockers,
        kusto_metrics=kusto_metrics,
    )
    conflicts = detect_evidence_conflicts(
        m365_evidence=augmented_evidence,
        icm_blockers=tuple(icm_blockers),
        kusto_metrics=tuple(kusto_metrics),
    )
    corroboration_notes: tuple[str, ...] = ()
    if augmented_evidence is not None:
        augmented_evidence, corroboration_notes = _boost_evidence_confidence_from_corroboration(
            augmented_evidence,
            ado_signals=ado_signals,
            ado_comments=ado_comments,
            kusto_metrics=kusto_metrics,
            icm_blockers=icm_blockers,
        )
    freshness_by_source = _bundle_freshness_by_source(
        ado_signals=ado_signals,
        ado_comments=ado_comments,
        kusto_metrics=kusto_metrics,
        icm_blockers=icm_blockers,
        reference_signals=reference_signals,
        m365_evidence=augmented_evidence,
    )
    lookback_intelligence = _bundle_lookback_intelligence(
        drift_patterns=ai_context.drift_patterns,
        item_ids=item_ids or set(),
    )
    return WorkstreamEvidenceBundle(
        lane_id=lane_id,
        ado_signals=tuple(ado_signals),
        kusto_metrics=tuple(kusto_metrics),
        icm_blockers=tuple(icm_blockers),
        ado_comments=tuple(ado_comments),
        reference_signals=tuple(reference_signals),
        m365_evidence=augmented_evidence,
        conflicts=conflicts,
        as_of=ai_context.as_of,
        lookback_intelligence=lookback_intelligence,
        freshness_by_source=freshness_by_source,
        corroboration_notes=corroboration_notes,
    )


def _augment_evidence_with_quantitative_signals(
    evidence: WorkstreamEvidence | None,
    *,
    icm_blockers: list[JournalSignal],
    kusto_metrics: list[JournalSignal],
) -> WorkstreamEvidence | None:
    """P4-8 (§8.5) / P4-9 (§8.6): fold IcM incident IDs into ``blocking_items`` and
    Kusto metric text into ``narrative_summary`` on the transient bundle evidence.

    The bundle evidence is a read-time construct (not persisted), so augmenting it
    does not alter the stored record. Returns ``evidence`` unchanged when it is None
    or when there are no IcM/Kusto signals to fold. IcM IDs already present in
    ``blocking_items`` are not duplicated.
    """
    if evidence is None:
        return None
    existing = {str(item) for item in evidence.blocking_items}
    new_icm: list[str] = []
    for sig in icm_blockers:
        meta = sig.metadata if isinstance(sig.metadata, dict) else {}
        incident_id = meta.get("incident_id")
        if not incident_id:
            blob = " ".join(sig.entity_refs) + " " + (sig.text or "")
            ids = extract_icm_ids(blob)
            incident_id = ids[0] if ids else None
        if not incident_id:
            continue
        token = f"IcM:{incident_id}"
        if token not in existing:
            new_icm.append(token)
            existing.add(token)
    kusto_lines = [
        f"Kusto: {(s.text or '').strip()}"
        for s in kusto_metrics
        if (s.text or "").strip()
    ]
    if not new_icm and not kusto_lines:
        return evidence
    augmented_blocking = tuple(list(evidence.blocking_items) + new_icm)
    base_narr = evidence.narrative_summary or ""
    augmented_narr = (base_narr + " | " + " | ".join(kusto_lines)) if kusto_lines else base_narr
    return replace(
        evidence,
        blocking_items=augmented_blocking,
        narrative_summary=augmented_narr,
    )


def _bundle_freshness_by_source(
    *,
    ado_signals: list[JournalSignal],
    ado_comments: list[JournalSignal],
    kusto_metrics: list[JournalSignal],
    icm_blockers: list[JournalSignal],
    reference_signals: list[JournalSignal],
    m365_evidence: WorkstreamEvidence | None,
) -> dict[str, datetime]:
    """Latest timestamp per source family for prompt/render freshness context."""
    freshness: dict[str, datetime] = {}

    def _record(source_key: str, timestamp: datetime | None) -> None:
        if timestamp is None:
            return
        current = freshness.get(source_key)
        if current is None or timestamp > current:
            freshness[source_key] = timestamp

    for signal in ado_signals:
        _record("ado", signal.timestamp)
    for signal in ado_comments:
        _record("ado_comment", signal.timestamp)
    for signal in kusto_metrics:
        _record("kusto", signal.timestamp)
    for signal in icm_blockers:
        _record("icm", signal.timestamp)
    for signal in reference_signals:
        _record("reference_doc", signal.timestamp)
    if m365_evidence is not None:
        _record("m365", m365_evidence.synthesized_at)
    return freshness


def _bundle_lookback_intelligence(
    *,
    drift_patterns: tuple[DriftPattern, ...],
    item_ids: set[int],
    limit: int = 3,
) -> tuple[str, ...]:
    """Compact trajectory / lookback context for the current workstream section."""
    if not item_ids:
        return ()
    relevant = [
        pattern
        for pattern in drift_patterns
        if pattern.work_item_id in item_ids
    ]
    relevant.sort(
        key=lambda pattern: (
            {"high": 0, "medium": 1, "low": 2}.get(pattern.severity, 3),
            pattern.work_item_id,
            pattern.pattern,
        )
    )
    return tuple(
        f"WI#{pattern.work_item_id} {pattern.pattern} ({pattern.severity}): {pattern.detail}"
        for pattern in relevant[:limit]
    )


def _build_workstream_source_footnote(
    bundle: WorkstreamEvidenceBundle | None,
    cited_source_refs: tuple[SourceRef, ...],
) -> str | None:
    """Render a concise provenance footnote for AI-seeded blurbs with M365 evidence."""
    if bundle is None or not cited_source_refs:
        return None

    entries: list[str] = []
    seen: set[tuple[str, str | None]] = set()

    def _append(label: str, source_date: object) -> None:
        normalized_label = " ".join(label.split()).strip()
        if not normalized_label:
            return
        date_label = source_date.isoformat() if source_date is not None and hasattr(source_date, "isoformat") else None
        key = (normalized_label.casefold(), date_label)
        if key in seen:
            return
        seen.add(key)
        entries.append(f"{normalized_label} ({date_label})" if date_label else normalized_label)

    def _append_source_ref(source_ref: SourceRef) -> None:
        """SP3-7: Format SharePoint/LT deck source_refs with spec-required attribution format."""
        source_type = getattr(source_ref, "source_type", None)
        date_label = source_ref.source_date.isoformat() if source_ref.source_date else None
        if source_type == "lt_deck":
            label = f"Per LT deck, {date_label}" if date_label else "Per LT deck"
        elif source_type in ("sharepoint_docx", "sharepoint_pptx", "sharepoint_xlsx"):
            desc = source_ref.description or "SharePoint doc"
            label = f"Per {desc}, {date_label}" if date_label else f"Per {desc}"
        else:
            _append(source_ref.description, source_ref.source_date)
            return
        normalized = " ".join(label.split()).strip()
        key = (normalized.casefold(), date_label)
        if key not in seen:
            seen.add(key)
            entries.append(f"[{normalized}]")

    ado_date = bundle.freshness_by_source.get("ado") or bundle.freshness_by_source.get("ado_comment")
    if ado_date is not None:
        _append("ADO tracking", ado_date.date())
    for source_ref in cited_source_refs:
        _append_source_ref(source_ref)
    kusto_date = bundle.freshness_by_source.get("kusto")
    if kusto_date is not None:
        _append("Kusto telemetry", kusto_date.date())
    icm_date = bundle.freshness_by_source.get("icm")
    if icm_date is not None:
        _append("IcM incidents", icm_date.date())

    if not entries:
        return None
    return f"Signal sources: {'; '.join(entries)}."


def _boost_evidence_confidence_from_corroboration(
    evidence: WorkstreamEvidence,
    *,
    ado_signals: list[JournalSignal],
    ado_comments: list[JournalSignal],
    kusto_metrics: list[JournalSignal],
    icm_blockers: list[JournalSignal],
) -> tuple[WorkstreamEvidence, tuple[str, ...]]:
    """P4-27: raise transient evidence confidence when independent sources agree."""
    family_tags: dict[str, set[str]] = {
        "m365": _fact_tags_for_m365_evidence(evidence),
        "ado": _fact_tags_for_signal_texts((*ado_signals, *ado_comments)),
        "kusto": _fact_tags_for_signal_texts(kusto_metrics),
        "icm": {"blocked"} if icm_blockers else set(),
    }
    corroborated_notes: list[str] = []
    boosted_confidence = evidence.confidence

    for tag in ("blocked", "at_risk", "on_track"):
        agreeing_families = tuple(
            family
            for family, tags in family_tags.items()
            if tag in tags
        )
        if len(agreeing_families) < 2:
            continue
        boost = min(0.10 * (len(agreeing_families) - 1), 0.30)
        candidate_confidence = round(min(0.95, boosted_confidence + boost), 2)
        if candidate_confidence <= boosted_confidence:
            continue
        corroborated_notes.append(
            f"{tag} agreement across {', '.join(agreeing_families)} raised confidence to {candidate_confidence:.2f}"
        )
        boosted_confidence = candidate_confidence

    if boosted_confidence == evidence.confidence:
        return evidence, ()
    return replace(evidence, confidence=boosted_confidence), tuple(corroborated_notes)


def _fact_tags_for_m365_evidence(evidence: WorkstreamEvidence) -> set[str]:
    text = " ".join(
        part
        for part in (
            evidence.narrative_summary,
            " ".join(evidence.raw_excerpts),
            " ".join(evidence.blocking_items),
        )
        if part
    )
    tags = _fact_tags_from_text(text)
    if evidence.blocking_items:
        tags.add("blocked")
    if getattr(evidence.risk_level, "value", "") in {"high", "medium", "blocked"}:
        tags.add("at_risk")
    return tags


def _fact_tags_for_signal_texts(signals: tuple[JournalSignal, ...] | list[JournalSignal]) -> set[str]:
    combined = " ".join((signal.text or "").strip() for signal in signals if (signal.text or "").strip())
    return _fact_tags_from_text(combined)


def _fact_tags_from_text(text: str) -> set[str]:
    lowered = text.lower()
    tags: set[str] = set()
    if re.search(r"\b(blocked|blocker|blocking|slip|slipped|incident|sev)\b", lowered):
        tags.add("blocked")
    if re.search(r"\b(at risk|risk|delayed|regression|regressed|concern|urgent)\b", lowered):
        tags.add("at_risk")
    if re.search(r"\b(on track|healthy|green|complete|completed|ready)\b", lowered):
        tags.add("on_track")
    return tags


def _exec_ai_context_lines(
    program_context: NarrativeProgramContext | None,
    ai_context: _DraftAIContext,
) -> tuple[str, ...]:
    lines = [
        f"Rolling summary [{workstream_id}]: {summary}"
        for workstream_id, summary in sorted(ai_context.rolling_summaries.items())
        if summary.strip()
    ]
    exec_summary = next(
        (
            summary
            for summary in summarize_recent_calibration(ai_context.program_id, programs_root=ai_context.programs_root)
            if summary.task_type == "exec_summary"
        ),
        None,
    )
    if exec_summary is not None:
        lines.append(
            "Recent calibration [exec_summary]: "
            f"score={exec_summary.calibration_score:.2f}; "
            f"avg_override={exec_summary.average_override_magnitude:.2f}; "
            f"samples={exec_summary.sample_count}."
        )
    lines.extend(
        _confidence_band_context_lines(
            ai_context.program_id,
            task_type="exec_summary",
            programs_root=ai_context.programs_root,
        )
    )
    lines.extend(
        _prompt_version_confidence_context_lines(
            ai_context.program_id,
            task_type="exec_summary",
            programs_root=ai_context.programs_root,
        )
    )
    lines.extend(
        _prompt_version_context_lines(
            ai_context.program_id,
            task_type="exec_summary",
            programs_root=ai_context.programs_root,
        )
    )
    lines.extend(
        _prompt_version_model_context_lines(
            ai_context.program_id,
            task_type="exec_summary",
            programs_root=ai_context.programs_root,
        )
    )
    lines.extend(
        _model_context_lines(
            ai_context.program_id,
            task_type="exec_summary",
            programs_root=ai_context.programs_root,
        )
    )
    lines.extend(_leadership_reader_context_lines(program_context))
    lines.extend(_lt_deck_context_lines(ai_context.program_id, programs_root=ai_context.programs_root))  # SP3-6
    lines.extend(
        _feedback_context_lines(
            ai_context.approved_signals,
            item_ids=set(),
            workstream_ids=(),
            limit=3,
            people_directory=ai_context.people_directory,
            source_confidence_order=ai_context.source_confidence_order,
            as_of=ai_context.as_of,
        )
    )
    lines.extend(
        _signal_context_lines(
            ai_context.approved_signals,
            item_ids=set(),
            workstream_ids=(),
            limit=8,
            people_directory=ai_context.people_directory,
            source_confidence_order=ai_context.source_confidence_order,
            as_of=ai_context.as_of,
        )
    )
    lines.extend(_drift_context_lines(ai_context.drift_patterns, item_ids=set(), limit=6))
    lines.extend(_cascade_context_lines(ai_context.dependency_cascades, workstream_ids=(), limit=3))
    return tuple(lines)


def _lt_deck_context_lines(program_id: str, *, programs_root: Path) -> tuple[str, ...]:
    """SP3-6: Inject approved LT deck evidence into exec summary context as '## LT Deck Context'.

    Reads approved lt_deck-sourced WorkstreamEvidence from evidence_store.jsonl.
    Returns empty tuple if no LT deck evidence available.
    """
    try:
        from src.core.jsonl_utils import read_jsonl_records
        evidence_path = programs_root / program_id / "journal" / "evidence_store.jsonl"
        if not evidence_path.exists():
            return ()

        lt_deck_markers: list[str] = []
        for record in read_jsonl_records(evidence_path):
            if not isinstance(record, dict):
                continue
            source_refs = record.get("source_refs", [])
            has_lt_deck = any(
                isinstance(r, dict) and r.get("source_type") == "lt_deck"
                for r in source_refs
            )
            if not has_lt_deck:
                continue
            # Extract narrative summary and raw excerpts
            narrative = record.get("narrative_summary", "")
            if narrative:
                lt_deck_markers.append(f"LT deck: {narrative[:200]}")
            for excerpt in (record.get("raw_excerpts") or [])[:2]:
                if excerpt:
                    lt_deck_markers.append(f"LT deck excerpt: {str(excerpt)[:150]}")

        if not lt_deck_markers:
            return ()
        header = "## LT Deck Context"
        return (header, *lt_deck_markers[:5])
    except Exception:
        return ()
def _prompt_version_context_lines(
    program_id: str,
    *,
    task_type: str,
    programs_root: Path,
    limit: int = 2,
) -> tuple[str, ...]:
    summaries = summarize_recent_prompt_versions(
        program_id,
        task_type=task_type,
        programs_root=programs_root,
    )
    if not summaries:
        return ()

    lines = [
        f"Recent prompt performance [{summaries[0].prompt_version}]: "
        f"score={summaries[0].calibration_score:.2f}; "
        f"avg_override={summaries[0].average_override_magnitude:.2f}; "
        f"samples={summaries[0].sample_count}."
    ]
    if len(summaries) > 1:
        for rank, summary in enumerate(summaries[:limit], start=1):
            lines.append(
                f"Prompt leaderboard [{task_type}] #{rank}: {summary.prompt_version}; "
                f"score={summary.calibration_score:.2f}; "
                f"avg_override={summary.average_override_magnitude:.2f}; "
                f"samples={summary.sample_count}."
            )
    return tuple(lines)


def _prompt_version_model_context_lines(
    program_id: str,
    *,
    task_type: str,
    programs_root: Path,
    output_root: Path | None = None,
    limit: int = 2,
) -> tuple[str, ...]:
    summaries = summarize_recent_prompt_version_models(
        program_id,
        task_type=task_type,
        programs_root=programs_root,
    )
    return tuple(
        f"Recent prompt model [{summary.prompt_version}/{summary.model}]: "
        f"score={summary.calibration_score:.2f}; "
        f"avg_override={summary.average_override_magnitude:.2f}; "
        f"deployments={summary.deployment_count}; "
        f"samples={summary.sample_count}."
        for summary in summaries[:limit]
    )


def _model_context_lines(
    program_id: str,
    *,
    task_type: str,
    programs_root: Path,
    output_root: Path | None = None,
    limit: int = 2,
) -> tuple[str, ...]:
    summaries = summarize_recent_models(
        program_id,
        task_type=task_type,
        programs_root=programs_root,
    )
    if not summaries:
        return ()

    lines = [
        f"Recent model performance [{summaries[0].model}]: "
        f"score={summaries[0].calibration_score:.2f}; "
        f"avg_override={summaries[0].average_override_magnitude:.2f}; "
        f"deployments={summaries[0].deployment_count}; "
        f"samples={summaries[0].sample_count}."
    ]
    if len(summaries) > 1:
        for rank, summary in enumerate(summaries[:limit], start=1):
            lines.append(
                f"Model leaderboard [{task_type}] #{rank}: {summary.model}; "
                f"score={summary.calibration_score:.2f}; "
                f"avg_override={summary.average_override_magnitude:.2f}; "
                f"deployments={summary.deployment_count}; "
                f"samples={summary.sample_count}."
            )
    return tuple(lines)


def _prompt_version_confidence_context_lines(
    program_id: str,
    *,
    task_type: str,
    programs_root: Path,
    limit: int = 2,
) -> tuple[str, ...]:
    summaries = summarize_recent_prompt_version_confidence_bands(
        program_id,
        task_type=task_type,
        programs_root=programs_root,
    )
    return tuple(
        f"Recent prompt confidence [{summary.prompt_version}/{summary.ai_confidence}]: "
        f"score={summary.calibration_score:.2f}; "
        f"avg_override={summary.average_override_magnitude:.2f}; "
        f"samples={summary.sample_count}."
        for summary in summaries[:limit]
    )


def _confidence_band_context_lines(
    program_id: str,
    *,
    task_type: str,
    programs_root: Path,
    limit: int = 2,
) -> tuple[str, ...]:
    summaries = summarize_recent_confidence_bands(
        program_id,
        task_type=task_type,
        programs_root=programs_root,
    )
    return tuple(
        f"Recent confidence calibration [{task_type}/{summary.ai_confidence}]: "
        f"score={summary.calibration_score:.2f}; "
        f"avg_override={summary.average_override_magnitude:.2f}; "
        f"samples={summary.sample_count}."
        for summary in summaries[:limit]
    )


def _leadership_reader_context_lines(program_context: NarrativeProgramContext | None) -> tuple[str, ...]:
    lines: list[str] = []
    if program_context is None:
        return ()
    for reader in program_context.leadership_readers:
        cares_about = ", ".join(reader.cares_about) if reader.cares_about else "general readiness"
        lines.append(f"Leadership reader {reader.name}: cares about {cares_about}.")
    return tuple(lines)


def _edit_pattern_context_lines(
    program_id: str,
    *,
    workstream_ids: tuple[str, ...],
    programs_root: Path,
    output_root: Path | None = None,
) -> tuple[str, ...]:
    lines: list[str] = []
    workstream_summary = next(
        (
            summary
            for summary in summarize_recent_calibration(program_id, programs_root=programs_root)
            if summary.task_type == "workstream_blurb"
        ),
        None,
    )
    if workstream_summary is not None:
        lines.append(
            "Recent calibration [workstream_blurb]: "
            f"score={workstream_summary.calibration_score:.2f}; "
            f"avg_override={workstream_summary.average_override_magnitude:.2f}; "
            f"samples={workstream_summary.sample_count}."
        )
    lines.extend(
        _confidence_band_context_lines(
            program_id,
            task_type="workstream_blurb",
            programs_root=programs_root,
        )
    )
    lines.extend(
        _prompt_version_confidence_context_lines(
            program_id,
            task_type="workstream_blurb",
            programs_root=programs_root,
        )
    )
    lines.extend(
        _prompt_version_context_lines(
            program_id,
            task_type="workstream_blurb",
            programs_root=programs_root,
        )
    )
    lines.extend(
        _prompt_version_model_context_lines(
            program_id,
            task_type="workstream_blurb",
            programs_root=programs_root,
        )
    )
    lines.extend(
        _model_context_lines(
            program_id,
            task_type="workstream_blurb",
            programs_root=programs_root,
        )
    )
    for workstream_id in workstream_ids:
        for pattern in load_recent_edit_patterns(
            program_id,
            section_id=workstream_id,
            limit=2,
            programs_root=programs_root,
        ):
            lines.append(f"Recent confirmed edit pattern [{workstream_id}]: {pattern.summary}")
    return tuple(lines)


def _signal_context_lines(
    signals: tuple[JournalSignal, ...],
    *,
    item_ids: set[int],
    workstream_ids: tuple[str, ...],
    limit: int,
    people_directory: tuple[PersonDirectory, ...] = (),
    source_confidence_order: tuple[str, ...] = (),
    as_of: datetime | None = None,
) -> tuple[str, ...]:
    filtered = tuple(
        signal
        for signal in signals
        if _signal_matches_item_ids(signal, item_ids)
        or (signal.workstream_id is not None and signal.workstream_id in workstream_ids)
        or (not item_ids and not workstream_ids)
    )
    if as_of is not None or people_directory or source_confidence_order:
        filtered = sort_signals_for_ai_context(
            filtered,
            people_directory=people_directory,
            as_of=as_of,
            source_confidence_order=source_confidence_order,
        )
    else:
        filtered = tuple(sorted(filtered, key=lambda entry: entry.timestamp, reverse=True))
    return _threaded_signal_context_lines(filtered, limit=limit)


def _feedback_context_lines(
    signals: tuple[JournalSignal, ...],
    *,
    item_ids: set[int],
    workstream_ids: tuple[str, ...],
    limit: int,
    people_directory: tuple[PersonDirectory, ...] = (),
    source_confidence_order: tuple[str, ...] = (),
    as_of: datetime | None = None,
) -> tuple[str, ...]:
    filtered = tuple(
        signal
        for signal in signals
        if signal.source == "workiq/email"
        and (
            _signal_matches_item_ids(signal, item_ids)
            or (signal.workstream_id is not None and signal.workstream_id in workstream_ids)
            or (not item_ids and not workstream_ids)
        )
    )
    if not filtered:
        return ()
    if as_of is not None or people_directory or source_confidence_order:
        filtered = sort_signals_for_ai_context(
            filtered,
            people_directory=people_directory,
            as_of=as_of,
            source_confidence_order=source_confidence_order,
        )
    else:
        filtered = tuple(sorted(filtered, key=lambda entry: entry.timestamp, reverse=True))
    return _threaded_feedback_context_lines(filtered, limit=limit)


def _threaded_feedback_context_lines(
    signals: tuple[JournalSignal, ...],
    *,
    limit: int,
) -> tuple[str, ...]:
    if not signals:
        return ()

    ordered_groups: list[str] = []
    grouped: dict[str, list[JournalSignal]] = {}
    for signal in signals:
        group_key = signal.thread_id or signal.id
        if group_key not in grouped:
            grouped[group_key] = []
            ordered_groups.append(group_key)
        grouped[group_key].append(signal)

    lines: list[str] = []
    for group_key in ordered_groups[:limit]:
        group = tuple(sorted(grouped[group_key], key=lambda entry: entry.timestamp, reverse=True))
        newest = group[0]
        subject = _signal_metadata_string(newest, "subject")
        sender_alias = _signal_metadata_string(newest, "sender_alias")
        header_parts: list[str] = []
        if subject:
            header_parts.append(f"subject={subject}")
        if sender_alias:
            header_parts.append(f"latest_sender={sender_alias}")
        excerpt_parts: list[str] = []
        for entry in group[:2]:
            sender = _signal_metadata_string(entry, "sender_alias") or "unknown"
            text = " ".join((entry.text or "").split())
            if text:
                excerpt_parts.append(f"{entry.timestamp.date().isoformat()} {sender}: {text}")
        if not excerpt_parts:
            excerpt_parts.append(newest.timestamp.date().isoformat())
        label = "Approved feedback thread"
        if newest.thread_id is None:
            label = "Approved feedback signal"
        header = f"{label} {group_key}"
        if header_parts:
            header += f" [{'; '.join(header_parts)}]"
        lines.append(f"{header}: {' | '.join(excerpt_parts)}")
    return tuple(lines)


def _signal_metadata_string(signal: JournalSignal, key: str) -> str | None:
    metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
    value = metadata.get(key)
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split()).strip()
    return normalized or None


def _threaded_signal_context_lines(
    signals: tuple[JournalSignal, ...],
    *,
    limit: int,
) -> tuple[str, ...]:
    if not signals:
        return ()

    ordered_groups: list[tuple[str, tuple[JournalSignal, ...]]] = []
    grouped: dict[str, list[JournalSignal]] = {}
    for signal in signals:
        group_key = signal.thread_id or signal.id
        if group_key not in grouped:
            grouped[group_key] = []
            ordered_groups.append((group_key, ()))
        grouped[group_key].append(signal)

    lines: list[str] = []
    for group_key, _ in ordered_groups[:limit]:
        group = tuple(sorted(grouped[group_key], key=lambda entry: entry.timestamp, reverse=True))
        newest = group[0]
        if newest.thread_id is None:
            lines.append(f"Approved signal {newest.timestamp.isoformat()} [{newest.source}]: {newest.text}")
            continue
        summaries = " | ".join(
            f"{entry.timestamp.date().isoformat()} [{entry.source}] {entry.text}"
            for entry in group
        )
        lines.append(f"Approved signal thread {newest.thread_id}: {summaries}")
    return tuple(lines)


def _drift_context_lines(
    drift_patterns: tuple[DriftPattern, ...],
    *,
    item_ids: set[int],
    limit: int,
) -> tuple[str, ...]:
    filtered = [
        pattern
        for pattern in drift_patterns
        if not item_ids or pattern.work_item_id in item_ids
    ]
    return tuple(
        f"Drift pattern WI:{pattern.work_item_id} [{pattern.pattern}/{pattern.severity}]: {pattern.detail}"
        for pattern in filtered[:limit]
    )


def _cascade_context_lines(
    cascades: tuple[DependencyCascade, ...],
    *,
    workstream_ids: tuple[str, ...],
    limit: int,
) -> tuple[str, ...]:
    filtered = [
        cascade
        for cascade in cascades
        if not workstream_ids or any(workstream_id in workstream_ids for workstream_id in cascade.target_workstream_ids)
    ]
    return tuple(
        f"Dependency cascade [{cascade.trigger_kind}]: {cascade.source_item} -> {cascade.target_item}: {cascade.impact}"
        for cascade in filtered[:limit]
    )


def _matching_program_workstream_ids(
    section_items: tuple[WorkItem, ...],
    workstreams: tuple[Workstream, ...],
) -> tuple[str, ...]:
    matched: set[str] = set()
    for item in section_items:
        for workstream in workstreams:
            if any(item.area_path.startswith(area_path) for area_path in workstream.area_paths):
                matched.add(workstream.id)
    return tuple(sorted(matched))


def _signal_matches_item_ids(signal: JournalSignal, item_ids: set[int]) -> bool:
    if not item_ids:
        return False
    for entity_ref in signal.entity_refs:
        if not entity_ref.startswith("WI:"):
            continue
        raw_id = entity_ref.split(":", 1)[1].strip()
        if raw_id.isdigit() and int(raw_id) in item_ids:
            return True
    return False


def _get_report_ai_client(
    *,
    deployment: str | None,
    temperature: float,
    budget_usd: float,
    client_cache: dict[_ReportAIClientCacheKey, LLMProvider | None],
    trace_context: AITraceContext | None,
    warnings: list[str],
    label: str,
    create_ai_client: Callable[..., LLMProvider],
    warn_on_failure: bool = True,
) -> LLMProvider | None:
    if deployment is None:
        warnings.append(_missing_report_ai_deployment_warning(label))
        return None
    cache_key = _report_ai_client_cache_key(deployment, trace_context)
    if cache_key in client_cache:
        return client_cache[cache_key]
    try:
        client = create_ai_client(
            deployment=deployment,
            temperature=temperature,
            budget_usd=budget_usd,
            trace_context=trace_context,
        )
    except (AIClientError, RuntimeError) as error:
        if warn_on_failure:
            warnings.append(f"AI {label} synthesis skipped: {error}")
        client_cache[cache_key] = None
        return None
    client_cache[cache_key] = client
    return client


def _run_report_ai_with_fallback(
    *,
    deployments: tuple[str, ...],
    temperature: float,
    budget_usd: float,
    client_cache: dict[_ReportAIClientCacheKey, LLMProvider | None],
    trace_context: AITraceContext | None,
    warnings: list[str],
    label: str,
    create_ai_client: Callable[..., LLMProvider],
    runner: Callable[[LLMProvider], Any],
) -> Any | None:
    if not deployments:
        warnings.append(_missing_report_ai_deployment_warning(label))
        return None

    last_error: Exception | None = None
    for index, deployment in enumerate(deployments):
        client = _get_report_ai_client(
            deployment=deployment,
            temperature=temperature,
            budget_usd=budget_usd,
            client_cache=client_cache,
            trace_context=trace_context,
            warnings=warnings,
            label=label,
            create_ai_client=create_ai_client,
            warn_on_failure=index == len(deployments) - 1,
        )
        if client is None:
            if index < len(deployments) - 1:
                warnings.append(
                    f"AI {label} primary deployment failed ({deployment}); trying backup deployment."
                )
            continue
        try:
            result = runner(client)
        except (AIClientError, BlurbGenerationError, ExecSummaryDraftError, RuntimeError) as error:
            last_error = error
            if index < len(deployments) - 1:
                warnings.append(
                    f"AI {label} primary deployment failed ({deployment}); trying backup deployment."
                )
                continue
            warnings.append(f"AI {label} synthesis skipped: {error}")
            return None
        if index > 0:
            warnings.append(f"AI {label} fallback deployment succeeded ({deployment}).")
        return result

    if last_error is not None:
        warnings.append(f"AI {label} synthesis skipped: {last_error}")
    return None


def _build_report_ai_trace_run_id(*, edition_name: str, issue_number: int, started_at: datetime) -> str:
    return f"{edition_name}:issue-{issue_number:03d}:{started_at.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"


def _missing_report_ai_deployment_warning(label: str) -> str:
    if label == "exec summary":
        return (
            "AI exec summary synthesis skipped: deployment is not configured. "
            "Set VERTEX_EXEC_DEPLOYMENT, VERTEX_AI_DEPLOYMENT, or AZURE_OPENAI_DEPLOYMENT; "
            f"{LEGACY_DEPLOYMENT_ALIAS_NOTICE}"
        )
    return (
        f"AI {label} synthesis skipped: deployment is not configured. "
        "Set VERTEX_AI_DEPLOYMENT or AZURE_OPENAI_DEPLOYMENT; "
        f"{LEGACY_DEPLOYMENT_ALIAS_NOTICE}"
    )


def _resolve_report_ai_deployments(
    *,
    feature_name: str,
    primary: str | None,
    backup: str | None,
    primary_fallback_envs: tuple[str, ...],
    backup_fallback_envs: tuple[str, ...],
) -> tuple[str, ...]:
    return resolve_ai_deployments_for_feature(
        feature_name=feature_name,
        primary_candidates=(primary,),
        backup_candidates=(backup,),
        primary_fallback_envs=primary_fallback_envs,
        backup_fallback_envs=backup_fallback_envs,
    )


_ReportAIClientCacheKey = tuple[str, tuple[tuple[str, str], ...]]


def _with_trace_metadata(trace_context: AITraceContext | None, **metadata: object) -> AITraceContext | None:
    if trace_context is None:
        return None
    merged = dict(trace_context.metadata)
    merged.update({str(key): value for key, value in metadata.items()})
    return replace(trace_context, metadata=merged)


def _report_ai_client_cache_key(
    deployment: str,
    trace_context: AITraceContext | None,
) -> _ReportAIClientCacheKey:
    if trace_context is None or not trace_context.metadata:
        return (deployment, ())
    return (
        deployment,
        tuple(
            sorted(
                (str(key), json.dumps(value, sort_keys=True, default=str))
                for key, value in trace_context.metadata.items()
            )
        ),
    )


def _report_ai_usage(
    client_cache: dict[_ReportAIClientCacheKey, LLMProvider | None],
) -> tuple[int, float, dict[str, float]]:
    ai_calls = sum(
        int(getattr(getattr(client, "usage_stats", None), "call_count", 0) or 0)
        for client in client_cache.values()
        if client is not None
    )
    ai_cost_usd = sum(
        float(getattr(client, "spent_usd", 0.0) or 0.0)
        for client in client_cache.values()
        if client is not None
    )
    ai_cost_by_model: dict[str, float] = {}
    for (deployment, _), client in client_cache.items():
        if client is None:
            continue
        ai_cost_by_model[deployment] = round(
            ai_cost_by_model.get(deployment, 0.0) + float(getattr(client, "spent_usd", 0.0) or 0.0),
            6,
        )
    return ai_calls, ai_cost_usd, ai_cost_by_model
