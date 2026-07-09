from __future__ import annotations

import os
from dataclasses import replace
from typing import Any

from src.core.exceptions import ConfigError
from src.core.fact_sor_state import resolve_family_sor_mode
from src.core.milestone_engine import assess_milestone_health, build_critical_path
from src.core.models_v2 import Dependency, LegacyDependency, Milestone
from src.core.pipeline import StageContext
from src.core.program_fact_store import load_program_facts, project_dependencies, project_milestones
from src.core.store_factory import build_trajectory_store_for_program_id

_ALLOW_LEGACY_MILESTONE_ROLLBACK_ENV = "VERTEX_REPORT_ALLOW_LEGACY_MILESTONE_ROLLBACK"


class MilestoneStage:
    def name(self) -> str:
        return "milestone"

    def execute(self, ctx: StageContext) -> StageContext:
        if ctx.milestone_assessments is not None:
            return ctx
        if ctx.resolved_edition_type is not None and ctx.resolved_edition_type.value == "lookback":
            return replace(ctx, milestones=(), milestone_assessments=(), milestone_warnings=())
        if ctx.resolved_v2 is None or ctx.programs_root is None or ctx.data_as_of is None:
            return replace(ctx, milestones=(), milestone_assessments=(), milestone_warnings=())

        program_id = ctx.resolved_v2.paths.program_id

        # S-8a: read through ProgramReality when the milestone authority family is non-legacy.
        # This must honor per-family flips even when the program default remains legacy.
        sor_mode = resolve_family_sor_mode(program_id, "workitem.state", programs_root=ctx.programs_root)
        if sor_mode != "legacy":
            milestones, dependencies, warnings, lineage = _load_milestones_via_reality(
                program_id,
                programs_root=ctx.programs_root,
                load_program_reality=(
                    getattr(ctx.stage_support, "load_program_reality", None)
                    if ctx.stage_support is not None
                    else None
                ),
                as_of=ctx.data_as_of,
                edition_name=ctx.edition_name,
                archive_root=ctx.archive_root,
            )
        else:
            try:
                milestones = _load_current_milestones(program_id, programs_root=ctx.programs_root)
                dependencies = _load_current_dependencies(program_id, programs_root=ctx.programs_root)
            except ConfigError as exc:
                return replace(
                    ctx,
                    milestones=(),
                    milestone_assessments=(),
                    milestone_lineage={},
                    milestone_warnings=(f"Milestones skipped: {exc}",),
                )
            warnings = ()
            lineage = {}

        if not milestones:
            return replace(ctx, milestones=(), milestone_assessments=(), milestone_lineage=lineage, milestone_warnings=warnings)

        trajectory_store = build_trajectory_store_for_program_id(
            program_id,
            programs_root=ctx.programs_root,
        )
        trajectories = {
            item_id: trajectory_store.read(program_id, item_id)
            for milestone in milestones
            for item_id in milestone.linked_work_item_ids
        }
        critical_path_ids = {milestone.id for milestone in build_critical_path(milestones, dependencies)}
        assessments = tuple(
            replace(
                assess_milestone_health(
                    milestone,
                    ctx.items,
                    trajectories,
                    ctx.data_as_of,
                ),
                critical_path=milestone.id in critical_path_ids,
            )
            for milestone in milestones
        )
        return replace(
            ctx,
            milestones=milestones,
            milestone_assessments=assessments,
            milestone_lineage=lineage,
            milestone_warnings=warnings,
        )


def _load_milestones_via_reality(
    program_id: str,
    *,
    programs_root,
    load_program_reality=None,
    as_of=None,
    edition_name: str | None = None,
    archive_root=None,
) -> tuple[tuple[Milestone, ...], tuple[Dependency | LegacyDependency, ...], tuple[str, ...], dict[str, dict[str, str | None]]]:
    """S-8a: load milestones from ProgramReality (shadow/primary SoR paths).

    Returns (milestones, dependencies, warnings).  Falls back to the legacy
    path if ProgramReality is unavailable.  The report facade injects
    ``ProgramReality.load`` so the pipeline has one explicit read facade.
    """
    try:
        if load_program_reality is None:
            from src.core.program_reality import ProgramReality  # noqa: PLC0415

            kwargs = {
                "programs_root": programs_root,
                "as_of": as_of,
                "edition_name": edition_name,
            }
            if archive_root is not None:
                kwargs["archive_root"] = archive_root
            reality = ProgramReality.load(program_id, **kwargs)
        else:
            reality = load_program_reality(
                program_id,
                programs_root=programs_root,
                as_of=as_of,
                edition_name=edition_name,
                archive_root=archive_root,
            )
        milestone_assessments = tuple(reality.milestones())
        milestones = tuple(fa.record for fa in milestone_assessments)
        lineage = _milestone_lineage_map(milestone_assessments)
        dependency_assessments = tuple(reality.dependencies())
        dependencies = tuple(assessment.record for assessment in dependency_assessments)
        return milestones, dependencies, (), lineage
    except ConfigError as exc:
        return (), (), (f"Milestones skipped (reality): {exc}",), {}
    except Exception as exc:  # noqa: BLE001
        if not _legacy_milestone_rollback_enabled():
            raise ConfigError(
                "ProgramReality milestone read failed while workitem.state SoR is non-legacy. "
                f"Set {_ALLOW_LEGACY_MILESTONE_ROLLBACK_ENV}=1 to use the audited legacy rollback path."
            ) from exc
        fallback_milestones = _load_current_milestones(program_id, programs_root=programs_root)
        fallback_deps = _load_current_dependencies(program_id, programs_root=programs_root)
        return (
            fallback_milestones,
            fallback_deps,
            (
                "[S-8a] degraded to legacy milestone source via audited rollback flag; "
                f"{_ALLOW_LEGACY_MILESTONE_ROLLBACK_ENV}=1; ProgramReality error: {exc}",
            ),
            {},
        )


def _milestone_lineage_map(milestone_assessments: tuple[Any, ...]) -> dict[str, dict[str, str | None]]:
    lineage_by_id: dict[str, dict[str, str | None]] = {}
    for assessment in milestone_assessments:
        record = getattr(assessment, "record", None)
        milestone_id = getattr(record, "id", None)
        if not milestone_id:
            continue
        lineage = getattr(assessment, "lineage", None)
        if lineage is None:
            continue
        lineage_by_id[str(milestone_id)] = {
            "source_document_key": getattr(lineage, "source_document_key", None),
            "approval_event_id": getattr(lineage, "approval_event_id", None),
            "truth_level": getattr(getattr(assessment, "truth_level", None), "value", None),
            "disputed": "true" if getattr(assessment, "disputed", False) else "false",
            "stale": "true" if getattr(assessment, "stale", False) else "false",
        }
    return lineage_by_id


def _legacy_milestone_rollback_enabled() -> bool:
    return os.environ.get(_ALLOW_LEGACY_MILESTONE_ROLLBACK_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _load_current_milestones(program_id: str, *, programs_root) -> tuple[Milestone, ...]:
    return project_milestones(
        load_program_facts(
            program_id,
            programs_root=programs_root,
            fact_types=("milestone.entry",),
        )
    )


def _load_current_dependencies(program_id: str, *, programs_root) -> tuple[Dependency | LegacyDependency, ...]:
    return project_dependencies(
        load_program_facts(
            program_id,
            programs_root=programs_root,
            fact_types=("dependency.link",),
        )
    )
