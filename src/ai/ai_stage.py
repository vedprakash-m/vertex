from __future__ import annotations

from dataclasses import replace

from src.ai.ai_mode import AIMode, get_ai_mode
from src.core.models import EditionType
from src.core.pipeline import StageContext


class AIStage:
    def name(self) -> str:
        return "ai"

    def execute(self, ctx: StageContext) -> StageContext:
        if ctx.resolved_edition_type == EditionType.LOOKBACK or ctx.render_exec_summary_text is not None:
            return ctx
        if (
            ctx.bundle is None
            or ctx.reports_root is None
            or ctx.resolved_edition_type is None
            or ctx.data_as_of is None
            or ctx.started_at is None
            or ctx.evidence_by_item is None
            or ctx.deltas is None
            or ctx.scorecards is None
            or ctx.scorecard_packets is None
            or ctx.overrides_document is None
            or ctx.continuity_chapters is None
            or ctx.exec_summary_text is None
            or ctx.loaded_exec_summary_text is None
            or ctx.programs_root is None
            or ctx.resolved_issue_number is None
            or ctx.workstream_blurbs is None
            or ctx.visible_section_ids is None
            or ctx.stage_support is None
        ):
            raise RuntimeError("NarrativeStage must execute before AIStage.")

        if get_ai_mode() == AIMode.DISABLED:
            return replace(
                ctx,
                ai_synthesis=support_build_disabled_result(ctx),
                render_exec_summary_text=ctx.exec_summary_text,
                render_workstream_blurbs=ctx.workstream_blurbs,
            )

        support = ctx.stage_support
        ai_context = None
        if not ctx.offline:
            ai_context = support.load_draft_ai_context(
                edition_name=ctx.edition_name,
                bundle=ctx.bundle,
                items=ctx.items,
                as_of=ctx.data_as_of,
                previous_snapshot=ctx.previous_snapshot,
                reports_root=ctx.reports_root,
                signal_context=ctx.signal_context,
            )
        ai_synthesis = support.synthesize_v2_ai_content(
            bundle=ctx.bundle,
            edition_name=ctx.edition_name,
            issue_number=ctx.resolved_issue_number,
            started_at=ctx.started_at,
            edition_type=ctx.resolved_edition_type,
            items=ctx.items,
            evidence_by_item=ctx.evidence_by_item,
            deltas=ctx.deltas,
            scorecards=ctx.scorecards,
            scorecard_packets=ctx.scorecard_packets,
            overrides_document=ctx.overrides_document,
            continuity_chapters=ctx.continuity_chapters,
            current_exec_summary_text=ctx.exec_summary_text,
            loaded_exec_summary_text=ctx.loaded_exec_summary_text,
            current_workstream_blurbs=ctx.workstream_blurbs,
            visible_section_ids=ctx.visible_section_ids,
            ai_program_context=ctx.bundle.program_context,
            ai_context=ai_context,
        )
        return replace(
            ctx,
            ai_synthesis=ai_synthesis,
            render_exec_summary_text=ai_synthesis.exec_summary_text,
            render_workstream_blurbs=ai_synthesis.workstream_blurbs,
        )


def support_build_disabled_result(ctx: StageContext):
    support = ctx.stage_support
    if support is None:
        raise RuntimeError("NarrativeStage must execute before AIStage.")
    return support.build_disabled_ai_synthesis_result(
        current_exec_summary_text=ctx.exec_summary_text or "",
        current_workstream_blurbs=ctx.workstream_blurbs or {},
    )
