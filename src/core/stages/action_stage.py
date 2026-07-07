from __future__ import annotations

from dataclasses import replace

from src.core.action_tracker import assess_action_staleness
from src.core.pipeline import StageContext
from src.core.program_fact_store import load_program_facts, project_action_items


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

        try:
            actions = _load_current_actions(ctx.resolved_v2.paths.program_id, programs_root=ctx.programs_root)
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


def _load_current_actions(program_id: str, *, programs_root):
    return project_action_items(
        load_program_facts(
            program_id,
            programs_root=programs_root,
            fact_types=("action.item",),
        )
    )