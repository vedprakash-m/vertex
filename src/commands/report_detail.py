from __future__ import annotations

import re
from typing import Any, Callable

from src.core.forecast_engine import ETAForecast
from src.core.config_loader import ReportBundle
from src.core.attribution_engine import build_query_citation
from src.core.jinja_filters import build_anchor, risk_label
from src.core.models import DeltaSet, DimensionRisk, EditionType, EvidencePacket, ReviewState, RiskLevel, ScorecardDelta, ScorecardEvidencePacket, WorkItem
from src.core.overrides_store import OverridesDocument
from src.core.view_models import ScorecardData, Top3Item, WorkstreamData


def _detail_section_id(scorecard_name: str, dimension_name: str) -> str:
    return build_anchor(f"{scorecard_name}-{dimension_name}")


def _is_presented_scorecard_dimension(dimension: Any) -> bool:
    # Linked dimensions should still appear in scorecards so layout and
    # executive visuals stay consistent with vetted scorecard definitions.
    return True


def _hidden_detail_dimensions(overrides_document: OverridesDocument) -> set[tuple[str, str]]:
    return {
        (scorecard.name, dimension.name)
        for scorecard in overrides_document.scorecards
        for dimension in scorecard.dimensions
        if dimension.hide_details
    }


def _slice_contract_map(bundle: ReportBundle) -> dict[tuple[str, str], Any]:
    return {
        (contract.scorecard_name, contract.title): contract
        for contract in (bundle.slice_contracts or ())
    }


def _focused_delta_item_ids(deltas: DeltaSet | None) -> set[int]:
    if deltas is None:
        return set()
    return {
        delta.work_item_id
        for delta_group in (deltas.new_items, deltas.closed_items, deltas.risk_changes, deltas.eta_changes)
        for delta in delta_group
    }


def _visible_detail_section_ids(
    bundle: ReportBundle,
    overrides_document: OverridesDocument,
    *,
    edition_type: EditionType | str | None = None,
    items: tuple[WorkItem, ...] = (),
    scorecards: tuple[ScorecardData, ...] = (),
    scorecard_packets: dict[str, dict[str, ScorecardEvidencePacket]] | None = None,
    deltas: DeltaSet | None = None,
    scorecard_deltas: tuple[ScorecardDelta, ...] = (),
    top_items: tuple[Top3Item, ...] = (),
    assign_dimension_items: Callable[..., Any],
    ado_query_base_url: str,
    slice_contracts: dict[tuple[str, str], Any],
) -> set[str]:
    hidden_dimensions = _hidden_detail_dimensions(overrides_document)
    base_visible_ids = {
        _detail_section_id(scorecard.name, dimension.name)
        for scorecard in bundle.config.scorecards
        for dimension in scorecard.dimensions
        if _is_presented_scorecard_dimension(dimension)
        if (scorecard.name, dimension.name) not in hidden_dimensions
    }
    base_visible_ids -= set(overrides_document.removed_sections)
    if edition_type is None:
        resolved_edition_type = EditionType.DETAILED
    elif isinstance(edition_type, EditionType):
        resolved_edition_type = edition_type
    else:
        resolved_edition_type = EditionType.from_string(str(edition_type))
    if resolved_edition_type != EditionType.FOCUSED:
        return base_visible_ids

    forced_section_ids = {section_id for section_id in overrides_document.focused_include if section_id in base_visible_ids}
    if not scorecards:
        return forced_section_ids

    changed_item_ids = _focused_delta_item_ids(deltas)
    changed_dimensions = {delta.dimension for delta in scorecard_deltas}
    top_item_anchors = {item.anchor for item in top_items}
    packet_maps = scorecard_packets or {}
    visible_section_ids = set(forced_section_ids)

    for scorecard in bundle.config.scorecards:
        scorecard_anchor = build_anchor(scorecard.name)
        packet_map = packet_maps.get(scorecard.name, {})
        for dimension in scorecard.dimensions:
            section_id = _detail_section_id(scorecard.name, dimension.name)
            if section_id not in base_visible_ids or section_id in visible_section_ids:
                continue
            if section_id in top_item_anchors or scorecard_anchor in top_item_anchors:
                visible_section_ids.add(section_id)
                continue
            if dimension.name in changed_dimensions:
                visible_section_ids.add(section_id)
                continue
            matching_items = assign_dimension_items(
                items,
                dimension,
                slice_contract=slice_contracts.get((scorecard.name, dimension.name)),
                ado_query_base_url=ado_query_base_url,
            ).items
            if any(item.id in changed_item_ids for item in matching_items):
                visible_section_ids.add(section_id)
                continue
            packet = packet_map.get(dimension.name)
            if packet is not None and any(item_id in changed_item_ids for item_id in packet.item_ids):
                visible_section_ids.add(section_id)

    return visible_section_ids


def _iter_detail_sections(
    *,
    bundle: ReportBundle,
    items: tuple[WorkItem, ...],
    scorecards: tuple[ScorecardData, ...],
    scorecard_packets: dict[str, dict[str, ScorecardEvidencePacket]],
    overrides_document: OverridesDocument,
    assign_dimension_items: Callable[..., Any],
    ado_query_base_url: str,
    slice_contracts: dict[tuple[str, str], Any],
    dimension_sort_rank: Callable[[DimensionRisk], int],
) -> tuple[tuple[str, str, DimensionRisk, ScorecardEvidencePacket, tuple[WorkItem, ...]], ...]:
    dimension_lookup = {
        (scorecard.scorecard_name, dimension.name): dimension
        for scorecard in scorecards
        for dimension in scorecard.dimensions
    }
    hidden_dimensions = _hidden_detail_dimensions(overrides_document)
    sections: list[tuple[str, str, DimensionRisk, ScorecardEvidencePacket, tuple[WorkItem, ...]]] = []
    for scorecard in bundle.config.scorecards:
        packet_map = scorecard_packets.get(scorecard.name, {})
        for dimension in scorecard.dimensions:
            if not _is_presented_scorecard_dimension(dimension):
                continue
            if (scorecard.name, dimension.name) in hidden_dimensions:
                continue
            model = dimension_lookup.get((scorecard.name, dimension.name))
            packet = packet_map.get(dimension.name)
            if model is None or packet is None:
                continue
            if bundle.config.scorecard_sort != "fixed" and model.risk == RiskLevel.DONE:
                continue
            matching_items = assign_dimension_items(
                items,
                dimension,
                slice_contract=slice_contracts.get((scorecard.name, dimension.name)),
                ado_query_base_url=ado_query_base_url,
            ).items
            sections.append(
                (
                    scorecard.name,
                    _detail_section_id(scorecard.name, dimension.name),
                    model,
                    packet,
                    matching_items,
                )
            )
    if bundle.config.scorecard_sort != "fixed":
        sections.sort(key=lambda entry: -dimension_sort_rank(entry[2]))
    return tuple(sections)


def _build_workstream_templates(
    issue_number: int,
    bundle: ReportBundle,
    items: tuple[WorkItem, ...],
    scorecards: tuple[ScorecardData, ...],
    scorecard_packets: dict[str, dict[str, ScorecardEvidencePacket]],
    overrides_document: OverridesDocument,
    *,
    assign_dimension_items: Callable[..., Any],
    ado_query_base_url: str,
    slice_contracts: dict[tuple[str, str], Any],
    dimension_sort_rank: Callable[[DimensionRisk], int],
) -> dict[str, str]:
    templates: dict[str, str] = {}
    for scorecard_name, section_id, model, packet, _ in _iter_detail_sections(
        bundle=bundle,
        items=items,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        overrides_document=overrides_document,
        assign_dimension_items=assign_dimension_items,
        ado_query_base_url=ado_query_base_url,
        slice_contracts=slice_contracts,
        dimension_sort_rank=dimension_sort_rank,
    ):
        templates[f"ws_{section_id}.md"] = "\n".join(
            [
                f"<!-- vertex:scaffold Issue {issue_number} — {model.display_name or model.name} -->",
                f"<!-- vertex:scaffold Scorecard: {scorecard_name} -->",
                f"<!-- vertex:scaffold Current call: {risk_label(model.risk)}. {model.summary} -->",
                f"<!-- vertex:scaffold Evidence: {packet.total_items} items ({packet.items_by_risk.get('high', 0)} High, {packet.items_by_risk.get('medium', 0)} Medium, {packet.items_by_risk.get('low', 0)} Low, {packet.items_by_risk.get('done', 0)} Done), {packet.stale_count} stale -->",
                "<!-- vertex:scaffold Editorial: Lead with delta. Max 3 sentences, 60 words. -->",
                "",
                "[Your narrative here]",
                "",
            ]
        )
    return templates


def _normalize_workstream_blurb(text: str) -> str:
    normalized = re.sub(r"^\s*Objective:\s*", "", text, count=1, flags=re.IGNORECASE).strip()
    if normalized and normalized[0].islower():
        normalized = normalized[0].upper() + normalized[1:]
    return normalized


def _resolve_workstream_blurb(
    *,
    section_id: str,
    workstream_blurbs: dict[str, str],
    model: DimensionRisk,
    packet: ScorecardEvidencePacket,
) -> str:
    if section_id not in workstream_blurbs:
        return _normalize_workstream_blurb(model.summary)
    explicit_blurb = workstream_blurbs.get(section_id, "").strip()
    if explicit_blurb:
        return _normalize_workstream_blurb(explicit_blurb)
    if model.risk in {RiskLevel.LOW, RiskLevel.DONE}:
        return f"{packet.total_items} items at {risk_label(model.risk)} - see ADO for current status."
    return ""


def _build_detail_workstream_data(
    *,
    issue_number: int,
    detail_sections: tuple[tuple[str, str, DimensionRisk, ScorecardEvidencePacket, tuple[WorkItem, ...]], ...],
    workstream_blurbs: dict[str, str],
    dependency_cascades: tuple[Any, ...],
    review_lookup: dict[str, ReviewState],
    evidence_by_item: dict[int, EvidencePacket],
    build_workstream_citations: Callable[[tuple[WorkItem, ...], dict[int, EvidencePacket]], tuple[Any, ...]],
    workstream_significant_findings: Callable[[tuple[WorkItem, ...]], tuple[str, ...]],
    cascade_messages_for_section: Callable[[str, str, tuple[Any, ...]], tuple[str, ...]],
    continuity_packet_eta_label: Callable[[ScorecardEvidencePacket, str], str | None],
    eta_forecasts: dict[int, ETAForecast],
    source_footnotes: dict[str, str] | None = None,
) -> tuple[WorkstreamData, ...]:
    workstreams: list[WorkstreamData] = []
    source_footnotes = source_footnotes or {}
    for scorecard_name, section_id, model, packet, workstream_items in detail_sections:
        citations = build_workstream_citations(workstream_items, evidence_by_item)
        ado_query_url = packet.ado_query_url or None
        if not citations and ado_query_url:
            citations = (
                build_query_citation(
                    title=model.display_name or model.name,
                    ado_url=ado_query_url,
                ),
            )
        workstreams.append(
            WorkstreamData(
                section_id=section_id,
                title=(model.display_name or model.name).rstrip("*").strip(),
                blurb=_resolve_workstream_blurb(
                    section_id=section_id,
                    workstream_blurbs=workstream_blurbs,
                    model=model,
                    packet=packet,
                ),
                significant_findings=workstream_significant_findings(workstream_items),
                dependency_cascades=cascade_messages_for_section(scorecard_name, model.name, dependency_cascades),
                items=workstream_items,
                citations=citations,
                review_state=review_lookup.get(f"ws:{section_id}", ReviewState.PENDING),
                scorecard_name=scorecard_name,
                risk=model.risk,
                prior_risk=packet.prior_confirmed_risk,
                derived_risk=model.derived_risk,
                override_risk=model.override_risk,
                eta_label=_append_eta_forecast_annotation(
                    continuity_packet_eta_label(packet, "%m/%d"),
                    eta_forecasts.get(workstream_items[0].id) if workstream_items else None,
                ),
                summary=model.summary,
                ado_query_url=ado_query_url,
                total_items=packet.total_items,
                blocked_count=packet.blocked_count,
                overdue_count=packet.overdue_count,
                unowned_count=packet.unowned_count,
                edit_path=f"narratives/issue_{issue_number:03d}/ws_{section_id}.md",
                edit_line=1,
                narrative_empty=section_id in workstream_blurbs and not workstream_blurbs.get(section_id, "").strip(),
                note=model.note,
                source_footnote=source_footnotes.get(section_id),
            )
        )
    return tuple(workstreams)


def _append_eta_forecast_annotation(base_label: str | None, forecast: ETAForecast | None) -> str | None:
    if forecast is None or forecast.display_annotation is None:
        return base_label
    if base_label is None:
        return forecast.display_annotation
    return f"{base_label} ({forecast.display_annotation})"
