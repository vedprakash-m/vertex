from __future__ import annotations

from datetime import timedelta
from dataclasses import dataclass, replace
from typing import Any

from src.core.archive_store import find_archive_index_inconsistencies
from src.core.ban_list_validator import find_ban_list_violations, find_structural_rule_violations
from src.core.continuation_contract import build_continuation_contract
from src.core.edition_resolver import get_program_output_dir
from src.core.hygiene_engine import evaluate_hygiene
from src.core.manifest_writer import build_run_manifest
from src.core.models import ReportData, RunManifest
from src.core.pipeline import StageContext
from src.core.persona_checker import run_persona_checks
from src.core.published_narrative_store import load_published_narratives
from src.core.quality_gates import combine_gate_reports, evaluate_bridge_gates, evaluate_chart_gates, evaluate_continuity_gates, evaluate_persona_signal_gates, evaluate_phase_1a_gates, evaluate_phase_1b_gates
from src.core.quality_gates import evaluate_phase_1c_gates
from src.core.signal_review import signal_is_approved_for_evidence
from src.core.scope_resolver import ScopeResolver
from src.core.section_proposal_store import load_stale_claim_ids
from src.core.store_factory import build_signal_store_for_program_id
from src.core.trusted_baseline_store import load_trusted_baseline
from src.core.verbosity_enforcer import enforce_verbosity
from src.core.voice_validator import find_voice_violations


@dataclass(frozen=True, slots=True)
class ValidationStageState:
    report: ReportData
    manifest: RunManifest
    warnings: tuple[str, ...]
    draft_readiness: Any
    exit_code: int
    persona_coverage: Any = None


class ValidationStage:
    def name(self) -> str:
        return "validation"

    def execute(self, ctx: StageContext) -> StageContext:
        if ctx.resolved_edition_type is not None and ctx.resolved_edition_type.value == "lookback":
            return ctx
        if ctx.validation_state is not None:
            return ctx
        if (
            ctx.bundle is None
            or ctx.reports_root is None
            or ctx.resolved_issue_number is None
            or ctx.started_at is None
            or ctx.data_as_of is None
            or ctx.dimension_risks is None
            or ctx.loaded_narratives is None
            or ctx.visible_section_ids is None
            or ctx.ai_synthesis is None
            or ctx.render_state is None
            or ctx.stage_support is None
            or ctx.exec_summary_text is None
            or ctx.workstream_blurbs is None
            or ctx.ado_calls is None
            or ctx.archive_root is None
            or ctx.editions_root is None
            or ctx.programs_root is None
            or ctx.programs_root is None
        ):
            raise RuntimeError("RenderStage must execute before ValidationStage.")

        support = ctx.stage_support
        render_state = ctx.render_state
        exec_summary_text = ctx.exec_summary_text
        workstream_blurbs = ctx.workstream_blurbs
        ado_calls = ctx.ado_calls
        archive_root = ctx.archive_root
        editions_root = ctx.editions_root
        programs_root = ctx.programs_root

        ban_violations = find_ban_list_violations(render_state.rendered_strings, ctx.bundle.editorial_rules)
        scope_resolver = ScopeResolver(
            exec_summary_text=exec_summary_text,
            workstream_blurbs=workstream_blurbs,
            loaded_narratives=ctx.loaded_narratives,
            rendered_html=render_state.html_body,
            subject_line=render_state.title,
        )
        structural_violations = find_structural_rule_violations(
            resolver=scope_resolver,
            editorial_rules=ctx.bundle.editorial_rules,
        )
        skip_persona = (
            getattr(ctx.bundle, "persona_registry", None) is not None
            and ctx.bundle.persona_registry.enforcement.enabled
        )
        voice_violations = find_voice_violations(
            editorial_rules=ctx.bundle.editorial_rules,
            edition_name=ctx.edition_name,
            exec_summary_text=exec_summary_text,
            workstream_blurbs=workstream_blurbs,
            program_context=ctx.bundle.program_context,
            skip_persona_violations=skip_persona,
        )
        verbosity_violations = enforce_verbosity(
            workstream_blurbs=workstream_blurbs,
            exec_summary_text=exec_summary_text,
            scorecard_summaries={dimension.name: dimension.summary for dimension in ctx.dimension_risks},
            subject_line=render_state.title,
            verbosity=ctx.bundle.editorial_rules.verbosity,
            edition_type=ctx.resolved_edition_type,
        )
        workstream_citations = {
            workstream.section_id: workstream.citations
            for workstream in render_state.workstream_data
        }
        hygiene_warnings = evaluate_hygiene(
            workstream_blurbs=workstream_blurbs,
            workstream_citations=workstream_citations,
            exec_summary_text=exec_summary_text,
            exec_summary_citations=render_state.exec_summary_citations,
            scorecard=ctx.dimension_risks,
        )
        persona_coverage = run_persona_checks(
            registry=getattr(ctx.bundle, "persona_registry", None),
            exec_summary_text=exec_summary_text,
            workstream_blurbs=workstream_blurbs,
            loaded_narratives=ctx.loaded_narratives,
            rendered_html=render_state.html_body,
            subject_line=render_state.title,
            ban_rule_results=ban_violations,
            structural_rule_results=structural_violations,
            editorial_rules=ctx.bundle.editorial_rules,
            overrides=ctx.overrides_document,
            program_phase=getattr(ctx.bundle.program_context, "current_phase", None) if ctx.bundle.program_context is not None else None,
            evaluation_date=ctx.data_as_of.date(),
            published_baseline=_load_published_baseline_map(ctx),
        )
        report = ReportData(
            issue_number=render_state.report.issue_number,
            edition=render_state.report.edition,
            generated_at=render_state.report.generated_at,
            ado_data_as_of=render_state.report.ado_data_as_of,
            program=render_state.report.program,
            items=render_state.report.items,
            deltas=render_state.report.deltas,
            scorecard=render_state.report.scorecard,
            scorecard_deltas=render_state.report.scorecard_deltas,
            exec_summary_text=render_state.report.exec_summary_text,
            workstream_blurbs=render_state.report.workstream_blurbs,
            freshness=render_state.report.freshness,
            hygiene_warnings=hygiene_warnings,
            review_status=render_state.report.review_status,
            manifest_id=render_state.report.manifest_id,
        )
        manifest_metadata = _build_manifest_metadata(ctx, render_state)

        provisional_manifest = build_run_manifest(
            manifest_id=report.manifest_id,
            issue_number=ctx.resolved_issue_number,
            edition=ctx.edition_name,
            started_at=ctx.started_at,
            ended_at=support.now_utc(),
            config_payload=ctx.bundle.config,
            snapshot=render_state.snapshot,
            html_content=render_state.html_body,
            markdown_content=render_state.markdown_body,
            ado_calls=ado_calls,
            ai_calls=ctx.ai_synthesis.ai_calls,
            ai_cost_usd=ctx.ai_synthesis.ai_cost_usd,
            ai_cost_by_model=ctx.ai_synthesis.ai_cost_by_model,
            freshness_summary={
                "blocks": render_state.freshness_report.blocks,
                "warns": render_state.freshness_report.warns,
                "infos": render_state.freshness_report.infos,
            },
            qg_results={},
            git_sha=support.read_git_sha(),
            metadata=manifest_metadata,
        )
        journal_signals, approved_signals = _load_gate_signals(ctx)
        qg_phase_1a = evaluate_phase_1a_gates(
            ban_list_violations=ban_violations,
            verbosity_violations=verbosity_violations,
            manifest=provisional_manifest,
            expected_snapshot_hash=provisional_manifest.snapshot_hash,
            dimension_risks=ctx.dimension_risks,
            program_id=ctx.bundle.program.id,
            edition_name=ctx.edition_name,
            issue_number=ctx.resolved_issue_number,
            archive_root=archive_root,
            programs_root=programs_root,
        )
        newsletter_items = support.build_newsletter_scoped_items(
            bundle=ctx.bundle,
            edition_type=ctx.resolved_edition_type,
            items=ctx.items,
            scorecards=ctx.scorecards,
            scorecard_packets=ctx.scorecard_packets,
            overrides_document=ctx.overrides_document,
            continuity_chapters=ctx.continuity_chapters,
            visible_section_ids=ctx.visible_section_ids,
        )
        narrative_covered_item_ids = support.build_newsletter_narrative_covered_item_ids(
            bundle=ctx.bundle,
            edition_type=ctx.resolved_edition_type,
            items=ctx.items,
            scorecards=ctx.scorecards,
            scorecard_packets=ctx.scorecard_packets,
            overrides_document=ctx.overrides_document,
            continuity_chapters=ctx.continuity_chapters,
            visible_section_ids=ctx.visible_section_ids,
            loaded_narratives=ctx.loaded_narratives,
        )
        qg_phase_1b = evaluate_phase_1b_gates(
            freshness_report=render_state.freshness_report,
            items=ctx.items,
            publishable_item_ids=tuple(item.id for item in newsletter_items),
            covered_item_ids=narrative_covered_item_ids,
            as_of=ctx.data_as_of,
            deltas=ctx.deltas,
            edition_name=ctx.edition_name,
            issue_number=ctx.resolved_issue_number,
            workstream_blurbs=ctx.workstream_blurbs,
            program_context=ctx.bundle.program_context,
            dimension_risks=ctx.dimension_risks,
            overrides_document=ctx.overrides_document,
            approved_signals=approved_signals,
            narratives=ctx.loaded_narratives,
            journal_signals=journal_signals,
            program_id=(ctx.resolved_v2.program.id if ctx.resolved_v2 is not None else None),
            program_maturity_level=(ctx.resolved_v2.program.maturity_level if ctx.resolved_v2 is not None else 0),
            workstreams=(ctx.resolved_v2.workstreams if ctx.resolved_v2 is not None else ()),
            scorecards=(ctx.resolved_v2.scorecards if ctx.resolved_v2 is not None else ()),
            archive_root=archive_root,
            programs_root=programs_root,
            stale_claim_ids=(
                load_stale_claim_ids(ctx.resolved_v2.program.id, ctx.resolved_issue_number, programs_root=programs_root)
                if ctx.resolved_v2 is not None
                else ()
            ),
        )
        qg_phase_1c = evaluate_phase_1c_gates(
            hygiene_warnings=hygiene_warnings,
            review_status=render_state.review_status,
            review_required=ctx.bundle.review.required,
            archive_inconsistencies=find_archive_index_inconsistencies(ctx.edition_name, archive_root=archive_root),
        )
        # Phase 3: Chart pipeline quality gates (QG-20, QG-21, QG-22)
        edition_charts_enabled = getattr(ctx.bundle.config, "charts", None) is not None and (
            getattr(ctx.bundle.config.charts, "enabled", True)
        )
        qg_charts = evaluate_chart_gates(
            render_state.kusto_sections,
            current_time=ctx.started_at,
            edition_charts_enabled=edition_charts_enabled,
        )
        qg_reports = [qg_phase_1a, qg_phase_1b, qg_phase_1c, qg_charts, evaluate_persona_signal_gates(persona_coverage)]
        if support.is_continuity_layout(ctx.bundle):
            qg_reports.append(
                evaluate_continuity_gates(
                    html_content=render_state.html_body,
                    issue_number=ctx.resolved_issue_number,
                )
            )
        trusted_baseline = load_trusted_baseline(
            ctx.edition_name,
            editions_root=editions_root,
            programs_root=programs_root,
        )
        qg_reports.append(
            evaluate_bridge_gates(
                continuation_contract=build_continuation_contract(
                    edition_name=ctx.edition_name,
                    issue_number=ctx.resolved_issue_number,
                    started_at=ctx.started_at,
                    reports_root=ctx.reports_root,
                    archive_root=archive_root,
                    editions_root=editions_root,
                    programs_root=programs_root,
                    overrides_document=ctx.overrides_document,
                    workstream_data=render_state.workstream_data,
                    output_dir=get_program_output_dir(ctx.edition_name, programs_root=programs_root),
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
                ),
                narratives=ctx.loaded_narratives,
                review_status=render_state.review_status,
                bridge_graduated=(trusted_baseline.bridge_graduated if trusted_baseline is not None else False),
            )
        )
        qg_report = combine_gate_reports(*qg_reports)
        draft_readiness = support.build_draft_readiness(
            edition_name=ctx.edition_name,
            qg_report=qg_report,
            items=newsletter_items,
            covered_item_ids=narrative_covered_item_ids,
            dimension_risks=ctx.dimension_risks,
            visible_section_ids=ctx.visible_section_ids,
            loaded_narratives=ctx.loaded_narratives,
            as_of=ctx.data_as_of,
            reports_root=ctx.reports_root,
            is_continuity=(support.is_continuity_layout(ctx.bundle) or bool(ctx.continuity_chapters)),
        )
        final_manifest = build_run_manifest(
            manifest_id=report.manifest_id,
            issue_number=ctx.resolved_issue_number,
            edition=ctx.edition_name,
            started_at=ctx.started_at,
            ended_at=support.now_utc(),
            config_payload=ctx.bundle.config,
            snapshot=render_state.snapshot,
            html_content=render_state.html_body,
            markdown_content=render_state.markdown_body,
            ado_calls=ado_calls,
            ai_calls=ctx.ai_synthesis.ai_calls,
            ai_cost_usd=ctx.ai_synthesis.ai_cost_usd,
            ai_cost_by_model=ctx.ai_synthesis.ai_cost_by_model,
            freshness_summary={
                "blocks": render_state.freshness_report.blocks,
                "warns": render_state.freshness_report.warns,
                "infos": render_state.freshness_report.infos,
            },
            qg_results=qg_report.qg_results,
            git_sha=support.read_git_sha(),
            metadata={
                **manifest_metadata,
                "draft_readiness": draft_readiness.to_payload(),
            },
        )

        blocking_warnings = (
            tuple(support.format_ban_violation(violation) for violation in ban_violations)
            + tuple(_format_structural_violation(violation) for violation in structural_violations if violation.severity == "block")
            + tuple(f"{violation.location}: {violation.message}" for violation in verbosity_violations)
            + tuple(f"{violation.location}: {violation.message}" for violation in voice_violations)
            + tuple(_format_persona_result(result) for result in (persona_coverage.blocks if persona_coverage is not None else ()))
        )
        gate_warnings = tuple(
            result.message
            for result in qg_report.failing_results
            if result.gate_id not in {"QG-4", "QG-5"}
        )
        non_blocking_warnings = _dedupe_warnings(
            tuple(hygiene_warnings)
            + tuple(_format_structural_violation(violation) for violation in structural_violations if violation.severity == "warn")
            + tuple(render_state.kusto_warnings)
            + render_state.narrative_warnings
            + ctx.ai_synthesis.warnings
            + ctx.milestone_warnings
            + gate_warnings
            + tuple(_format_persona_result(result) for result in (persona_coverage.warnings if persona_coverage is not None else ()))
        )
        has_blockers = bool(blocking_warnings)
        has_warnings = bool(non_blocking_warnings)
        warning_exit_code = 3 if has_blockers else 2 if has_warnings else 0
        exit_code = max(warning_exit_code, 0 if qg_report.passed else qg_report.exit_code)

        return replace(
            ctx,
            validation_state=ValidationStageState(
                report=report,
                manifest=final_manifest,
                warnings=blocking_warnings + non_blocking_warnings,
                draft_readiness=draft_readiness,
                exit_code=exit_code,
                persona_coverage=persona_coverage,
            ),
        )


def _format_structural_violation(violation: Any) -> str:
    hint = f" Hint: {violation.autofix_hint}" if getattr(violation, "autofix_hint", None) else ""
    matched = f" ({violation.matched_text})" if getattr(violation, "matched_text", "") else ""
    return f"{violation.location}: structural rule {violation.rule_id} failed{matched}.{hint}".strip()


def _format_persona_result(result: Any) -> str:
    detail = f" matched {result.matched_text!r}" if getattr(result, "matched_text", None) else ""
    hint = f" Hint: {result.remediation_hint}" if getattr(result, "remediation_hint", None) else ""
    return f"{result.location}: persona {result.persona_id}/{result.check_id}: {result.message}{detail}.{hint}".strip()


def _load_gate_signals(ctx: StageContext) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    if (
        ctx.bundle is None
        or ctx.resolved_v2 is None
        or ctx.programs_root is None
        or ctx.data_as_of is None
    ):
        return (), ()

    signal_store = build_signal_store_for_program_id(
        ctx.resolved_v2.program.id,
        programs_root=ctx.programs_root,
    )
    journal_signals = signal_store.read(
        ctx.resolved_v2.program.id,
        end=ctx.data_as_of,
    )
    evidence_window_start = ctx.data_as_of - timedelta(days=ctx.bundle.config.ado.date_window_days)
    review_states = signal_store.read_reviews(ctx.resolved_v2.program.id)
    approved_signals = tuple(
        signal
        for signal in journal_signals
        if signal.timestamp >= evidence_window_start
        and signal_is_approved_for_evidence(signal, review_states)
    )
    return journal_signals, approved_signals


def _dedupe_warnings(warnings: tuple[str, ...]) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for warning in warnings:
        if warning in seen:
            continue
        seen.add(warning)
        ordered.append(warning)
    return tuple(ordered)


def _load_published_baseline_map(ctx: StageContext) -> dict[str, str] | None:
    trusted_issue_number = getattr(ctx, "trusted_baseline_issue_number", None)
    if trusted_issue_number is None or ctx.archive_root is None:
        return None
    published = load_published_narratives(
        ctx.edition_name,
        trusted_issue_number,
        archive_root=ctx.archive_root,
    )
    if not published:
        return None
    baseline: dict[str, str] = {}
    for filename, text in published.items():
        if filename == "exec_summary.md":
            baseline["exec_summary"] = text
            continue
        if filename.startswith("ws_") and filename.endswith(".md"):
            section_id = filename[3:-3]
            baseline[f"narrative:{section_id}"] = text
    return baseline or None


def _build_manifest_metadata(ctx: StageContext, render_state: Any) -> dict[str, Any]:
    return {
        "suggested_subject": render_state.email_subject,
        "suggested_preheader": render_state.email_preheader,
        "subject_signal": render_state.subject_signal,
        "productivity_dividend_hours": render_state.productivity_dividend_hours,
        "forecast_summary": (render_state.forecast.summary if render_state.forecast is not None else None),
        "forecast_confidence": (render_state.forecast.confidence.value if render_state.forecast is not None else None),
        "forecast_sources": (list(render_state.forecast.source_item_ids) if render_state.forecast is not None else []),
        "milestone_assessments": _serialize_milestone_assessments(ctx),
        "ai_safety": _build_ai_safety_metadata(ctx),
    }


def _build_ai_safety_metadata(ctx: StageContext) -> dict[str, Any]:
    if ctx.bundle is None:
        return {}

    budget_usd = round(float(ctx.bundle.config.ai.budget_usd_per_run), 6)

    if ctx.ai_synthesis is None:
        # AI was not invoked this run — emit zero-spend config-level metadata
        # so manifest consumers can still show budget ceiling and enabled state.
        return {
            "enabled": bool(ctx.bundle.config.ai.enabled),
            "trace_run_id": None,
            "budget_usd": budget_usd,
            "spent_usd": 0.0,
            "remaining_usd": budget_usd,
            "ai_calls": 0,
            "within_budget": True,
            "budget_exceeded": False,
        }

    spent_usd = round(float(ctx.ai_synthesis.ai_cost_usd), 6)
    remaining_usd = round(budget_usd - spent_usd, 6)
    budget_exceeded = spent_usd > budget_usd
    return {
        "enabled": bool(ctx.bundle.config.ai.enabled),
        "trace_run_id": ctx.ai_synthesis.trace_run_id,
        "budget_usd": budget_usd,
        "spent_usd": spent_usd,
        "remaining_usd": remaining_usd,
        "ai_calls": int(ctx.ai_synthesis.ai_calls),
        "within_budget": not budget_exceeded,
        "budget_exceeded": budget_exceeded,
    }


def _serialize_milestone_assessments(ctx: StageContext) -> list[dict[str, Any]]:
    assessments = ctx.milestone_assessments or ()
    milestones = {milestone.id: milestone for milestone in (ctx.milestones or ())}
    return [
        {
            "milestone_id": assessment.milestone_id,
            "milestone_name": (milestones[assessment.milestone_id].name if assessment.milestone_id in milestones else None),
            "declared_status": (milestones[assessment.milestone_id].status.value if assessment.milestone_id in milestones else None),
            "target_date": (
                milestones[assessment.milestone_id].target_date.isoformat()
                if assessment.milestone_id in milestones
                else None
            ),
            "completion_date": (assessment.completion_date.isoformat() if assessment.completion_date is not None else None),
            "computed_health": assessment.computed_health.value,
            "blocked_criteria": list(assessment.blocked_criteria),
            "slip_probability": assessment.slip_probability,
            "critical_path": assessment.critical_path,
            "confidence": assessment.confidence.value,
            "reasoning": assessment.reasoning,
        }
        for assessment in assessments
    ]
