from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

from src.core.claim_tracker import load_open_claims, load_open_decision_asks
from src.core.issue_projection import IssueProjection, build_issue_projection
from src.core.archive_store import read_vitality_history
from src.core.attribution_engine import build_inline_citations
from src.core.deck_renderer import DeckRenderer
from src.core.forecast_engine import ForecastAssessment, build_forecast_assessment
from src.core.freshness_engine import build_freshness_report
from src.core.html_renderer import HTMLRenderer, RenderContext
from src.core.journal import read_signals
import functools

from src.core.chart_cache_store import load_chart_cache
from src.core.chart_renderer_registry import build_default_registry
from src.core.kusto_rendering import TelemetryObservation, build_kusto_sections
from src.core.theme_context import build_theme_context
from src.core.kusto_templates import KustoTemplateContext
from src.core.models import EditionType, ReportData, ReviewState, ReviewStatus, RiskLevel, Snapshot, WorkItem
from src.core.notification_state_store import load_latest_notification_state
from src.core.pipeline import StageContext
from src.core.program_fact_store import load_program_facts, project_risk_entries
from src.core.program_reality import ProgramReality
from src.core.quality_matrix_engine import QualityMatrix, build_quality_matrix
from src.core.remediation_engine import RemediationReport, build_remediation_report
from src.core.review_status_store import get_review_status_path
from src.core.risk_register_engine import assess_risk_staleness
from src.core.signal_review import signal_is_approved_for_evidence
from src.core.signal_ranking import signal_source_family
from src.core.store_factory import build_signal_store_for_program_id
from src.core.teams_renderer import TeamsRenderer
from src.core.velocity_metrics import build_velocity_kusto_section
from src.core.vitality_reporting import build_vitality_section
from src.core.view_models import EditionMeta, WorkstreamData

log = logging.getLogger(__name__)
_RISK_RENDER_WARNING = (
    "⚠ System error: this section is showing a stale, unmaintained data source because live risk rendering failed — see logs."
)
_RISK_RENDER_FALLBACK_EXCEPTIONS = (AttributeError, KeyError, TypeError, ValueError)


@dataclass(frozen=True, slots=True)
class RenderStageState:
    freshness_report: Any
    review_status: ReviewStatus
    review_status_path: Path
    report: ReportData
    workstream_data: Any
    narrative_warnings: tuple[str, ...]
    forecast: ForecastAssessment | None
    kusto_sections: Any
    kusto_warnings: tuple[str, ...]
    quality_matrix: QualityMatrix
    remediation_report: RemediationReport
    exec_summary_citations: Any
    title: str
    subject_signal: str
    email_subject: str
    email_preheader: str
    productivity_dividend_hours: float | None
    html_body: str
    markdown_body: str
    snapshot: Snapshot
    rendered_strings: dict[str, str]


class RenderStage:
    def name(self) -> str:
        return "render"

    def execute(self, ctx: StageContext) -> StageContext:
        if ctx.resolved_edition_type == EditionType.LOOKBACK or ctx.render_state is not None:
            return ctx
        if (
            ctx.bundle is None
            or ctx.reports_root is None
            or ctx.archive_root is None
            or ctx.programs_root is None
            or ctx.programs_root is None
            or ctx.archive_index is None
            or ctx.resolved_issue_number is None
            or ctx.resolved_edition_type is None
            or ctx.started_at is None
            or ctx.data_as_of is None
            or ctx.overrides_document is None
            or ctx.top_items is None
            or ctx.auto_suggestions is None
            or ctx.continuity_chapters is None
            or ctx.narratives_dir is None
            or ctx.exec_summary_text is None
            or ctx.workstream_blurbs is None
            or ctx.workstream_narrative_history is None
            or ctx.ai_synthesis is None
            or ctx.render_exec_summary_text is None
            or ctx.render_workstream_blurbs is None
            or ctx.dimension_risks is None
            or ctx.scorecard_deltas is None
            or ctx.scorecards is None
            or ctx.scorecard_packets is None
            or ctx.evidence_by_item is None
            or ctx.stage_support is None
        ):
            raise RuntimeError("AIStage must execute before RenderStage.")

        support = ctx.stage_support

        previous_notification_state = load_latest_notification_state(
            edition=ctx.edition_name,
            programs_root=ctx.programs_root,
        )
        freshness_report = build_freshness_report(
            current_items=ctx.items,
            issue_number=ctx.resolved_issue_number,
            as_of=ctx.data_as_of,
            stale_warn_days=ctx.bundle.editorial_rules.stale_warn_days,
            stale_block_days=ctx.bundle.editorial_rules.stale_block_days,
            previous_snapshot=ctx.previous_snapshot,
            previous_notification_state=previous_notification_state,
            program_context=ctx.bundle.program_context,
            workstream_narrative_history=ctx.workstream_narrative_history,
        )
        review_status = support.ensure_review_status(
            edition_name=ctx.edition_name,
            issue_number=ctx.resolved_issue_number,
            workstream_section_ids=(
                tuple(chapter.id for chapter in ctx.continuity_chapters)
                if support.is_continuity_layout(ctx.bundle)
                else tuple(ctx.workstream_blurbs)
            ),
            skipped_section_ids=(
                set()
                if support.is_continuity_layout(ctx.bundle)
                else support.skipped_review_sections(
                    bundle=ctx.bundle,
                    items=ctx.items,
                    scorecards=ctx.scorecards,
                    scorecard_packets=ctx.scorecard_packets,
                    overrides_document=ctx.overrides_document,
                    deltas=ctx.deltas,
                    freshness_report=freshness_report,
                    top_items=ctx.top_items,
                    previous_snapshot=ctx.previous_snapshot,
                )
            ),
            reports_root=ctx.reports_root,
        )
        review_status_path = get_review_status_path(ctx.edition_name, reports_root=ctx.reports_root)

        report = ReportData(
            issue_number=ctx.resolved_issue_number,
            edition=ctx.resolved_edition_type,
            generated_at=ctx.started_at,
            ado_data_as_of=ctx.data_as_of,
            program=support.build_model_program_context(ctx.bundle),
            items=ctx.items,
            deltas=ctx.deltas,
            scorecard=ctx.dimension_risks,
            scorecard_deltas=ctx.scorecard_deltas,
            exec_summary_text=ctx.render_exec_summary_text,
            workstream_blurbs=ctx.render_workstream_blurbs,
            freshness=freshness_report,
            hygiene_warnings=(),
            review_status=review_status,
            manifest_id=uuid.uuid4().hex,
        )

        item_urls = support.build_item_urls(ctx.bundle, ctx.items)
        approved_signals: tuple[Any, ...] = ()
        if ctx.resolved_v2 is not None:
            signal_store = build_signal_store_for_program_id(
                ctx.resolved_v2.program.id,
                programs_root=ctx.programs_root,
            )
            review_states = signal_store.read_reviews(ctx.resolved_v2.program.id)
            approved_signals = tuple(
                signal
                for signal in signal_store.read(
                    ctx.resolved_v2.program.id,
                    end=ctx.data_as_of,
                )
                if signal_is_approved_for_evidence(signal, review_states)
            )
        workstream_data = support.build_workstream_data(
            issue_number=ctx.resolved_issue_number,
            bundle=ctx.bundle,
            edition_type=ctx.resolved_edition_type,
            items=ctx.items,
            scorecards=ctx.scorecards,
            scorecard_packets=ctx.scorecard_packets,
            overrides_document=ctx.overrides_document,
            workstream_blurbs=ctx.render_workstream_blurbs,
            dependency_cascades=(ctx.signal_context.dependency_cascades if ctx.signal_context is not None else ()),
            review_status=review_status,
            evidence_by_item=ctx.evidence_by_item,
            item_urls=item_urls,
            eta_forecasts=ctx.eta_forecasts,
            approved_signals=approved_signals,
            workstreams=(ctx.resolved_v2.workstreams if ctx.resolved_v2 is not None else ()),
            program_id=(ctx.resolved_v2.program.id if ctx.resolved_v2 is not None else None),
            programs_root=ctx.programs_root,
            source_footnotes=(
                dict(ctx.ai_synthesis.workstream_source_footnotes)
                if ctx.ai_synthesis is not None
                else {}
            ),
        )
        if ctx.visible_section_ids is not None:
            workstream_data = tuple(
                workstream
                for workstream in workstream_data
                if workstream.section_id in ctx.visible_section_ids
            )
        if not support.is_continuity_layout(ctx.bundle):
            workstream_data = _append_carried_forward_workstreams(
                issue_number=ctx.resolved_issue_number,
                bundle=ctx.bundle,
                workstream_data=workstream_data,
                workstream_blurbs=ctx.render_workstream_blurbs,
                review_status=review_status,
                removed_section_ids=set(ctx.overrides_document.removed_sections),
                overrides_document=ctx.overrides_document,
                scorecards=ctx.scorecards,
                source_footnotes=(
                    dict(ctx.ai_synthesis.workstream_source_footnotes)
                    if ctx.ai_synthesis is not None
                    else {}
                ),
            )
        narrative_warning_workstream_data = workstream_data
        if not support.is_continuity_layout(ctx.bundle) and ctx.bundle.chapter_contract is not None:
            narrative_warning_workstream_data = _build_dashboard_chapter_warning_workstreams(
                issue_number=ctx.resolved_issue_number,
                bundle=ctx.bundle,
                edition_type=ctx.resolved_edition_type,
                scorecards=ctx.scorecards,
                scorecard_packets=ctx.scorecard_packets,
                workstream_blurbs=ctx.render_workstream_blurbs,
                review_status=review_status,
                items=ctx.items,
                source_footnotes=(
                    dict(ctx.ai_synthesis.workstream_source_footnotes)
                    if ctx.ai_synthesis is not None
                    else {}
                ),
            ) or narrative_warning_workstream_data
        continuity_render = support.build_continuity_render_data(
            bundle=ctx.bundle,
            issue_number=ctx.resolved_issue_number,
            edition_type=ctx.resolved_edition_type,
            overrides_document=ctx.overrides_document,
            scorecards=ctx.scorecards,
            scorecard_packets=ctx.scorecard_packets,
            workstream_data=workstream_data,
            items=ctx.items,
            item_urls=item_urls,
            eta_forecasts=ctx.eta_forecasts,
        )
        narrative_warnings = support.workstream_narrative_warnings(
            issue_number=ctx.resolved_issue_number,
            workstream_data=narrative_warning_workstream_data,
            stale_narratives=support.detect_stale_narratives(
                program_id=(ctx.resolved_v2.program.id if ctx.resolved_v2 is not None else None),
                workstream_data=narrative_warning_workstream_data,
                narratives_dir=ctx.narratives_dir,
                programs_root=ctx.programs_root,
            ),
            stage="dry_run",
        )
        forecast = build_forecast_assessment(
            enabled=ctx.bundle.config.forecast_enabled,
            edition_name=ctx.edition_name,
            as_of=ctx.data_as_of,
            workstreams=workstream_data,
            deltas=ctx.deltas,
            archive_root=ctx.archive_root,
        )
        new_high_count = support.count_new_high_dimensions(ctx.scorecard_deltas)
        severe_ack_required = support.decision_strip_ack_required(ctx.top_items, new_high_count, freshness_report)
        read_time_minutes = support.compute_read_time_minutes(
            ctx.exec_summary_text,
            ctx.workstream_blurbs,
            ctx.resolved_edition_type,
        )
        healthy_streak = support.compute_healthy_streak(
            ctx.edition_name,
            ctx.resolved_issue_number,
            ctx.dimension_risks,
            ctx.archive_root,
        )
        health = _build_health_summary_with_risk_fallback(
            ctx,
            support,
            ctx.dimension_risks,
            ctx.continuity_snapshot,
            overrides_document=ctx.overrides_document,
            top_items=ctx.top_items,
            forecast=forecast,
            items=ctx.items,
            milestones=(ctx.milestones or ()),
            milestone_assessments=(ctx.milestone_assessments or ()),
            risks=(ctx.risks or ()),
            risk_assessments=(ctx.risk_assessments or ()),
            stale_risk_ids=ctx.stale_risk_ids,
            program_id=(ctx.resolved_v2.program.id if ctx.resolved_v2 is not None else None),
            programs_root=ctx.programs_root,
            as_of=ctx.data_as_of,
            severe_ack_required=severe_ack_required,
            is_dry_run=True,
            read_time_minutes=read_time_minutes,
            edition_type=ctx.resolved_edition_type,
            new_high_count=new_high_count,
            healthy_streak=healthy_streak,
            status_note=(
                f"Offline - data may be stale. Using {ctx.offline_source_label}."
                if ctx.offline_source_label is not None
                else None
            ),
        )
        forwarding_context = support.resolve_forwarding_context(ctx.overrides_document, ctx.top_items, ctx.auto_suggestions)
        kusto_template_context = None
        if ctx.resolved_v2 is not None:
            kusto_template_context = KustoTemplateContext(
                program_id=ctx.resolved_v2.program.id,
                area_paths=ctx.bundle.config.ado.area_paths,
                date_window_days=ctx.bundle.config.ado.date_window_days,
            )
        ado_vitality = None
        if ctx.resolved_v2 is not None and ctx.resolved_edition_type in {EditionType.DETAILED, EditionType.FOCUSED}:
            vitality_snapshot, vitality_settings = support.build_v2_vitality_snapshot(
                resolved_v2=ctx.resolved_v2,
                items=ctx.items,
                as_of=ctx.data_as_of,
                programs_root=ctx.programs_root,
            )
            if vitality_settings.newsletter_aggregate:
                ado_vitality = build_vitality_section(
                    vitality_snapshot,
                    current_issue_number=ctx.resolved_issue_number,
                    history_entries=read_vitality_history(ctx.edition_name, archive_root=ctx.archive_root),
                    items=ctx.items,
                    workstreams=ctx.resolved_v2.workstreams,
                    include_individual_praise=vitality_settings.newsletter_individual_praise,
                )
        # Chart pipeline: bind cache loader and build registry/theme once per render
        _chart_cache_loader = (
            functools.partial(load_chart_cache, programs_root=ctx.programs_root)
            if ctx.programs_root is not None
            else None
        )
        _chart_registry = build_default_registry()
        _theme_context = build_theme_context()

        kusto_sections, telemetry_observations, kusto_warnings = build_kusto_sections(
            replace(ctx.bundle.config.kusto, enabled=False) if ctx.offline else ctx.bundle.config.kusto,
            ctx.kusto_query_executor or support.live_kusto_query_executor(),
            observed_at=ctx.started_at,
            template_context=kusto_template_context,
            programs_root=ctx.programs_root,
            program_id=(ctx.resolved_v2.program.id if ctx.resolved_v2 is not None else None),
            chart_cache_loader=_chart_cache_loader,
            chart_registry=_chart_registry,
            theme_context=_theme_context,
        )
        kusto_sections, telemetry_observations, kusto_warnings = _apply_kusto_fallbacks(
            ctx=ctx,
            kusto_sections=kusto_sections,
            telemetry_observations=telemetry_observations,
            kusto_warnings=kusto_warnings,
            ado_vitality_available=ado_vitality is not None,
        )
        kusto_sections = tuple(section for section in kusto_sections if section.query_id != "icm-mttr")
        if ctx.resolved_v2 is not None and not ctx.bundle.config.kusto.enabled:
            velocity_section = build_velocity_kusto_section(
                program_id=ctx.resolved_v2.program.id,
                item_ids=(item.id for item in ctx.items),
                as_of=ctx.data_as_of.date(),
                window_days=ctx.bundle.config.ado.date_window_days,
                programs_root=ctx.programs_root,
            )
            if velocity_section is not None:
                kusto_sections = (*kusto_sections, velocity_section)

        # Phase 3: Suppress charts on condensed editions (spec §5.10 — ≤30s read budget)
        if ctx.resolved_edition_type == EditionType.CONDENSED:
            kusto_sections = tuple(
                s for s in kusto_sections
                if getattr(s, "render_mode", None) not in ("chart", "chart_image")
                or not getattr(s, "chart_png_base64", None)
            )

        # Phase 3: Chart placement routing — spec §5.3
        kusto_sections, exec_summary_chart, workstream_data = _route_chart_placements(kusto_sections, workstream_data)
        kusto_sections = tuple(section for section in kusto_sections if section.query_id != "icm-mttr")

        quality_matrix = build_quality_matrix(
            bundle=ctx.bundle,
            issue_number=ctx.resolved_issue_number,
            generated_at=ctx.started_at,
            current_items=ctx.items,
            previous_issue_number=ctx.continuity_previous_issue_number,
            telemetry_observations=telemetry_observations,
            ado_query_base_url=support.ado_saved_query_base_url(ctx.bundle),
        )
        remediation_report = build_remediation_report(quality_matrix)
        exec_summary_citations = build_inline_citations(
            ctx.items,
            ctx.evidence_by_item,
            ado_base_url=support.ado_item_base_url(ctx.bundle),
        )

        title = support.format_edition_title(ctx.bundle, ctx.resolved_issue_number, ctx.data_as_of)
        subject_signal = support.subject_signal(ctx.dimension_risks, ctx.top_items, ctx.auto_suggestions, ctx.scorecard_deltas)
        email_subject = support.build_email_subject(title, health, subject_signal)
        email_preheader = support.build_email_preheader(health, health.bluf, ctx.top_items or ctx.auto_suggestions)
        offline_note = (
            f"Offline - data may be stale. Using {ctx.offline_source_label}."
            if ctx.offline_source_label is not None
            else ""
        )
        _productivity_raw = round((len(workstream_data) * 0.25) + (len(ctx.items) * 0.08) + (len(review_status.sections) * 0.10), 1)
        productivity_dividend: float | None = None if _productivity_raw < 1.0 else _productivity_raw
        milestone_rows = ()
        if (
            ctx.resolved_v2 is not None
            and ctx.resolved_edition_type in {EditionType.DETAILED, EditionType.FOCUSED}
            and ctx.milestones
            and ctx.milestone_assessments
        ):
            milestone_rows = support.build_report_milestone_rows(
                ctx.milestones,
                ctx.milestone_assessments,
                items=ctx.items,
                program_id=ctx.resolved_v2.program.id,
                programs_root=ctx.programs_root,
                as_of=ctx.data_as_of,
                milestone_lineage=ctx.milestone_lineage,
            )
        confirmed_depth = sum(1 for entry in ctx.archive_index.issues if entry.kind == "confirmed")
        edition_meta = EditionMeta(
            edition=ctx.edition_name,
            issue_number=ctx.resolved_issue_number,
            generated_at=ctx.started_at,
            ado_data_as_of=ctx.data_as_of,
            manifest_id=report.manifest_id,
            qg_status=support.derive_qg_status(has_blockers=False, has_warnings=True),
            email_subject=email_subject,
            email_preheader=email_preheader,
            subject_signal=subject_signal,
            productivity_dividend_hours=productivity_dividend,
            show_orientation=ctx.overrides_document.show_orientation or confirmed_depth < 2,
            productivity_dividend_published=ctx.bundle.config.productivity_dividend_published,
        )
        template_contract = None
        if ctx.bundle.template_contract is not None:
            template_contract = ctx.bundle.template_contract.family_for(ctx.resolved_edition_type.value)
        render_context = RenderContext(
            title=title,
            subtitle="",
            preheader=email_preheader,
            report=report,
            edition_meta=edition_meta,
            layout_mode=ctx.bundle.config.layout_mode,
            header_label=None,
            footer_label=None,
            health=health,
            milestone_rows=milestone_rows,
            top_items=ctx.top_items,
            auto_suggestions=ctx.auto_suggestions,
            forwarding_context=forwarding_context,
            decision_strip_ack_required=severe_ack_required,
            scorecards=ctx.scorecards,
            kusto_sections=kusto_sections,
            ado_vitality=ado_vitality,
            workstreams=workstream_data,
            exec_summary_citations=exec_summary_citations,
            exec_summary_chart=exec_summary_chart,
            sections=(),
            template_contract=template_contract,
            prior_date_label=support.format_prior_date_label(ctx.continuity_snapshot),
            changes_url=None,
            item_urls=item_urls,
            scorecard_packets=ctx.scorecard_packets,
            scorecard_deltas=support.group_scorecard_deltas(ctx.scorecard_deltas),
            scorecard_urls={name: next(iter(packet_map.values())).ado_query_url for name, packet_map in ctx.scorecard_packets.items() if packet_map},
            workstream_urls={
                workstream.section_id: workstream.ado_query_url
                for workstream in workstream_data
                if workstream.ado_query_url
            },
            eta_forecasts=ctx.eta_forecasts,
            is_dry_run=True,
            workspace_root=str(Path(__file__).resolve().parents[2]),
            mobile_safe_scorecards=ctx.bundle.config.mobile_safe_scorecards,
            type_scale_v2=ctx.bundle.config.type_scale_v2,
            continuity=continuity_render,
            show_footer=not support.is_continuity_layout(ctx.bundle),
            hidden_render_sections=frozenset(ctx.overrides_document.removed_sections),
        )

        if ctx.resolved_edition_type == EditionType.DECK:
            open_ask_rows, closed_ask_rows = ((), ())
            key_decision_rows = ()
            key_assumption_rows = ()
            issue_projections: tuple[IssueProjection, ...] = ()
            if ctx.resolved_v2 is not None:
                deck_reality = ProgramReality.load(
                    ctx.resolved_v2.program.id,
                    programs_root=ctx.programs_root,
                )
                open_decision_asks = load_open_decision_asks(
                    ctx.resolved_v2.program.id,
                    programs_root=ctx.programs_root,
                )
                open_claims = load_open_claims(
                    ctx.resolved_v2.program.id,
                    programs_root=ctx.programs_root,
                )
                key_assumption_rows = support.build_deck_assumption_rows(
                    program_id=ctx.resolved_v2.program.id,
                    as_of=ctx.data_as_of,
                    programs_root=ctx.programs_root,
                    reality=deck_reality,
                )
                key_decision_rows = support.build_deck_decision_rows(
                    program_id=ctx.resolved_v2.program.id,
                    as_of=ctx.data_as_of,
                    programs_root=ctx.programs_root,
                    reality=deck_reality,
                )
                overdue_action_ids = set(ctx.overdue_action_ids or ())
                overdue_actions = tuple(
                    action
                    for action in (ctx.actions or ())
                    if action.id in overdue_action_ids
                )
                signal_store = build_signal_store_for_program_id(
                    ctx.resolved_v2.program.id,
                    programs_root=ctx.programs_root,
                )
                icm_signals = tuple(
                    signal
                    for signal in signal_store.read(
                        ctx.resolved_v2.program.id,
                        start=ctx.data_as_of - timedelta(days=30),
                        end=ctx.data_as_of,
                    )
                    if signal_source_family(signal.source) == "icm"
                )
                issue_projections = build_issue_projection(
                    items=ctx.items,
                    freshness_report=freshness_report,
                    icm_signals=icm_signals,
                    open_asks=open_decision_asks,
                    overdue_actions=overdue_actions,
                    open_claims=open_claims,
                    risk_entries=(
                        ctx.risks
                        # Normal report runs already populate ctx.risks in
                        # RiskStage; only isolated RenderStage tests/fallback
                        # paths should hit the legacy loader below.
                        if ctx.risks is not None
                        else _load_current_risks(ctx.resolved_v2.program.id, programs_root=ctx.programs_root)
                    ),
                    ado_item_base_url=support.ado_item_base_url(ctx.bundle),
                )
                open_ask_rows, closed_ask_rows = support.build_deck_ask_rows(
                    program_id=ctx.resolved_v2.program.id,
                    issue_number=ctx.resolved_issue_number,
                    as_of=ctx.started_at,
                    last_confirmed_at=(ctx.latest_confirmed_entry.generated_at if ctx.latest_confirmed_entry is not None else None),
                    programs_root=ctx.programs_root,
                )
            html_body = ""
            markdown_body = DeckRenderer(ctx.edition_name, reports_root=ctx.reports_root).render(
                support.build_deck_render_context(
                    issue_number=ctx.resolved_issue_number,
                    data_as_of=ctx.data_as_of,
                    generated_at=ctx.started_at,
                    title=title,
                    source_label=f"ADO {ctx.bundle.config.ado.organization}/{ctx.bundle.config.ado.project}",
                    area_path_count=len(ctx.bundle.config.ado.area_paths),
                    manifest_id=report.manifest_id,
                    dimension_risks=ctx.dimension_risks,
                    top_items=ctx.top_items,
                    deltas=ctx.deltas,
                    scorecard_deltas=ctx.scorecard_deltas,
                    items=ctx.items,
                    eta_forecasts=ctx.eta_forecasts,
                    raw_program=ctx.resolved_v2.raw_program,
                    program_id=ctx.resolved_v2.program.id,
                    programs_root=ctx.programs_root,
                    reality=deck_reality,
                    milestones=(ctx.milestones or ()),
                    milestone_assessments=(ctx.milestone_assessments or ()),
                    issue_projections=issue_projections,
                    key_decision_rows=key_decision_rows,
                    key_assumption_rows=key_assumption_rows,
                    open_ask_rows=open_ask_rows,
                    closed_ask_rows=closed_ask_rows,
                )
            )
        else:
            html_body = HTMLRenderer(ctx.edition_name, reports_root=ctx.reports_root).render(render_context)
            markdown_body = TeamsRenderer(ctx.edition_name, reports_root=ctx.reports_root).render(render_context)
        snapshot = support.build_snapshot(report, ctx.scorecard_packets)
        rendered_strings = {
            "html": html_body,
            "markdown": markdown_body,
            "exec_summary": ctx.render_exec_summary_text,
            **{f"workstream:{section_id}": blurb for section_id, blurb in ctx.render_workstream_blurbs.items()},
        }

        return replace(
            ctx,
            render_state=RenderStageState(
                freshness_report=freshness_report,
                review_status=review_status,
                review_status_path=review_status_path,
                report=report,
                workstream_data=workstream_data,
                narrative_warnings=narrative_warnings,
                forecast=forecast,
                kusto_sections=kusto_sections,
                kusto_warnings=kusto_warnings,
                quality_matrix=quality_matrix,
                remediation_report=remediation_report,
                exec_summary_citations=exec_summary_citations,
                title=title,
                subject_signal=subject_signal,
                email_subject=email_subject,
                email_preheader=email_preheader,
                productivity_dividend_hours=productivity_dividend,
                html_body=html_body,
                markdown_body=markdown_body,
                snapshot=snapshot,
                rendered_strings=rendered_strings,
            ),
        )


def _load_current_risks(program_id: str, *, programs_root: Path):
    return project_risk_entries(
        load_program_facts(
            program_id,
            programs_root=programs_root,
            fact_types=("risk.entry",),
        )
    )


def _build_health_summary_with_risk_fallback(
    ctx: StageContext,
    support: Any,
    *args,
    **kwargs,
):
    try:
        return support.build_health_summary(*args, **kwargs)
    except _RISK_RENDER_FALLBACK_EXCEPTIONS:
        if ctx.risk_assessments is None or ctx.resolved_v2 is None or ctx.programs_root is None or ctx.data_as_of is None:
            raise
        log.critical(
            "Risk render fallback activated for program %s issue %s",
            ctx.resolved_v2.program.id,
            ctx.resolved_issue_number,
            exc_info=True,
        )
        legacy_risks = _load_current_risks(ctx.resolved_v2.program.id, programs_root=ctx.programs_root)
        legacy_stale_risk_ids = tuple(
            risk.id
            for risk in legacy_risks
            if assess_risk_staleness(risk, ctx.data_as_of.date())
        )
        fallback_health = support.build_health_summary(
            *args,
            **{
                **kwargs,
                "risks": legacy_risks,
                "risk_assessments": (),
                "stale_risk_ids": legacy_stale_risk_ids,
            },
        )
        return replace(fallback_health, risk_render_warning=_RISK_RENDER_WARNING)


def _append_carried_forward_workstreams(
    *,
    issue_number: int,
    bundle,
    workstream_data: tuple[WorkstreamData, ...],
    workstream_blurbs: dict[str, str],
    review_status: ReviewStatus,
    removed_section_ids: set[str],
    overrides_document=None,
    scorecards=(),
    source_footnotes: dict[str, str] | None = None,
) -> tuple[WorkstreamData, ...]:
    existing_section_ids = {workstream.section_id for workstream in workstream_data}
    review_lookup = {section.section_id: section.state for section in review_status.sections}
    # Build a lookup from section_id to (risk, note, scorecard_name) from overrides and scorecards.
    override_risk_lookup: dict[str, RiskLevel] = {}
    override_note_lookup: dict[str, str] = {}
    scorecard_lookup: dict[str, str] = {}
    if overrides_document is not None:
        from src.core.models import RiskLevel
        from src.core.jinja_filters import build_anchor
        for sc_overrides in overrides_document.scorecards:
            sc_name = sc_overrides.name
            for dim in sc_overrides.dimensions:
                dim_section_id = build_anchor(f"{sc_name}-{dim.name}")
                if dim.risk is not None:
                    override_risk_lookup[dim_section_id] = dim.risk
                if dim.note:
                    override_note_lookup[dim_section_id] = dim.note
                scorecard_lookup[dim_section_id] = sc_name
    source_footnotes = source_footnotes or {}
    carried_forward: list[WorkstreamData] = []
    for section_id, blurb in sorted(workstream_blurbs.items()):
        if section_id in existing_section_ids or section_id in removed_section_ids or not blurb.strip():
            continue
        carried_forward.append(
            WorkstreamData(
                section_id=section_id,
                title=_carried_forward_section_title(bundle, section_id, overrides_document=overrides_document),
                blurb=blurb.strip(),
                dependency_cascades=(),
                items=(),
                citations=(),
                review_state=review_lookup.get(f"ws:{section_id}", ReviewState.PENDING),
                summary=blurb.strip()[:200] if blurb.strip() else "Carried forward from trusted baseline.",
                risk=override_risk_lookup.get(section_id),
                scorecard_name=scorecard_lookup.get(section_id),
                note=override_note_lookup.get(section_id),
                total_items=0,
                blocked_count=0,
                overdue_count=0,
                unowned_count=0,
                edit_path=f"narratives/issue_{issue_number:03d}/ws_{section_id}.md",
                edit_line=1,
                narrative_empty=False,
                source_footnote=source_footnotes.get(section_id),
            )
        )
    return (*workstream_data, *carried_forward)


def _build_dashboard_chapter_warning_workstreams(
    *,
    issue_number: int,
    bundle,
    edition_type: EditionType,
    scorecards,
    scorecard_packets,
    workstream_blurbs: dict[str, str],
    review_status: ReviewStatus,
    items: tuple[WorkItem, ...],
    source_footnotes: dict[str, str] | None = None,
) -> tuple[WorkstreamData, ...]:
    chapter_contract = bundle.chapter_contract
    if chapter_contract is None:
        return ()

    chapter_defs = {
        chapter.id: chapter
        for chapter in chapter_contract.chapters_for(edition_type.value)
        if not chapter.chapter_exempt and chapter.id in workstream_blurbs
    }
    if not chapter_defs:
        return ()

    item_lookup = {item.id: item for item in items}
    source_footnotes = source_footnotes or {}
    review_lookup = {section.section_id: section.state for section in review_status.sections}
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

    workstreams: list[WorkstreamData] = []
    for chapter_id, chapter in chapter_defs.items():
        chapter_items: list[WorkItem] = []
        seen_item_ids: set[int] = set()
        chapter_risk = RiskLevel.UNKNOWN
        for dimension_id in chapter.dimensions:
            binding = chapter_contract.resolve_dimension(dimension_id)
            if binding is None:
                continue
            dimension = model_lookup.get(binding)
            packet = packet_lookup.get(binding)
            if dimension is not None and _risk_rank(dimension.risk) > _risk_rank(chapter_risk):
                chapter_risk = dimension.risk
            if packet is None:
                continue
            for item_id in packet.item_ids:
                if item_id in seen_item_ids or item_id not in item_lookup:
                    continue
                seen_item_ids.add(item_id)
                chapter_items.append(item_lookup[item_id])

        chapter_note = workstream_blurbs.get(chapter_id, "").strip()
        workstreams.append(
            WorkstreamData(
                section_id=chapter_id,
                title=chapter.title,
                blurb=chapter_note,
                dependency_cascades=(),
                items=tuple(chapter_items),
                citations=(),
                review_state=review_lookup.get(f"ws:{chapter_id}", ReviewState.PENDING),
                risk=chapter_risk,
                edit_path=f"narratives/issue_{issue_number:03d}/chapter_{chapter_id}.md",
                edit_line=1,
                narrative_empty=(chapter_id in workstream_blurbs and not chapter_note),
                source_footnote=source_footnotes.get(chapter_id),
            )
        )
    return tuple(workstreams)


def _risk_rank(level: RiskLevel) -> int:
    return {
        RiskLevel.UNKNOWN: 0,
        RiskLevel.DONE: 1,
        RiskLevel.LOW: 2,
        RiskLevel.MEDIUM: 3,
        RiskLevel.HIGH: 4,
    }[level]


def _carried_forward_section_title(bundle, section_id: str, overrides_document=None) -> str:
    # Prefer override label if set (e.g., "DB Key Refresh" instead of "PF Infra").
    if overrides_document is not None:
        for sc_overrides in overrides_document.scorecards:
            for dim in sc_overrides.dimensions:
                from src.core.jinja_filters import build_anchor
                if build_anchor(f"{sc_overrides.name}-{dim.name}") == section_id and dim.label:
                    return dim.label.rstrip("*").strip()
    for scorecard in bundle.config.scorecards:
        for dimension in scorecard.dimensions:
            if _detail_section_id(scorecard.name, dimension.name) == section_id:
                return dimension.name
    return _humanize_section_id(section_id)


def _detail_section_id(scorecard_name: str, dimension_name: str) -> str:
    from src.core.jinja_filters import build_anchor

    return build_anchor(f"{scorecard_name}-{dimension_name}")


def _route_chart_placements(
    kusto_sections: tuple[Any, ...],
    workstream_data: tuple[Any, ...],
) -> tuple[tuple[Any, ...], Any, tuple[Any, ...]]:
    """
    Route chart sections to their designated placements.

    Returns (updated_kusto_sections, exec_summary_chart, updated_workstreams) where:
      - attached charts are removed from kusto_sections and attached to
        the matching workstream via attached_charts
      - exec_summary chart (section_placement == "exec_summary") is returned
        separately for the exec_summary panel (not in kusto_sections)
      - standalone charts remain in kusto_sections
    """
    exec_summary_chart: Any = None
    remaining: list[Any] = []
    workstream_map: dict[str, Any] = {ws.section_id: ws for ws in workstream_data}

    for section in kusto_sections:
        placement = getattr(section, "section_placement", "standalone") or "standalone"
        if placement == "exec_summary":
            # First exec_summary chart wins; route to RenderContext.exec_summary_chart
            if exec_summary_chart is None:
                exec_summary_chart = section
            else:
                remaining.append(section)
        elif placement.startswith("workstream:"):
            ws_target = placement[len("workstream:") :]
            if ws_target in workstream_map:
                ws = workstream_map[ws_target]
                workstream_map[ws_target] = _with_attached_chart(ws, section)
            else:
                remaining.append(section)
        elif placement == "standalone":
            remaining.append(section)
        else:
            # Unknown placement — treat as standalone
            remaining.append(section)

    # Rebuild workstream_data with updated attached_charts
    updated_workstreams = tuple(workstream_map.values())

    # Re-assemble kusto_sections (not including exec_summary_chart or attached charts)
    updated_kusto = tuple(remaining)

    return updated_kusto, exec_summary_chart, updated_workstreams


def _with_attached_chart(ws: Any, section: Any) -> Any:
    """Return a new WorkstreamData with section appended to attached_charts."""
    from dataclasses import replace

    existing = getattr(ws, "attached_charts", ()) or ()
    return replace(ws, attached_charts=(*existing, section))


def _humanize_section_id(section_id: str) -> str:
    words = section_id.replace("_", " ").replace("-", " ").split()
    return " ".join(word.upper() if word.isupper() else word.capitalize() for word in words) or "This issue"


def _apply_kusto_fallbacks(
    *,
    ctx: StageContext,
    kusto_sections: tuple[Any, ...],
    telemetry_observations: tuple[TelemetryObservation, ...],
    kusto_warnings: tuple[str, ...],
    ado_vitality_available: bool,
) -> tuple[tuple[Any, ...], tuple[TelemetryObservation, ...], tuple[str, ...]]:
    if ctx.resolved_v2 is None:
        return kusto_sections, telemetry_observations, kusto_warnings

    kusto_sections, telemetry_observations, kusto_warnings = _apply_velocity_query_fallback(
        ctx=ctx,
        kusto_sections=kusto_sections,
        telemetry_observations=telemetry_observations,
        kusto_warnings=kusto_warnings,
    )
    if not ado_vitality_available:
        return kusto_sections, telemetry_observations, kusto_warnings

    return _apply_fleet_health_query_fallback(
        ctx=ctx,
        kusto_sections=kusto_sections,
        telemetry_observations=telemetry_observations,
        kusto_warnings=kusto_warnings,
    )


def _apply_velocity_query_fallback(
    *,
    ctx: StageContext,
    kusto_sections: tuple[Any, ...],
    telemetry_observations: tuple[TelemetryObservation, ...],
    kusto_warnings: tuple[str, ...],
) -> tuple[tuple[Any, ...], tuple[TelemetryObservation, ...], tuple[str, ...]]:
    if any(section.query_id == "velocity-p50" and not section.is_degraded for section in kusto_sections):
        return kusto_sections, telemetry_observations, kusto_warnings

    if not any(
        observation.query_id == "velocity-p50" and observation.execution_state == "degraded"
        for observation in telemetry_observations
    ):
        return kusto_sections, telemetry_observations, kusto_warnings

    if ctx.data_as_of is None or ctx.programs_root is None or ctx.started_at is None:
        return kusto_sections, telemetry_observations, kusto_warnings

    velocity_section = build_velocity_kusto_section(
        program_id=ctx.resolved_v2.program.id,
        item_ids=(item.id for item in ctx.items),
        as_of=ctx.data_as_of.date(),
        window_days=ctx.bundle.config.ado.date_window_days,
        programs_root=ctx.programs_root,
        section_id="velocity-p50",
        title="Deployment Velocity",
        query_id="velocity-p50",
        source_label="ADO trajectory fallback",
        confidence="medium",
        caveats=(
            "Derived from trajectory state transitions because live Kusto query velocity-p50 is unavailable.",
        ),
    )
    if velocity_section is None:
        return kusto_sections, telemetry_observations, kusto_warnings

    fallback_observation = TelemetryObservation(
        query_id="velocity-p50",
        cluster="ado://trajectory",
        database=ctx.resolved_v2.program.id,
        confidence=velocity_section.confidence,
        kusto_section_validates_slice=True,
        execution_state="fallback",
        observed_at=ctx.started_at,
        last_successful_fetch_at=ctx.started_at,
        message="Used ADO trajectory fallback because live Kusto query velocity-p50 was unavailable.",
    )

    updated_sections: list[Any] = []
    section_replaced = False
    for section in kusto_sections:
        if section.query_id == "velocity-p50":
            if not section_replaced:
                updated_sections.append(velocity_section)
                section_replaced = True
            continue
        updated_sections.append(section)
    if not section_replaced:
        updated_sections.append(velocity_section)

    updated_observations: list[TelemetryObservation] = []
    observation_replaced = False
    for observation in telemetry_observations:
        if observation.query_id == "velocity-p50":
            if not observation_replaced:
                updated_observations.append(fallback_observation)
                observation_replaced = True
            continue
        updated_observations.append(observation)
    if not observation_replaced:
        updated_observations.append(fallback_observation)

    updated_warnings = tuple(
        warning for warning in kusto_warnings if "velocity-p50" not in warning
    )
    return tuple(updated_sections), tuple(updated_observations), updated_warnings


def _apply_fleet_health_query_fallback(
    *,
    ctx: StageContext,
    kusto_sections: tuple[Any, ...],
    telemetry_observations: tuple[TelemetryObservation, ...],
    kusto_warnings: tuple[str, ...],
) -> tuple[tuple[Any, ...], tuple[TelemetryObservation, ...], tuple[str, ...]]:
    if any(section.query_id == "fleet-health" and not section.is_degraded for section in kusto_sections):
        return kusto_sections, telemetry_observations, kusto_warnings

    degraded_observation = next(
        (
            observation
            for observation in telemetry_observations
            if observation.query_id == "fleet-health" and observation.execution_state == "degraded"
        ),
        None,
    )
    if degraded_observation is None:
        return kusto_sections, telemetry_observations, kusto_warnings

    if ctx.started_at is None:
        return kusto_sections, telemetry_observations, kusto_warnings

    fallback_observation = TelemetryObservation(
        query_id="fleet-health",
        cluster="ado://vitality",
        database=ctx.resolved_v2.program.id,
        confidence=degraded_observation.confidence,
        kusto_section_validates_slice=degraded_observation.kusto_section_validates_slice,
        execution_state="fallback",
        observed_at=ctx.started_at,
        last_successful_fetch_at=ctx.started_at,
        message="Suppressed degraded Fleet Health Kusto card because ADO Vitality This Week already provides deterministic weekly health coverage.",
    )

    updated_sections = tuple(section for section in kusto_sections if section.query_id != "fleet-health")
    updated_observations: list[TelemetryObservation] = []
    observation_replaced = False
    for observation in telemetry_observations:
        if observation.query_id == "fleet-health":
            if not observation_replaced:
                updated_observations.append(fallback_observation)
                observation_replaced = True
            continue
        updated_observations.append(observation)
    if not observation_replaced:
        updated_observations.append(fallback_observation)

    updated_warnings = tuple(
        warning for warning in kusto_warnings if "fleet-health" not in warning
    )
    return updated_sections, tuple(updated_observations), updated_warnings
