from __future__ import annotations

from dataclasses import replace

from src.core.exceptions import ConfigError
from src.core.pipeline import StageContext
from src.core.program_fact_store import load_program_facts, project_risk_entries
from src.core.risk_register_engine import assess_risk_staleness


class RiskStage:
    def name(self) -> str:
        return "risk"

    def execute(self, ctx: StageContext) -> StageContext:
        if ctx.risks is not None:
            return ctx
        if ctx.resolved_edition_type is not None and ctx.resolved_edition_type.value == "lookback":
            return replace(ctx, risks=(), stale_risk_ids=(), risk_warnings=())
        if ctx.resolved_v2 is None or ctx.programs_root is None or ctx.data_as_of is None:
            return replace(ctx, risks=(), stale_risk_ids=(), risk_warnings=())

        try:
            risks = _load_current_risks(ctx.resolved_v2.paths.program_id, programs_root=ctx.programs_root)
        except ConfigError as exc:
            return replace(
                ctx,
                risks=(),
                stale_risk_ids=(),
                risk_warnings=(f"Risk register skipped: {exc}",),
            )

        stale_risk_ids = tuple(
            risk.id
            for risk in risks
            if assess_risk_staleness(risk, ctx.data_as_of.date())
        )
        return replace(
            ctx,
            risks=risks,
            stale_risk_ids=stale_risk_ids,
            risk_warnings=(),
        )


def _load_current_risks(program_id: str, *, programs_root):
    return project_risk_entries(
        load_program_facts(
            program_id,
            programs_root=programs_root,
            fact_types=("risk.entry",),
        )
    )