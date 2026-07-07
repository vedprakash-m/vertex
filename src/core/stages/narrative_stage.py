from __future__ import annotations

from dataclasses import replace

from src.core.continuation_contract import build_bridge_section_roster_ids
from src.core.exceptions import ConfigError
from src.core.jinja_filters import build_anchor
from src.core.models import EditionType
from src.core.narrative_store import REMOVED_SECTION_MARKER, build_workstream_narrative_history, load_narrative_seeding_state, load_narratives, merge_narratives
from src.core.narrative_store import narrative_seeding_disabled
from src.core.narrative_store import seed_narratives_from_prior
from src.core.narrative_store import sync_published_baseline_to_target
from src.core.pipeline import StageContext
from src.core.workstream_registry import build_workstream_issue_snapshot_from_packets, load_workstream_registry, section_id_for_slice_contract


class NarrativeStage:
    def name(self) -> str:
        return "narrative"

    def execute(self, ctx: StageContext) -> StageContext:
        if ctx.resolved_edition_type == EditionType.LOOKBACK or ctx.narratives_dir is not None:
            return ctx
        if (
            ctx.bundle is None
            or ctx.reports_root is None
            or ctx.archive_root is None
            or ctx.resolved_issue_number is None
            or ctx.resolved_edition_type is None
            or ctx.overrides_document is None
            or ctx.scorecards is None
            or ctx.scorecard_packets is None
            or ctx.scorecard_deltas is None
            or ctx.deltas is None
            or ctx.default_exec_summary is None
            or ctx.stage_support is None
        ):
            raise RuntimeError("ComputeStage must execute before NarrativeStage.")

        support = ctx.stage_support
        top_items = support.build_top_items(ctx.overrides_document, ctx.scorecards)
        auto_suggestions = support.build_auto_suggested_top_items(ctx.scorecard_deltas, ctx.scorecard_packets)
        is_continuity_layout = support.is_continuity_layout(ctx.bundle)
        raw_section_templates = support.build_workstream_templates(
            issue_number=ctx.resolved_issue_number,
            bundle=ctx.bundle,
            items=ctx.items,
            scorecards=ctx.scorecards,
            scorecard_packets=ctx.scorecard_packets,
            overrides_document=ctx.overrides_document,
        )
        continuity_chapters = support.visible_continuity_chapters(ctx.bundle, ctx.resolved_edition_type)
        chapter_surface_chapters = continuity_chapters
        if (
            not is_continuity_layout
            and ctx.resolved_v2 is not None
            and ctx.bundle.chapter_contract is not None
            and ctx.resolved_edition_type in {EditionType.DETAILED, EditionType.FOCUSED}
        ):
            chapter_surface_chapters = tuple(
                chapter
                for chapter in ctx.bundle.chapter_contract.chapters_for(ctx.resolved_edition_type.value)
                if not chapter.chapter_exempt
            )
        chapter_surface_active = is_continuity_layout or (
            bool(chapter_surface_chapters)
            and not ctx.section_filter_ids
        )
        if ctx.section_filter_ids and chapter_surface_chapters:
            chapter_section_ids = {chapter.id for chapter in chapter_surface_chapters}
            chapter_surface_active = set(ctx.section_filter_ids).issubset(chapter_section_ids)
        active_chapters = chapter_surface_chapters if chapter_surface_active else ()
        section_templates = (
            support.build_chapter_templates(ctx.resolved_issue_number, active_chapters)
            if is_continuity_layout
            else {
                **raw_section_templates,
                **(
                    support.build_chapter_templates(ctx.resolved_issue_number, active_chapters)
                    if active_chapters
                    else {}
                ),
            }
        )
        narrative_templates = {
            "exec_summary.md": (
                support.build_continuity_exec_summary_template(
                    issue_number=ctx.resolved_issue_number,
                    program_objective=(ctx.bundle.program_context.objective if ctx.bundle.program_context is not None else None),
                    auto_suggestions=auto_suggestions,
                    scorecard_deltas=ctx.scorecard_deltas,
                    dimension_risks=ctx.dimension_risks,
                )
                if is_continuity_layout
                else support.build_exec_summary_template(
                    ctx.resolved_issue_number,
                    layout_mode=ctx.bundle.config.layout_mode,
                )
            ),
            **section_templates,
        }
        seeding_disabled = narrative_seeding_disabled(
            ctx.edition_name,
            ctx.resolved_issue_number,
            reports_root=ctx.reports_root,
        )
        valid_seed_filenames = _build_valid_seed_filenames(ctx, continuity_chapters, narrative_templates)
        removed_section_ids = set(ctx.overrides_document.removed_sections)
        if (
            ctx.trusted_baseline_issue_number is not None
            and not ctx.no_seed
            and not seeding_disabled
        ):
            seed_narratives_from_prior(
                ctx.edition_name,
                target_issue_number=ctx.resolved_issue_number,
                source_issue_number=ctx.trusted_baseline_issue_number,
                valid_filenames=valid_seed_filenames,
                removed_section_ids=removed_section_ids,
                reports_root=ctx.reports_root,
                archive_root=ctx.archive_root,
            )
            if ctx.resolved_issue_number == ctx.trusted_baseline_issue_number + 1:
                sync_published_baseline_to_target(
                    ctx.edition_name,
                    target_issue_number=ctx.resolved_issue_number,
                    source_issue_number=ctx.trusted_baseline_issue_number,
                    reports_root=ctx.reports_root,
                    archive_root=ctx.archive_root,
                    valid_filenames=valid_seed_filenames,
                    removed_section_ids=removed_section_ids,
                )
        existing_narratives = load_narratives(
            ctx.edition_name,
            ctx.resolved_issue_number,
            reports_root=ctx.reports_root,
        )
        if active_chapters:
            narrative_templates.update(
                {
                    name: content
                    for name, content in existing_narratives.items()
                    if name.startswith("ws_") and name not in narrative_templates
                }
            )
        elif ctx.resolved_edition_type == EditionType.DETAILED and ctx.trusted_baseline_issue_number is not None:
            narrative_templates.update(
                {
                    name: content
                    for name, content in existing_narratives.items()
                    if name.startswith("ws_")
                    and name not in narrative_templates
                    and content.strip()
                    and not content.startswith(REMOVED_SECTION_MARKER)
                    and name.removeprefix("ws_").removesuffix(".md") not in set(ctx.overrides_document.removed_sections)
                }
            )
        narratives_dir = merge_narratives(
            ctx.edition_name,
            ctx.resolved_issue_number,
            templates=narrative_templates,
            reports_root=ctx.reports_root,
        )
        loaded_narratives = load_narratives(
            ctx.edition_name,
            ctx.resolved_issue_number,
            reports_root=ctx.reports_root,
        )
        loaded_exec_summary_text = loaded_narratives.get("exec_summary.md", "").strip()
        exec_summary_text = loaded_exec_summary_text or ctx.default_exec_summary

        if active_chapters:
            visible_section_ids = {
                chapter.id
                for chapter in active_chapters
                if chapter.id not in removed_section_ids
            }
            if ctx.section_filter_ids:
                visible_section_ids = _select_requested_section_ids(
                    ctx.section_filter_ids,
                    visible_section_ids,
                    edition_name=ctx.edition_name,
                )
            workstream_blurbs = support.active_chapter_notes(loaded_narratives, active_chapters)
            for chapter in active_chapters:
                workstream_blurbs.setdefault(chapter.id, "")
            supplemental_section_ids = set(visible_section_ids)
            if ctx.resolved_edition_type == EditionType.DETAILED and not ctx.section_filter_ids:
                raw_visible_section_ids = support.visible_detail_section_ids(
                    ctx.bundle,
                    ctx.overrides_document,
                    edition_type=ctx.resolved_edition_type,
                    items=ctx.items,
                    scorecards=ctx.scorecards,
                    scorecard_packets=ctx.scorecard_packets,
                    deltas=ctx.deltas,
                    scorecard_deltas=ctx.scorecard_deltas,
                    top_items=top_items,
                )
                bridge_visible_section_ids, bridge_diagnostic_section_ids = build_bridge_section_roster_ids(
                    edition_name=ctx.edition_name,
                    edition_type=ctx.resolved_edition_type,
                    trusted_issue=ctx.trusted_baseline_issue_number,
                    reports_root=ctx.reports_root,
                    archive_root=ctx.archive_root,
                    current_section_ids=raw_visible_section_ids,
                    loaded_narratives=loaded_narratives,
                    removed_section_ids=removed_section_ids,
                )
                workstream_blurbs.update(
                    support.active_workstream_blurbs(loaded_narratives, bridge_visible_section_ids)
                )
                supplemental_section_ids |= bridge_diagnostic_section_ids
            if (
                ctx.resolved_edition_type == EditionType.FOCUSED
                and not ctx.section_filter_ids
                and ctx.resolved_v2 is not None
                and ctx.programs_root is not None
            ):
                preview_visible_section_ids = _registry_preview_visible_section_ids(ctx, loaded_narratives)
                workstream_blurbs.update(
                    support.active_workstream_blurbs(loaded_narratives, preview_visible_section_ids)
                )
                supplemental_section_ids |= preview_visible_section_ids
            if ctx.section_filter_ids:
                workstream_blurbs = {
                    section_id: blurb
                    for section_id, blurb in workstream_blurbs.items()
                    if section_id in visible_section_ids
                }
            workstream_narrative_history = {}
            section_roster_current_ids = tuple(sorted(("exec_summary", *visible_section_ids, *supplemental_section_ids)))
        else:
            if ctx.section_filter_ids:
                raw_visible_section_ids = support.visible_detail_section_ids(
                    ctx.bundle,
                    ctx.overrides_document,
                    edition_type=EditionType.DETAILED,
                    items=ctx.items,
                    scorecards=ctx.scorecards,
                    scorecard_packets=ctx.scorecard_packets,
                    deltas=ctx.deltas,
                    scorecard_deltas=ctx.scorecard_deltas,
                    top_items=top_items,
                )
            else:
                raw_visible_section_ids = support.visible_detail_section_ids(
                    ctx.bundle,
                    ctx.overrides_document,
                    edition_type=ctx.resolved_edition_type,
                    items=ctx.items,
                    scorecards=ctx.scorecards,
                    scorecard_packets=ctx.scorecard_packets,
                    deltas=ctx.deltas,
                    scorecard_deltas=ctx.scorecard_deltas,
                    top_items=top_items,
                )
                if (
                    ctx.resolved_edition_type == EditionType.FOCUSED
                    and ctx.resolved_v2 is not None
                    and ctx.programs_root is not None
                ):
                    raw_visible_section_ids |= _registry_preview_visible_section_ids(ctx, loaded_narratives)
            visible_section_ids, diagnostic_section_ids = build_bridge_section_roster_ids(
                edition_name=ctx.edition_name,
                edition_type=ctx.resolved_edition_type,
                trusted_issue=ctx.trusted_baseline_issue_number,
                reports_root=ctx.reports_root,
                archive_root=ctx.archive_root,
                current_section_ids=raw_visible_section_ids,
                loaded_narratives=loaded_narratives,
                removed_section_ids=removed_section_ids,
            )
            if ctx.section_filter_ids:
                visible_section_ids = _select_requested_section_ids(
                    ctx.section_filter_ids,
                    visible_section_ids,
                    edition_name=ctx.edition_name,
                )
            workstream_blurbs = support.active_workstream_blurbs(loaded_narratives, visible_section_ids)
            workstream_narrative_history = build_workstream_narrative_history(
                edition=ctx.edition_name,
                issue_number=ctx.resolved_issue_number,
                workstream_names=(
                    tuple(workstream.name for workstream in ctx.bundle.program_context.workstreams)
                    if ctx.bundle.program_context is not None
                    else ()
                ),
                current_workstream_blurbs=workstream_blurbs,
                archive_root=ctx.archive_root,
            )
            section_roster_current_ids = tuple(sorted(("exec_summary", *diagnostic_section_ids)))

        return replace(
            ctx,
            top_items=top_items,
            auto_suggestions=auto_suggestions,
            continuity_chapters=active_chapters,
            narratives_dir=narratives_dir,
            narrative_seeding=load_narrative_seeding_state(
                ctx.edition_name,
                ctx.resolved_issue_number,
                reports_root=ctx.reports_root,
            ),
            loaded_narratives=loaded_narratives,
            visible_section_ids=visible_section_ids,
            section_roster_current_ids=section_roster_current_ids,
            loaded_exec_summary_text=loaded_exec_summary_text,
            exec_summary_text=exec_summary_text,
            workstream_blurbs=workstream_blurbs,
            workstream_narrative_history=workstream_narrative_history,
        )


def _select_requested_section_ids(
    requested_section_ids: tuple[str, ...],
    available_section_ids: set[str],
    *,
    edition_name: str,
) -> set[str]:
    missing_section_ids = [section_id for section_id in requested_section_ids if section_id not in available_section_ids]
    if missing_section_ids:
        raise ConfigError(
            f"--sections contains unknown or unavailable section ids for {edition_name}: {', '.join(missing_section_ids)}"
        )
    return {section_id for section_id in requested_section_ids}


def _registry_preview_visible_section_ids(ctx: StageContext, loaded_narratives: dict[str, str]) -> set[str]:
    if (
        ctx.bundle is None
        or ctx.bundle.slice_contracts is None
        or ctx.programs_root is None
        or ctx.resolved_v2 is None
        or ctx.resolved_issue_number is None
        or ctx.started_at is None
        or ctx.scorecard_packets is None
    ):
        return set()
    registry_entries = load_workstream_registry(
        program_id=ctx.resolved_v2.program.id,
        slice_contracts=ctx.bundle.slice_contracts,
        programs_root=ctx.programs_root,
        program_context=ctx.bundle.program_context,
    )
    preview_snapshot = build_workstream_issue_snapshot_from_packets(
        program_id=ctx.resolved_v2.program.id,
        issue_number=ctx.resolved_issue_number,
        edition=ctx.edition_name,
        generated_at=ctx.started_at,
        registry_entries=registry_entries,
        slice_contracts=ctx.bundle.slice_contracts,
        scorecard_packets=ctx.scorecard_packets,
        narrative_blurbs={
            name.removeprefix("ws_").removesuffix(".md"): content
            for name, content in loaded_narratives.items()
            if name.startswith("ws_") and name.endswith(".md") and content.strip()
        },
    )
    contracts_by_id = {contract.id: contract for contract in ctx.bundle.slice_contracts}
    visible_section_ids: set[str] = set()
    for entry in preview_snapshot.workstreams:
        if entry.report_relevance != "full_section":
            continue
        for slice_id in entry.source_slice_ids:
            contract = contracts_by_id.get(slice_id)
            if contract is not None:
                visible_section_ids.add(section_id_for_slice_contract(contract))
    return visible_section_ids


def _build_valid_seed_filenames(
    ctx: StageContext,
    continuity_chapters,
    narrative_templates: dict[str, str],
) -> set[str]:
    valid_filenames = set(narrative_templates)
    valid_filenames.add("exec_summary.md")
    if ctx.bundle is not None and ctx.bundle.program_context is not None:
        valid_filenames.update(
            f"ws_{build_anchor(workstream.name)}.md"
            for workstream in ctx.bundle.program_context.workstreams
        )
    if ctx.bundle is not None and ctx.bundle.slice_contracts is not None:
        valid_filenames.update(
            f"ws_{section_id_for_slice_contract(contract)}.md"
            for contract in ctx.bundle.slice_contracts
        )
    valid_filenames.update(f"chapter_{chapter.id}.md" for chapter in continuity_chapters)
    return valid_filenames


def _carried_forward_detail_section_ids(
    loaded_narratives: dict[str, str],
    *,
    removed_section_ids: set[str],
) -> set[str]:
    carried_forward: set[str] = set()
    for filename, content in loaded_narratives.items():
        if not filename.startswith("ws_") or not filename.endswith(".md"):
            continue
        if not content.strip() or content.startswith(REMOVED_SECTION_MARKER):
            continue
        section_id = filename.removeprefix("ws_").removesuffix(".md")
        if section_id in removed_section_ids:
            continue
        carried_forward.add(section_id)
    return carried_forward