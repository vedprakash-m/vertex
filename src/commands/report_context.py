from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from src.commands.report_ai import _ReportSignalContext
from src.core.config_loader import ReportBundle
from src.core.forecast_engine import ETAForecast
from src.core.models import DeltaSet, DimensionRisk, PersonProfile, ProgramContext, ScorecardDelta, WorkItem
from src.core.models import Workstream as ReportWorkstream
from src.core.overrides_store import OverridesDocument
from src.core.program_reality import ProgramReality
from src.core.view_models import ScorecardData


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
    chart_rows_scrubber: Callable[[list[dict[str, Any]]], list[dict[str, Any]]]
    now_utc: Callable[[], datetime]
    read_git_sha: Callable[[], str | None]
    build_draft_readiness: Callable[..., Any]
    format_ban_violation: Callable[..., str]


def _build_model_program_context(bundle: ReportBundle) -> ProgramContext:
    if bundle.program_context is None:
        return ProgramContext(
            program_name=bundle.config.edition.name,
            mission=bundle.config.edition.title,
            pillars=(),
            workstreams=(),
            glossary={},
            people=(),
        )
    return ProgramContext(
        program_name=bundle.program_context.program_name,
        mission=bundle.program_context.mission or bundle.program_context.objective or bundle.config.edition.title,
        pillars=bundle.program_context.pillars,
        workstreams=tuple(
            ReportWorkstream(
                name=workstream.name,
                aliases=workstream.aliases,
                area_paths=workstream.area_paths,
                dri_email=workstream.dri_email or "",
                description=workstream.description or "",
            )
            for workstream in bundle.program_context.workstreams
        ),
        glossary=bundle.program_context.glossary,
        people=tuple(
            PersonProfile(
                email=person.email,
                display_name=person.display_name or person.email,
                role=person.role or "",
                workstreams=person.workstreams,
            )
            for person in bundle.program_context.people
        ),
    )