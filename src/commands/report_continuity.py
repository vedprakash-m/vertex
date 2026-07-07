from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Callable

from src.core.attribution_engine import build_query_citation, build_section_citations
from src.core.chapter_contract_loader import ChapterDefinition
from src.core.config_loader import ReportBundle
from src.core.delta_engine import build_deltas
from src.core.forecast_engine import ETAForecast
from src.core.jinja_filters import build_anchor, risk_label
from src.core.models import AttributionTier, DeltaKind, DeltaSet, EditionType, EvidencePacket, ReviewState, ReviewStatus, RiskLevel, ScorecardDelta, ScorecardEvidencePacket, Snapshot, WorkItem
from src.core.narrative_store import REMOVED_SECTION_MARKER
from src.core.overrides_store import OverridesDocument
from src.core.slice_contract_loader import SliceContract
from src.core.view_models import Citation, ContinuityBandCell, ContinuityBandData, ContinuityChapterData, ContinuityChapterRowData, ContinuityJumpLink, ContinuityRenderData
from src.core.view_models import DimensionRisk, ScorecardData, Top3Item, WorkstreamData


@dataclass(frozen=True, slots=True)
class _ContinuityGeneratedSection:
    section_id: str
    title: str
    items: tuple[WorkItem, ...]


def _build_continuity_deltas(
    current_items: tuple[WorkItem, ...],
    previous_snapshot: Snapshot | None,
    issue_number: int,
    previous_issue_number: int | None,
    evidence_by_item: dict[int, EvidencePacket],
) -> DeltaSet:
    if previous_snapshot is None:
        return DeltaSet(
            issue_number=issue_number,
            previous_issue_number=None,
            new_items=(),
            closed_items=(),
            risk_changes=(),
            eta_changes=(),
            unchanged_count=len(current_items),
        )
    return build_deltas(
        current_items=current_items,
        previous_snapshot=previous_snapshot,
        issue_number=issue_number,
        previous_issue_number=previous_issue_number,
        evidence_by_item=evidence_by_item,
    )


def _has_usable_continuity_baseline(previous_snapshot: Snapshot | None) -> bool:
    return previous_snapshot is not None and bool(previous_snapshot.items)


def _continuity_chapter_title(chapter: ChapterDefinition, overrides_document: OverridesDocument) -> str:
    subtitle = overrides_document.chapter_subtitles.get(chapter.id) or chapter.subtitle
    if subtitle:
        return f"{chapter.title}: {subtitle}"
    return chapter.title


def _is_continuity_layout(bundle: ReportBundle) -> bool:
    return bundle.config.layout_mode == "continuity" and bundle.chapter_contract is not None


def _visible_continuity_chapters(
    bundle: ReportBundle,
    edition_type: EditionType | str,
) -> tuple[ChapterDefinition, ...]:
    if not _is_continuity_layout(bundle):
        return ()
    resolved_edition = edition_type.value if isinstance(edition_type, EditionType) else str(edition_type)
    assert bundle.chapter_contract is not None
    return tuple(
        chapter
        for chapter in bundle.chapter_contract.chapters_for(resolved_edition)
        if not chapter.chapter_exempt
    )


def _build_exec_summary_template(issue_number: int, *, layout_mode: str = "dashboard") -> str:
    if layout_mode == "continuity":
        signal_seeds = ["No WHAT MOVED signal extracted from current scorecard deltas."] * 3
        return "\n".join(
            [
                "<!-- SCAFFOLD -->",
                f"<!-- vertex:scaffold Issue {issue_number} — Executive Summary -->",
                "<!-- vertex:scaffold Continuity mode: replace these scaffold markers with final prose before publish. -->",
                "<!-- vertex:scaffold Shape: write one narrative executive-summary block with inline lane updates; do not use program-specific bucket headings or bullet lists. -->",
                "<!-- vertex:scaffold Heading: Executive Summary -->",
                "<!-- vertex:scaffold Program objective seed: Summarize the program objective for this issue. -->",
                "<!-- {PROGRAM_OBJECTIVE} -->",
                "<!-- vertex:scaffold Current state seed: Summarize the current state before the deltas. -->",
                "<!-- {CURRENT_STATE_SIGNAL} -->",
                f"<!-- vertex:scaffold WHAT MOVED seed 1: {signal_seeds[0]} -->",
                "<!-- {WHAT_CHANGED_SIGNAL_1} -->",
                f"<!-- vertex:scaffold WHAT MOVED seed 2: {signal_seeds[1]} -->",
                "<!-- {WHAT_CHANGED_SIGNAL_2} -->",
                f"<!-- vertex:scaffold WHAT MOVED seed 3: {signal_seeds[2]} -->",
                "<!-- {WHAT_CHANGED_SIGNAL_3} -->",
                "<!-- vertex:scaffold Trajectory seed: State whether the issue is improving, stable, or degrading. -->",
                "<!-- vertex:scaffold Severe signals seed: Call out any severe signals that still require attention. -->",
                "<!-- vertex:scaffold Decision / ask placeholder: {DECISION_OR_ASK} -->",
                "<!-- vertex:scaffold Keep the visible prose under the continuity word budget before publish. -->",
                "",
            ]
        )
    return "\n".join(
        [
            f"<!-- vertex:scaffold Issue {issue_number} — Executive Summary -->",
            "<!-- vertex:scaffold Format: write narrative executive-summary prose with inline lane updates; do not use program-specific bucket headings or bullet lists -->",
            "<!-- vertex:scaffold Verbosity cap: 150 words total -->",
            "",
            "[WHAT MOVED paragraph]",
            "",
            "<!-- state -->",
            "",
            "[WHERE WE ARE paragraph]",
            "",
        ]
    )


def _build_chapter_templates(
    issue_number: int,
    chapters: tuple[ChapterDefinition, ...],
) -> dict[str, str]:
    return {
        f"chapter_{chapter.id}.md": "\n".join(
            [
                f"<!-- vertex:scaffold Issue {issue_number} — {chapter.title} -->",
                "<!-- vertex:scaffold Optional chapter summary or note. Leave empty to render the table only. -->",
                "",
            ]
        )
        for chapter in chapters
    }


def _sanitize_scaffold_seed(value: str) -> str:
    normalized = " ".join(value.split())
    return normalized.replace("--", "-")


def _build_exec_summary_state_seed(dimension_risks: tuple[DimensionRisk, ...]) -> str:
    counts = {
        RiskLevel.HIGH: 0,
        RiskLevel.MEDIUM: 0,
        RiskLevel.LOW: 0,
        RiskLevel.DONE: 0,
    }
    for dimension in dimension_risks:
        if dimension.risk in counts:
            counts[dimension.risk] += 1
    if counts[RiskLevel.HIGH] == 0 and counts[RiskLevel.MEDIUM] == 0:
        return f"All tracked dimensions are at Low/Done ({counts[RiskLevel.LOW]} Low, {counts[RiskLevel.DONE]} Done)."
    return (
        f"Current inventory: {counts[RiskLevel.HIGH]} High, {counts[RiskLevel.MEDIUM]} Medium, "
        f"{counts[RiskLevel.LOW]} Low, {counts[RiskLevel.DONE]} Done."
    )


def _build_exec_summary_signal_seeds(
    auto_suggestions: tuple[Top3Item, ...],
    scorecard_deltas: tuple[ScorecardDelta, ...],
    dimension_risks: tuple[DimensionRisk, ...],
) -> tuple[str, str, str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add_candidate(candidate: str) -> None:
        normalized = candidate.strip()
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        candidates.append(normalized)

    for item in auto_suggestions:
        add_candidate(item.text)
    for delta in scorecard_deltas:
        add_candidate(
            f"{delta.dimension} moved {risk_label(delta.old_risk)} -> {risk_label(delta.new_risk)}. {delta.summary}"
        )
    risk_order = {
        RiskLevel.HIGH: 0,
        RiskLevel.MEDIUM: 1,
        RiskLevel.LOW: 2,
        RiskLevel.DONE: 3,
        RiskLevel.UNKNOWN: 4,
    }
    for dimension in sorted(dimension_risks, key=lambda model: (risk_order.get(model.risk, 5), model.name.lower())):
        add_candidate(f"{dimension.name} is {risk_label(dimension.risk)}. {dimension.summary}")
        if len(candidates) >= 3:
            break
    while len(candidates) < 3:
        candidates.append("No additional WHAT MOVED signal extracted from current deterministic inputs.")
    seeds = [_sanitize_scaffold_seed(c) for c in candidates[:3]]
    return seeds[0], seeds[1], seeds[2]


def _build_exec_summary_trajectory_seed(scorecard_deltas: tuple[ScorecardDelta, ...]) -> str:
    increased = [delta.dimension for delta in scorecard_deltas if delta.delta_kind == DeltaKind.RISK_UP]
    decreased = [delta.dimension for delta in scorecard_deltas if delta.delta_kind == DeltaKind.RISK_DOWN]
    if increased:
        focus = ", ".join(increased[:2])
        suffix = "" if len(increased) <= 2 else ", plus additional risk movement"
        return _sanitize_scaffold_seed(f"Degrading — risk increased in {focus}{suffix}.")
    if decreased:
        focus = ", ".join(decreased[:2])
        suffix = "" if len(decreased) <= 2 else ", plus additional improvements"
        return _sanitize_scaffold_seed(f"Improving — risk decreased in {focus}{suffix}.")
    return "Stable — no scorecard risk movement detected."


def _build_exec_summary_severe_signal_seeds(
    auto_suggestions: tuple[Top3Item, ...],
    *,
    is_decision_type: Callable[[str], bool],
    is_risk_type: Callable[[str], bool],
) -> tuple[str, ...]:
    severe = [
        _sanitize_scaffold_seed(item.text)
        for item in auto_suggestions
        if is_decision_type(item.item_type) or is_risk_type(item.item_type)
    ]
    if severe:
        return tuple(severe[:3])
    return ("No severe scorecard escalation detected from current ADO deltas.",)


def _build_continuity_exec_summary_template(
    *,
    issue_number: int,
    program_objective: str | None,
    auto_suggestions: tuple[Top3Item, ...],
    scorecard_deltas: tuple[ScorecardDelta, ...],
    dimension_risks: tuple[DimensionRisk, ...],
    is_decision_type: Callable[[str], bool],
    is_risk_type: Callable[[str], bool],
) -> str:
    signal_seeds = _build_exec_summary_signal_seeds(auto_suggestions, scorecard_deltas, dimension_risks)
    severe_signal_text = "; ".join(
        _build_exec_summary_severe_signal_seeds(
            auto_suggestions,
            is_decision_type=is_decision_type,
            is_risk_type=is_risk_type,
        )
    )
    return "\n".join(
        [
            "<!-- SCAFFOLD -->",
            f"<!-- vertex:scaffold Issue {issue_number} — Executive Summary -->",
            "<!-- vertex:scaffold Continuity mode: replace these scaffold markers with final prose before publish. -->",
            "<!-- vertex:scaffold Heading: Executive Summary -->",
            f"<!-- vertex:scaffold Program objective seed: {_sanitize_scaffold_seed(program_objective or 'Summarize the program objective for this issue.')} -->",
            "<!-- {PROGRAM_OBJECTIVE} -->",
            f"<!-- vertex:scaffold Current state seed: {_build_exec_summary_state_seed(dimension_risks)} -->",
            "<!-- {CURRENT_STATE_SIGNAL} -->",
            f"<!-- vertex:scaffold WHAT MOVED seed 1: {signal_seeds[0]} -->",
            "<!-- {WHAT_CHANGED_SIGNAL_1} -->",
            f"<!-- vertex:scaffold WHAT MOVED seed 2: {signal_seeds[1]} -->",
            "<!-- {WHAT_CHANGED_SIGNAL_2} -->",
            f"<!-- vertex:scaffold WHAT MOVED seed 3: {signal_seeds[2]} -->",
            "<!-- {WHAT_CHANGED_SIGNAL_3} -->",
            f"<!-- vertex:scaffold Trajectory seed: {_build_exec_summary_trajectory_seed(scorecard_deltas)} -->",
            f"<!-- vertex:scaffold Severe signals seed: {severe_signal_text} -->",
            "<!-- vertex:scaffold Decision / ask placeholder: {DECISION_OR_ASK} -->",
            "<!-- vertex:scaffold Keep the visible prose under the continuity word budget before publish. -->",
            "",
        ]
    )


def _active_chapter_notes(
    loaded_narratives: dict[str, str],
    chapters: tuple[ChapterDefinition, ...],
) -> dict[str, str]:
    notes: dict[str, str] = {}
    for chapter in chapters:
        filename = f"chapter_{chapter.id}.md"
        content = loaded_narratives.get(filename, "")
        if content.startswith(REMOVED_SECTION_MARKER):
            continue
        stripped = content.strip()
        if stripped:
            notes[chapter.id] = stripped
    return notes


def _iter_continuity_ai_sections(
    *,
    bundle: ReportBundle,
    edition_type: EditionType,
    items: tuple[WorkItem, ...],
    scorecards: tuple[ScorecardData, ...],
    scorecard_packets: dict[str, dict[str, ScorecardEvidencePacket]],
    chapter_title_builder: Callable[[ChapterDefinition], str],
) -> tuple[_ContinuityGeneratedSection, ...]:
    if bundle.chapter_contract is None:
        return ()
    item_lookup = {item.id: item for item in items}
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
    sections: list[_ContinuityGeneratedSection] = []
    for chapter in _visible_continuity_chapters(bundle, edition_type):
        chapter_items: list[WorkItem] = []
        seen_item_ids: set[int] = set()
        for dimension_id in chapter.dimensions:
            binding = bundle.chapter_contract.resolve_dimension(dimension_id)
            if binding is None:
                continue
            model = model_lookup.get(binding)
            packet = packet_lookup.get(binding)
            if model is None or packet is None or packet.total_items == 0:
                continue
            if not _dimension_included_in_continuity_membership(chapter, model):
                continue
            for item_id in packet.item_ids:
                if item_id in seen_item_ids or item_id not in item_lookup:
                    continue
                seen_item_ids.add(item_id)
                chapter_items.append(item_lookup[item_id])
        if chapter_items:
            sections.append(
                _ContinuityGeneratedSection(
                    section_id=chapter.id,
                    title=chapter_title_builder(chapter),
                    items=tuple(chapter_items),
                )
            )
    return tuple(sections)


def _dimension_included_in_continuity_membership(
    chapter: ChapterDefinition,
    model: DimensionRisk,
) -> bool:
    if model.risk == RiskLevel.DONE:
        return False
    if model.risk == RiskLevel.LOW and not chapter.include_low_risk_dimensions:
        return False
    return True


def _build_continuity_workstream_data(
    *,
    issue_number: int,
    bundle: ReportBundle,
    edition_type: EditionType,
    scorecards: tuple[ScorecardData, ...],
    scorecard_packets: dict[str, dict[str, ScorecardEvidencePacket]],
    overrides_document: OverridesDocument,
    workstream_blurbs: dict[str, str],
    review_status: ReviewStatus,
    evidence_by_item: dict[int, EvidencePacket],
    items: tuple[WorkItem, ...],
    chapter_title_builder: Callable[[ChapterDefinition], str],
    higher_risk: Callable[[RiskLevel, RiskLevel], RiskLevel],
    ado_base_url: str,
    chapters: tuple[ChapterDefinition, ...] | None = None,
    eta_forecasts: dict[int, ETAForecast] | None = None,
    source_footnotes: dict[str, str] | None = None,
) -> tuple[WorkstreamData, ...]:
    assert bundle.chapter_contract is not None
    forecast_lookup = eta_forecasts or {}
    source_footnotes = source_footnotes or {}
    review_lookup = {section.section_id: section.state for section in review_status.sections}
    item_lookup = {item.id: item for item in items}
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
    active_chapters = chapters if chapters is not None else _visible_continuity_chapters(bundle, edition_type)
    for chapter in active_chapters:
        chapter_items: list[WorkItem] = []
        seen_item_ids: set[int] = set()
        chapter_risk = RiskLevel.UNKNOWN
        chapter_ado_query_url: str | None = None
        chapter_dimension_note: str | None = None
        blocked_count = 0
        overdue_count = 0
        unowned_count = 0
        for dimension_id in chapter.dimensions:
            binding = bundle.chapter_contract.resolve_dimension(dimension_id)
            if binding is None:
                continue
            model = model_lookup.get(binding)
            packet = packet_lookup.get(binding)
            if model is not None:
                chapter_risk = higher_risk(chapter_risk, model.risk)
                model_note = getattr(model, "note", None)
                if chapter_dimension_note is None and model_note:
                    chapter_dimension_note = model_note
                if not _dimension_included_in_continuity_membership(chapter, model):
                    continue
            if packet is not None and packet.ado_query_url and chapter_ado_query_url is None:
                chapter_ado_query_url = packet.ado_query_url
            if packet is None or packet.total_items == 0:
                continue
            blocked_count += packet.blocked_count
            overdue_count += packet.overdue_count
            unowned_count += packet.unowned_count
            for item_id in packet.item_ids:
                if item_id in seen_item_ids or item_id not in item_lookup:
                    continue
                seen_item_ids.add(item_id)
                chapter_items.append(item_lookup[item_id])

        chapter_title = chapter_title_builder(chapter)
        chapter_note = workstream_blurbs.get(chapter.id, "").strip()
        if not chapter_items and not chapter_note:
            continue
        primary_item = chapter_items[0] if chapter_items else None
        citations = _build_workstream_citations(
            tuple(chapter_items),
            evidence_by_item,
            ado_base_url=ado_base_url,
        )
        if not citations and chapter_ado_query_url:
            citations = (
                build_query_citation(
                    title=chapter_title,
                    ado_url=chapter_ado_query_url,
                ),
            )
        workstreams.append(
            WorkstreamData(
                section_id=chapter.id,
                title=chapter_title,
                blurb=chapter_note,
                significant_findings=_workstream_significant_findings(tuple(chapter_items)),
                dependency_cascades=(),
                items=tuple(chapter_items),
                citations=citations,
                review_state=review_lookup.get(f"ws:{chapter.id}", ReviewState.PENDING),
                risk=chapter_risk,
                eta_label=_append_eta_forecast_annotation(
                    _continuity_eta_label(primary_item) if primary_item is not None else None,
                    forecast_lookup.get(primary_item.id) if primary_item is not None else None,
                ),
                summary=chapter_note,
                ado_query_url=chapter_ado_query_url,
                total_items=len(chapter_items),
                blocked_count=blocked_count,
                overdue_count=overdue_count,
                unowned_count=unowned_count,
                edit_path=f"narratives/issue_{issue_number:03d}/chapter_{chapter.id}.md",
                edit_line=1,
                narrative_empty=(chapter.id in workstream_blurbs and not chapter_note),
                note=chapter_dimension_note,
                source_footnote=source_footnotes.get(chapter.id),
            )
        )
    return tuple(workstreams)


def _workstream_significant_findings(
    items: tuple[WorkItem, ...],
    *,
    limit: int = 4,
) -> tuple[str, ...]:
    findings: list[str] = []
    seen: set[str] = set()
    for item in items:
        raw_findings = item.custom_fields.get("significant_findings")
        if not isinstance(raw_findings, list):
            continue
        for raw_finding in raw_findings:
            text = None if raw_finding is None else str(raw_finding).strip()
            if not text:
                continue
            prefixed = text if "ADO#" in text else f"ADO#{item.id}: {text}"
            if prefixed in seen:
                continue
            seen.add(prefixed)
            findings.append(prefixed)
            if len(findings) >= limit:
                return tuple(findings)
    return tuple(findings)


def _build_workstream_citations(
    items: tuple[WorkItem, ...],
    evidence_by_item: dict[int, EvidencePacket],
    *,
    ado_base_url: str,
    limit: int = 6,
) -> tuple[Citation, ...]:
    citations = build_section_citations(
        items,
        {item.id: evidence_by_item[item.id] for item in items if item.id in evidence_by_item},
        ado_base_url=ado_base_url,
    )
    if citations:
        return citations[:limit]

    fallback: list[Citation] = []
    for item in sorted(items, key=lambda candidate: (candidate.target_date or date.max, candidate.id)):
        fallback.append(
            Citation(
                work_item_id=item.id,
                title=item.title,
                ado_url=f"{ado_base_url}/{item.id}",
                tier=AttributionTier.TIER2,
            )
        )
        if len(fallback) >= limit:
            break
    return tuple(fallback)


def _build_continuity_render_data(
    *,
    bundle: ReportBundle,
    issue_number: int,
    edition_type: EditionType,
    overrides_document: OverridesDocument,
    scorecards: tuple[ScorecardData, ...],
    scorecard_packets: dict[str, dict[str, ScorecardEvidencePacket]],
    workstream_data: tuple[WorkstreamData, ...],
    items: tuple[WorkItem, ...],
    item_urls: dict[int, str],
    eta_forecasts: dict[int, ETAForecast],
    slice_contracts: dict[tuple[str, str], SliceContract],
    order_dimensions: Callable[[tuple[DimensionRisk, ...], str], tuple[DimensionRisk, ...]],
) -> ContinuityRenderData | None:
    if not _is_continuity_layout(bundle):
        return None
    assert bundle.chapter_contract is not None

    chapter_defs = {chapter.id: chapter for chapter in _visible_continuity_chapters(bundle, edition_type)}
    active_chapters = tuple(workstream for workstream in workstream_data if workstream.section_id in chapter_defs)
    active_chapter_ids = {chapter.section_id for chapter in active_chapters}
    item_lookup = {item.id: item for item in items}
    binding_to_dimension_id = {
        binding: dimension_id
        for dimension_id, binding in bundle.chapter_contract.dimension_lookup.items()
    }
    dimension_to_chapter = {
        dimension_id: chapter.id
        for chapter in chapter_defs.values()
        for dimension_id in chapter.dimensions
        if chapter.id in active_chapter_ids
    }
    model_lookup = {
        (scorecard.scorecard_name, dimension.name): dimension
        for scorecard in scorecards
        for dimension in scorecard.dimensions
    }

    scorecard_bands: list[ContinuityBandData] = []
    for scorecard in scorecards:
        ordered_dimensions = order_dimensions(scorecard.dimensions, bundle.config.scorecard_sort)
        cells: list[ContinuityBandCell] = []
        for dimension in ordered_dimensions:
            binding = (scorecard.scorecard_name, dimension.name)
            dimension_id = binding_to_dimension_id.get(binding)
            chapter_id = dimension_to_chapter.get(dimension_id or "")
            packet = scorecard_packets.get(scorecard.scorecard_name, {}).get(dimension.name)
            cells.append(
                ContinuityBandCell(
                    dimension_id=dimension_id or build_anchor(f"{scorecard.scorecard_name}-{dimension.name}"),
                    label=dimension.display_name or dimension.name,
                    risk=dimension.risk,
                    anchor_id=_continuity_chapter_anchor(chapter_id) if chapter_id else None,
                    query_url=packet.ado_query_url if packet is not None else None,
                    eta_label=_continuity_packet_eta_label(packet, "%m/%d") if packet is not None else None,
                )
            )
        if not cells:
            continue
        band_id = _continuity_scorecard_band_id(scorecard.scorecard_name)
        scorecard_bands.append(
            ContinuityBandData(
                band_id=band_id,
                title=_continuity_scorecard_title(scorecard.scorecard_name),
                anchor_id=f"scorecard-{band_id}",
                cells=tuple(cells),
            )
        )

    chapters: list[ContinuityChapterData] = []
    workstream_lookup = {workstream.section_id: workstream for workstream in active_chapters}
    for chapter_id in [workstream.section_id for workstream in active_chapters]:
        chapter = chapter_defs[chapter_id]
        workstream = workstream_lookup[chapter_id]
        rows: list[ContinuityChapterRowData] = []
        row_number = 1
        for dimension_id in chapter.dimensions:
            dim_binding = bundle.chapter_contract.resolve_dimension(dimension_id)
            if dim_binding is None:
                continue
            packet = scorecard_packets.get(dim_binding[0], {}).get(dim_binding[1])
            model = model_lookup.get(dim_binding)
            if packet is None or model is None or packet.total_items == 0:
                continue
            slice_contract = slice_contracts.get(dim_binding)
            rows.append(
                _build_continuity_chapter_row(
                    row_number=row_number,
                    label=model.display_name or model.name,
                    risk=model.risk,
                    packet=packet,
                    item_lookup=item_lookup,
                    item_urls=item_urls,
                    eta_forecasts=eta_forecasts,
                    summary_text=model.summary,
                    slice_contract=slice_contract,
                )
            )
            row_number += 1
        if not rows and not workstream.blurb.strip():
            continue
        chapters.append(
            ContinuityChapterData(
                chapter_id=chapter.id,
                anchor_id=_continuity_chapter_anchor(chapter.id),
                title=workstream.title,
                note=workstream.blurb,
                chapter_owner=_continuity_chapter_owner(chapter, overrides_document),
                rows=tuple(rows),
                review_state=workstream.review_state,
            )
        )

    return ContinuityRenderData(
        brand_name=bundle.config.brand_name,
        brand_header_url=bundle.config.brand_header_url,
        edition_intro=overrides_document.edition_intro,
        cadence_note=_resolve_cadence_note(bundle, issue_number, edition_type),
        scorecard_bands=tuple(scorecard_bands),
        jump_links=tuple(
            ContinuityJumpLink(label=chapter.title, anchor_id=chapter.anchor_id)
            for chapter in chapters
            if chapter_defs[chapter.chapter_id].show_in_jump_list
        ),
        chapters=tuple(chapters),
    )


def _continuity_scorecard_band_id(scorecard_name: str) -> str:
    return build_anchor(scorecard_name)


def _continuity_scorecard_title(scorecard_name: str) -> str:
    return f"{scorecard_name} (Risk levels)"


def _build_continuity_chapter_row(
    *,
    row_number: int,
    label: str,
    risk: RiskLevel,
    packet: ScorecardEvidencePacket,
    item_lookup: dict[int, WorkItem],
    item_urls: dict[int, str],
    eta_forecasts: dict[int, ETAForecast],
    summary_text: str | None,
    slice_contract: SliceContract | None,
) -> ContinuityChapterRowData:
    row_items = tuple(item_lookup[item_id] for item_id in packet.item_ids if item_id in item_lookup)
    primary_item = row_items[0] if row_items else None
    issue_text, approach_text = _build_continuity_row_copy(
        label,
        risk,
        packet,
        row_items,
        summary_text=summary_text,
    )
    work_item_id = primary_item.id if primary_item is not None else None
    owner = _continuity_owner_label(slice_contract, primary_item)
    state_label = primary_item.state if primary_item is not None else None
    eta_base_label = _continuity_packet_eta_label(packet) or (_continuity_eta_label(primary_item) if primary_item is not None else None)
    eta_label = _append_eta_forecast_annotation(
        eta_base_label,
        eta_forecasts.get(primary_item.id) if primary_item is not None else None,
    )
    return ContinuityChapterRowData(
        row_number=row_number,
        label=label,
        risk=risk,
        owner=owner,
        team=None,
        state_label=state_label,
        eta_label=eta_label,
        issue_text=issue_text,
        approach_text=approach_text,
        ado_work_item_id=work_item_id,
        ado_url=item_urls.get(work_item_id) if work_item_id is not None else None,
    )


def _build_continuity_row_copy(
    label: str,
    risk: RiskLevel,
    packet: ScorecardEvidencePacket,
    row_items: tuple[WorkItem, ...],
    summary_text: str | None,
) -> tuple[str, str | None]:
    if summary_text is not None and summary_text.strip():
        supporting_lines: list[str] = []
        if row_items:
            state = row_items[0].state.strip()
            if state:
                supporting_lines.append(f"Current state: {state}.")
        checkpoint_label = _continuity_packet_eta_label(packet)
        if checkpoint_label is not None:
            supporting_lines.append(f"Next checkpoint: {checkpoint_label}.")
        return summary_text.strip(), " ".join(supporting_lines) or None

    description_text = ""
    if row_items:
        description_text = _clean_continuity_text(
            str(
                row_items[0].custom_fields.get("description")
                or row_items[0].custom_fields.get("System.Description")
                or ""
            )
        )
    if description_text:
        sentences = [sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+", description_text) if sentence.strip()]
        issue_text = " ".join(sentences[:2]).strip() or packet.dimension_description.strip()
        remainder = " ".join(sentences[2:4]).strip()
        approach_text = remainder or None
        if issue_text:
            return issue_text, approach_text
    issue_text = packet.dimension_description.strip() or f"{label} is currently {risk_label(risk)} based on the active ADO inventory."
    if packet.total_items == 0:
        return issue_text, None
    approach_text = (
        f"{packet.total_items} ADO items remain in scope"
        f" with {packet.blocked_count} blocked, {packet.overdue_count} overdue, and {packet.unowned_count} unowned."
    )
    return issue_text, approach_text


def _continuity_owner_label(
    slice_contract: SliceContract | None,
    item: WorkItem | None,
) -> str | None:
    if slice_contract is not None and slice_contract.owners.primary.strip():
        return slice_contract.owners.primary
    if item is not None and item.assigned_to:
        return item.assigned_to
    return None


def _resolve_cadence_note(
    bundle: ReportBundle,
    issue_number: int,
    edition_type: EditionType,
) -> str:
    if bundle.config.cadence_note is None:
        return ""
    if bundle.config.cadence_note.first_issue_override and issue_number == 77:
        return bundle.config.cadence_note.first_issue_override
    if edition_type == EditionType.FOCUSED:
        return bundle.config.cadence_note.focused
    return bundle.config.cadence_note.detailed


def _continuity_chapter_owner(chapter: ChapterDefinition, overrides_document: OverridesDocument) -> str | None:
    return overrides_document.chapter_owner_overrides.get(chapter.id) or chapter.chapter_owner


def _continuity_chapter_anchor(chapter_id: str | None) -> str:
    return f"chapter-{chapter_id}" if chapter_id else "chapter"


def _clean_continuity_text(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value)
    return " ".join(without_tags.split())


def _continuity_eta_label(item: WorkItem) -> str | None:
    iteration_parts = [segment.strip() for segment in item.iteration_path.split("\\") if segment.strip()]
    iteration_label = iteration_parts[-1] if iteration_parts else None
    if iteration_label is not None:
        iteration_label = re.sub(r"^Issue\s+\d+\s*-\s*", "", iteration_label, flags=re.IGNORECASE)
        if re.fullmatch(r"Issue\s+\d+", iteration_label, flags=re.IGNORECASE):
            iteration_label = None
    target_label = item.target_date.strftime("%b %d") if item.target_date is not None else None
    if iteration_label and target_label:
        return f"{iteration_label} - {target_label}"
    return target_label or iteration_label


def _append_eta_forecast_annotation(base_label: str | None, forecast: ETAForecast | None) -> str | None:
    if forecast is None:
        return base_label
    if forecast.display_annotation is None:
        return base_label
    if base_label is None:
        return forecast.display_annotation
    return f"{base_label} ({forecast.display_annotation})"


def _continuity_packet_eta_label(
    packet: ScorecardEvidencePacket,
    fmt: str = "%b %d",
) -> str | None:
    target_date = packet.author_target_date or packet.next_target_date or packet.latest_target_date
    if target_date is None:
        return None
    return target_date.strftime(fmt)
