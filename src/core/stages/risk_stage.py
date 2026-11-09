from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, TypedDict

from src.core.pipeline import StageContext
from src.core.fact_sor_state import resolve_family_sor_mode
from src.core.program_fact_store import build_legacy_program_fact_snapshot, load_program_facts, project_risk_entries
from src.core.risk_register_engine import assess_risk_staleness
from src.core.stages.sor_gated_load import (
    record_report_family_selector, report_reality_scope, report_reality_scope_matches,
    retained_report_fact_snapshot, sor_gated_family_load,
)

_ALLOW_LEGACY_RISK_ROLLBACK_ENV = "VERTEX_REPORT_ALLOW_LEGACY_RISK_ROLLBACK"


class _ArchiveOptions(TypedDict, total=False):
    archive_root: Path


class RiskStage:
    def name(self) -> str:
        return "risk"

    def execute(self, ctx: StageContext) -> StageContext:
        if ctx.risks is not None:
            return ctx
        ctx = replace(
            ctx, risk_legacy_rollback_nonempty=False,
            risk_reality_empty_selected=False, risk_legacy_empty_selected=False,
        )
        if ctx.resolved_edition_type is not None and ctx.resolved_edition_type.value == "lookback":
            return replace(ctx, risks=(), risk_assessments=None, risk_lineage=None, stale_risk_ids=(), risk_warnings=())
        if ctx.resolved_v2 is None or ctx.programs_root is None or ctx.data_as_of is None:
            return replace(ctx, risks=(), risk_assessments=None, risk_lineage=None, stale_risk_ids=(), risk_warnings=())

        programs_root = ctx.programs_root
        data_as_of = ctx.data_as_of
        program_id = ctx.resolved_v2.paths.program_id
        fact_snapshot = retained_report_fact_snapshot(ctx, program_id)
        scope = report_reality_scope(ctx, program_id, fact_snapshot)
        reuse_reality = report_reality_scope_matches(ctx, scope)
        if not reuse_reality:
            ctx = replace(ctx, report_reality=None)
        sor_mode = resolve_family_sor_mode(program_id, "judgment", programs_root=programs_root)
        ctx = record_report_family_selector(ctx, scope, "judgment", sor_mode)
        reality_empty = False
        legacy_empty = False

        def read_reality_risks(reality: Any) -> tuple[Any, ...]:
            nonlocal reality_empty
            assessments = tuple(reality.risks())
            reality_empty = not assessments
            return assessments

        def read_current_risks() -> tuple[Any, ...]:
            nonlocal legacy_empty
            records = _load_current_risks(
                program_id, programs_root=programs_root,
                **({"archive_root": ctx.archive_root} if ctx.archive_root is not None else {}),
            )
            legacy_empty = not records
            return records

        risks, risk_assessments, warnings, lineage = sor_gated_family_load(
            program_id=program_id,
            # judgment, not "risk" -- risk/decision/assumption all share
            # this one authority family (fix-data-flow.md v1.5 / PS-15/16).
            family="judgment",
            programs_root=programs_root,
            reality_accessor=read_reality_risks,
            legacy_loader=read_current_risks,
            legacy_comparison_loader=lambda: _load_legacy_risks(
                program_id, programs_root=programs_root,
                **({"archive_root": ctx.archive_root} if ctx.archive_root is not None else {}),
            ),
            allow_legacy_rollback_env=_ALLOW_LEGACY_RISK_ROLLBACK_ENV,
            cross_check_label="risk",
            load_program_reality=(
                getattr(ctx.stage_support, "load_program_reality", None)
                if ctx.stage_support is not None
                else None
            ),
            as_of=data_as_of,
            edition_name=ctx.edition_name,
            archive_root=ctx.archive_root,
            fact_snapshot=fact_snapshot,
            reality=ctx.report_reality if reuse_reality else None,
            sor_mode=sor_mode,
        )

        stale_risk_ids = tuple(
            risk.id
            for risk in risks
            if assess_risk_staleness(risk, data_as_of.date())
        )
        return replace(
            ctx,
            risks=risks,
            risk_assessments=risk_assessments,
            risk_lineage=lineage,
            stale_risk_ids=stale_risk_ids,
            risk_warnings=warnings,
            # A successful reality read returns one assessment per risk.
            risk_legacy_rollback_nonempty=(
                sor_mode != "legacy" and bool(risks) and risk_assessments == ()
            ),
            risk_reality_empty_selected=reality_empty and not risks,
            risk_legacy_empty_selected=sor_mode == "legacy" and legacy_empty and not risks,
        )


def _load_current_risks(program_id: str, *, programs_root, archive_root: Path | None = None):
    archive_options: _ArchiveOptions = {"archive_root": archive_root} if archive_root is not None else {}
    return project_risk_entries(
        load_program_facts(
            program_id,
            programs_root=programs_root,
            fact_types=("risk.entry",),
            **archive_options,
        )
    )


def _load_legacy_risks(program_id: str, *, programs_root, archive_root: Path | None = None):
    archive_options: _ArchiveOptions = {"archive_root": archive_root} if archive_root is not None else {}
    return project_risk_entries(
        build_legacy_program_fact_snapshot(
            program_id,
            programs_root=programs_root,
            fact_types=("risk.entry",),
            **archive_options,
        )
    )
