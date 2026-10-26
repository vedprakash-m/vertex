from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TypedDict

from src.core.action_tracker import assess_action_staleness
from src.core.pipeline import StageContext
from src.core.program_fact_store import load_program_facts, project_action_items
from src.core.stages.sor_gated_load import retained_report_fact_snapshot


class _ArchiveOptions(TypedDict, total=False):
    archive_root: Path


class ActionStage:
    def name(self) -> str:
        return "action"

    def execute(self, ctx: StageContext) -> StageContext:
        if ctx.actions is not None:
            return ctx
        if ctx.resolved_edition_type is not None and ctx.resolved_edition_type.value == "lookback":
            return replace(ctx, actions=(), overdue_action_ids=(), action_warnings=())
        if ctx.resolved_v2 is None or ctx.programs_root is None or ctx.data_as_of is None:
            return replace(ctx, actions=(), overdue_action_ids=(), action_warnings=())

        program_id = ctx.resolved_v2.paths.program_id
        fact_snapshot = retained_report_fact_snapshot(ctx, program_id)
        try:
            actions = (
                project_action_items(fact_snapshot)
                if fact_snapshot is not None
                else _load_current_actions(
                    program_id, programs_root=ctx.programs_root,
                    **({"archive_root": ctx.archive_root} if ctx.archive_root is not None else {}),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            return replace(
                ctx,
                actions=(),
                overdue_action_ids=(),
                action_warnings=(f"Action register skipped: {exc}",),
            )

        overdue_action_ids = tuple(
            action.id
            for action in assess_action_staleness(actions, ctx.data_as_of.date())
        )
        return replace(
            ctx,
            actions=actions,
            overdue_action_ids=overdue_action_ids,
            action_warnings=(),
        )


def _load_current_actions(program_id: str, *, programs_root, archive_root: Path | None = None):
    archive_options: _ArchiveOptions = {"archive_root": archive_root} if archive_root is not None else {}
    return project_action_items(
        load_program_facts(
            program_id,
            programs_root=programs_root,
            fact_types=("action.item",),
            **archive_options,
        )
    )